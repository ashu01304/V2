"""Offline checks for automatic discovery and live-only monitoring."""

from datetime import datetime, timezone, timedelta
import unittest
from unittest.mock import MagicMock, patch

import requests

from market_data.live_monitor import LiveContractMonitor, active_contracts, discovery_requests, matching_instruments, manual_items


class LiveMonitorTests(unittest.TestCase):
    def test_confirmed_discovery_requests_cover_all_configured_products(self):
        self.assertEqual(discovery_requests(), [
            {"products": ["CL", "BRN", "G", "HO", "NG", "RB"], "strategies": ["OUT", "MS", "MF"]},
            {"products": ["+1xCL"], "strategies": [""]}])

    def test_no_selection_limit_and_blank_code_box_preserved(self):
        probe = MagicMock()
        monitor = LiveContractMonitor(probe)
        quotes = [{"instid": str(i), "product": "CL", "qh_code": f"test{i}", "strategy": "MS"} for i in range(601)]
        box = {"instid": "999", "product": "+1xCL", "qh_code": "", "strategy": "", "name": "box spread"}
        with patch("market_data.live_monitor.requests.Session") as session:
            session.return_value.__enter__.return_value.post.side_effect = [
                MagicMock(**{"json.return_value": {"data": quotes}}), MagicMock(**{"json.return_value": {"data": [box]}})]
            monitor._load()
        settings = probe.start.call_args.args[0]
        self.assertEqual(len(settings["items"]), 602)
        self.assertEqual(settings["instruments"]["TT-999"]["name"], "box spread")
        self.assertEqual(settings["batch_size"], 250)

    def test_fresh_post_snapshot_prices_only_and_box_label(self):
        now = datetime.now(timezone.utc)
        quotes = [
            {"item": "live", "live_updates": 2, "LastPrice": "1.2", "last_live_received_at_utc": now.isoformat()},
            {"item": "snapshot", "snapshot": True, "LastPrice": "3.1"},
            {"item": "empty", "live_updates": 1, "last_live_received_at_utc": now.isoformat()},
            {"item": "stale", "live_updates": 2, "BidPrice": "2.0", "last_live_received_at_utc": (now-timedelta(seconds=121)).isoformat()},
        ]
        state = {"status": "CONNECTED:WS-STREAMING", "settings": {"instruments": {"live": {
            "product": "+1xCL", "qh_code": "", "name": "CL/BZ box", "strategy": ""}}}, "rows": quotes}
        result = active_contracts(state, now=now)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["contract"], "CL/BZ box")
        self.assertEqual(result[0]["kind"], "BOX")
        self.assertEqual(result[0]["LastPrice"], 1.2)
        self.assertIsNone(result[0]["BidPrice"])
        state["status"] = "Stopped"
        self.assertEqual(active_contracts(state, now=now), [])

    def test_multiple_tabs_start_one_discovery_worker(self):
        monitor = LiveContractMonitor(MagicMock())
        with patch("market_data.live_monitor.threading.Thread") as worker:
            monitor.ensure_started()
            monitor.ensure_started()
            worker.assert_called_once()
            worker.return_value.start.assert_called_once()

    def test_partial_discovery_failure_is_visible(self):
        probe = MagicMock()
        monitor = LiveContractMonitor(probe)
        with patch("market_data.live_monitor.requests.Session") as session:
            session.return_value.__enter__.return_value.post.side_effect = [requests.Timeout("timed out"),
                MagicMock(**{"json.return_value": {"data": [{"product": "+1xCL", "instid": "9", "strategy": ""}]}})]
            monitor._load()
        self.assertEqual(len(monitor.catalogue), 1)
        self.assertIn("timed out", monitor.errors[0])
        probe.start.assert_called_once()

    def test_filters_and_explicit_selection_replace_actual_subscription(self):
        probe = MagicMock()
        probe.snapshot.return_value = {"settings": {"items": ["TT-1", "TT-2", "TT-3"]}}
        monitor = LiveContractMonitor(probe)
        monitor.catalogue = {
            "TT-1": {"product": "CL", "qh_code": "CLZ26", "strategy": "OUT"},
            "TT-2": {"product": "CL", "qh_code": "CLZ26-F27", "strategy": "MS"},
            "TT-3": {"product": "BRN", "qh_code": "COZ26-F27", "strategy": "MS"},
        }
        options = monitor.options(["CL"], ["MS"])
        self.assertEqual([option["value"] for option in options], ["TT-2"])
        monitor.apply_selection(["CL"], ["MS"], ["TT-2"])
        self.assertEqual(probe.start.call_args.args[0]["items"], ["TT-2"])
        self.assertEqual(list(matching_instruments(monitor.catalogue, ["CO"], ["MS"])), ["TT-3"])
        probe.start.reset_mock()
        with self.assertRaises(ValueError):
            monitor.apply_selection(["CL"], ["MS"], ["TT-3"])
        probe.start.assert_not_called()
        monitor.apply_selection([], [], [])
        probe.start.assert_not_called()  # Same complete universe: do not reconnect.

    def test_empty_match_does_not_stop_existing_subscription(self):
        monitor = LiveContractMonitor(MagicMock())
        monitor.catalogue = {"TT-1": {"product": "CL", "strategy": "OUT"}}
        with self.assertRaises(ValueError):
            monitor.apply_selection(["NG"], ["BOX"])
        monitor.probe.start.assert_not_called()

    def test_box_product_filter_normalizes_case_and_subscribes_exact_id(self):
        probe = MagicMock()
        probe.snapshot.return_value = {"settings": {"items": []}}
        monitor = LiveContractMonitor(probe)
        monitor.catalogue = {
            "TT-9": {"product": "+1xCL", "qh_code": "", "strategy": "", "name": "CL/BZ box"},
            "TT-10": {"product": "CL", "qh_code": "CLZ26-F27", "strategy": "MS"},
        }
        for product in ("+1xCL", "+1XCL", "+1xcl"):
            self.assertEqual(list(matching_instruments(monitor.catalogue, [product])), ["TT-9"])
        self.assertEqual([option["value"] for option in monitor.options(["+1xCL"], ["BOX"])], ["TT-9"])
        monitor.apply_selection(["+1xCL"], [], [])
        self.assertEqual(probe.start.call_args.args[0]["items"], ["TT-9"])
        self.assertEqual(probe.start.call_args.args[0]["instruments"]["TT-9"]["product"], "+1xCL")

    def test_manual_ids_preserve_large_values_and_support_unknown_items(self):
        self.assertEqual(manual_items("543741535855301483,tt-17510963859699672486\nTT-543741535855301483"),
                         ["TT-543741535855301483", "TT-17510963859699672486"])
        probe = MagicMock()
        probe.snapshot.return_value = {"settings": {"items": []}}
        monitor = LiveContractMonitor(probe)
        monitor.catalogue = {"TT-1": {"product": "+1xCL", "name": "box", "strategy": ""}}
        monitor.apply_ids("1, 17510963859699672486")
        settings = probe.start.call_args.args[0]
        self.assertEqual(settings["items"], ["TT-1", "TT-17510963859699672486"])
        self.assertEqual(settings["instruments"]["TT-1"]["name"], "box")
        self.assertEqual(settings["instruments"]["TT-17510963859699672486"]["strategy"], "ID")
        probe.start.reset_mock()
        with self.assertRaises(ValueError):
            monitor.apply_ids("1, invalid-id")
        probe.start.assert_not_called()


if __name__ == "__main__":
    unittest.main()

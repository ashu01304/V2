"""Offline checks only: catalogue/SDK calls are mocked."""

import unittest
from unittest.mock import MagicMock, patch

from market_data.live_probe import StreamProbe, catalogue_item, catalogue_rows


class LiveProbeTests(unittest.TestCase):
    def test_structure_catalogue_rows_are_not_filtered_or_truncated(self):
        rows = [{"product": "CL", "qh_code": "CLH27-2*J27+K27", "instid": 123, "strategy": "example"}]
        self.assertEqual(catalogue_rows({"data": rows}), rows)
        self.assertEqual(catalogue_item(rows[0]), "TT-123")
        self.assertEqual(catalogue_item({}), "")

    def test_exact_subscription_and_raw_fields_without_recording(self):
        settings = dict(server="https://example.invalid", adapter_set="adapter-set", data_adapter="prices",
                        user="test", mode="MERGE", snapshot="yes", frequency="1",
                        items=["TT-123"], fields=["BidPrice", "AskPrice", "Timestamp"])
        probe = StreamProbe()
        with patch("lightstreamer.client.LightstreamerClient") as client_class, patch("lightstreamer.client.Subscription") as sub_class:
            probe.start(settings, "secret")
            client_class.assert_called_once_with(settings["server"], "adapter-set")
            sub_class.assert_called_once_with("MERGE", ["TT-123"], settings["fields"])
            client = client_class.return_value
            subscription = sub_class.return_value
            subscription.setDataAdapter.assert_called_once_with("prices")
            client.connectionDetails.setPassword.assert_called_once_with("secret")
            listener = subscription.addListener.call_args.args[0]
            update = MagicMock()
            update.getItemName.return_value = "TT-123"
            update.isSnapshot.return_value = False
            values = {"BidPrice": "-1.25", "AskPrice": "-1.20", "Timestamp": "example timestamp"}
            update.getValue.side_effect = values.__getitem__
            listener.onItemUpdate(update)
            state = probe.snapshot()
            self.assertEqual(state["rows"][0]["BidPrice"], "-1.25")
            self.assertEqual(state["rows"][0]["AskPrice"], "-1.20")
            self.assertEqual(state["rows"][0]["updates"], 1)
            self.assertEqual(state["rows"][0]["live_updates"], 1)
            self.assertEqual(state["rows"][0]["snapshot_updates"], 0)
            update.isSnapshot.return_value = True
            listener.onItemUpdate(update)
            self.assertEqual(probe.snapshot()["rows"][0]["live_updates"], 1)
            self.assertEqual(probe.snapshot()["rows"][0]["snapshot_updates"], 1)
            self.assertNotIn("secret", str(state))
            probe.stop()
            client.disconnect.assert_called_once()
            listener.onItemUpdate(update)
            self.assertEqual(probe.snapshot()["rows"][0]["updates"], 2)

    def test_subscription_error_is_visible(self):
        probe = StreamProbe()
        settings = dict(server="https://example.invalid", adapter_set="x", data_adapter="y", user="", mode="MERGE",
                        snapshot="yes", frequency="1", items=["TT-123"], fields=["BidPrice"])
        with patch("lightstreamer.client.LightstreamerClient"), patch("lightstreamer.client.Subscription") as sub:
            probe.start(settings)
            listener = sub.return_value.addListener.call_args.args[0]
            listener.onSubscriptionError(21, "No permission")
            self.assertEqual(probe.snapshot()["status"], "Subscription error")
            self.assertIn("No permission", str(probe.snapshot()["events"]))
            probe.stop()

    def test_subscription_batches_cover_every_item(self):
        probe = StreamProbe()
        settings = dict(server="https://example.invalid", adapter_set="x", data_adapter="y", user="", mode="MERGE",
                        snapshot="yes", frequency="1", items=[f"TT-{i}" for i in range(601)], fields=["BidPrice"], batch_size=250)
        with patch("lightstreamer.client.LightstreamerClient") as client, patch("lightstreamer.client.Subscription") as sub:
            probe.start(settings)
            groups = [call.args[1] for call in sub.call_args_list]
            self.assertEqual([len(group) for group in groups], [250, 250, 101])
            self.assertEqual([item for group in groups for item in group], settings["items"])
            self.assertEqual(client.return_value.subscribe.call_count, 3)
            probe.stop()


if __name__ == "__main__":
    unittest.main()

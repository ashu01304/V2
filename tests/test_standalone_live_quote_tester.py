"""Offline checks: no HTTP calls, ports, credentials or live subscriptions."""
from datetime import datetime, timezone, timedelta
import unittest
from unittest.mock import MagicMock, patch

from diagnostics import standalone_live_quote_tester as tester


class StandaloneCoverageTests(unittest.TestCase):
    def tearDown(self):
        tester.stop()

    def test_requested_accepted_data_and_live_are_distinct(self):
        now = datetime.now(timezone.utc)
        with tester.lock:
            tester.state.update(items=["1-2", "2-2", "3-2", "4-2", "5-2"],
                batches={"a": {"items": ["1-2", "2-2", "3-2", "4-2"], "status": "Accepted"}},
                users={"u": {"connection": "CONNECTED:WS-STREAMING"}}, unmapped={"5-2": "No token mapping"},
                rows={
                    "1-2": {"user": "u", "snapshot": False, "live_count": 2, "last_live_utc": now.isoformat(), "tradePrice": "1.2"},
                    "2-2": {"user": "u", "snapshot": True, "live_count": 0, "bidPrice": "NaN"},
                    "3-2": {"user": "u", "snapshot": False, "live_count": 1, "last_live_utc": (now-timedelta(seconds=121)).isoformat(), "bidPrice": "1.1"},
                })
            counts, rows = tester.coverage()
        self.assertEqual(counts, {"requested": 5, "accepted": 4, "received": 3, "priced": 2, "live": 1})
        self.assertEqual(rows[-1]["status"], "No token mapping")
        tester.stop()
        self.assertEqual(tester.coverage()[0]["live"], 0)
        self.assertFalse(tester.has_price({"bidPrice": "NaN", "askPrice": "Infinity"}))

    def test_bulk_ids_are_batched_and_accepted_only_after_ack(self):
        items = [f"{i}-2" for i in range(601)]
        with patch.object(tester, "credentials", return_value={"CL": ("u", "secret")}), \
             patch.object(tester, "LightstreamerClient"), patch.object(tester, "Subscription") as subscription:
            tester.start(items, "CL")
            self.assertEqual([len(c.args[1]) for c in subscription.call_args_list], [250, 250, 101])
            self.assertEqual(tester.coverage()[0]["accepted"], 0)
            listeners = [c.args[0] for c in subscription.return_value.addListener.call_args_list]
            for listener in listeners:
                listener.onSubscription()
            self.assertEqual(tester.coverage()[0]["accepted"], 601)
            listeners[0].onSubscriptionError(1, "secret")
            self.assertEqual(tester.coverage()[0]["accepted"], 351)
            self.assertNotIn("secret", tester.refresh(0)[0])

    def test_no_product_token_does_not_block_other_products(self):
        with patch.dict(tester.catalogue, {"99-2": {"product": "UNKNOWN"}}), \
             patch.object(tester, "credentials", return_value={"CL": ("u", "secret")}), \
             patch.object(tester, "LightstreamerClient"), patch.object(tester, "Subscription") as subscription:
            tester.start(["1-2", "99-2"], "CL")
            self.assertEqual(subscription.call_args.args[1], ["1-2"])
            self.assertIn("99-2", tester.state["unmapped"])


if __name__ == "__main__":
    unittest.main()

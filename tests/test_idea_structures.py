"""Offline checks for selectable outrights, spreads, and butterfly reference prices."""

from datetime import date
import unittest
from unittest.mock import patch

import pandas as pd

import idea_dashboard as canvas


class StructureTests(unittest.TestCase):
    def setUp(self):
        self.codes = ["Z26", "F27", "G27", "H27", "J27", "K27", "M27"]
        self.prices = {"Z26": 10, "F27": 9, "G27": 7}

    def test_spreads_and_flies_use_selected_month_spacing(self):
        self.assertEqual(canvas.structure_expressions(self.codes[:5], "spread", 2),
                         ["Z26-G27", "F27-H27", "G27-J27"])
        self.assertEqual(canvas.structure_expressions(self.codes[:5], "fly", 2), ["Z26-2*G27+J27"])
        self.assertEqual(canvas.structure_expressions(self.codes[:4], "fly", 1),
                         ["Z26-2*F27+G27", "F27-2*G27+H27"])
        self.assertEqual(canvas.structure_expressions(self.codes[:2], "fly", 1), [])

    def test_fly_reference_value_and_missing_middle_leg(self):
        self.assertEqual(canvas.spread_values(self.prices, self.codes[:3], "fly"), [-1.])
        self.prices.pop("F27")
        self.assertEqual(canvas.spread_values(self.prices, self.codes[:3], "fly"), [None])

    def test_single_product_uses_its_own_expiries_and_snapshot(self):
        with patch.object(canvas, "load_expiries", return_value=[("Z26", date(2026, 11, 20))]) as expiries:
            self.assertEqual(canvas.consecutive_contracts(2, date(2026, 10, 28), product="G"), self.codes[:3])
            expiries.assert_called_once_with("G")
        now = pd.Timestamp("2026-10-28T12:00Z")
        records = [{"product": "G", "contract": "Z26", "value": 700, "bid": 699.5, "ask": 700.5,
                    "received_at": now.isoformat()}]
        with patch.object(canvas, "LiveMarketDataClient") as factory:
            factory.return_value.health.return_value = {"status": "CONNECTED:WS-STREAMING"}
            factory.return_value.snapshot.return_value = records
            reference, _ = canvas.live_prices(now, product="G")
            self.assertEqual(reference, {"Z26": 700})
            factory.return_value.snapshot.assert_called_once_with(["G"])

    def test_history_cache_and_selections_follow_product_and_structure(self):
        canvas._historical_changes.cache_clear()
        try:
            with patch.object(canvas, "MarketData") as factory:
                db = factory.return_value
                db.expression_snapshots.return_value = {}
                args = (tuple(self.codes[:3]), "2026-10-28T12:00Z", ("v1",))
                canvas._historical_changes(*args, "G", "fly", 1)
                canvas._historical_changes(*args, "NG", "fly", 1)
                self.assertEqual(db.expression_snapshots.call_count, 2)
                self.assertEqual(db.expression_snapshots.call_args_list[0].args[0],
                                 [("Z26-2*F27+G27", "G", "Z26-2*F27+G27")])
                self.assertEqual(db.expression_snapshots.call_args_list[1].args[0][0][1], "NG")
        finally:
            canvas._historical_changes.cache_clear()

    def test_callback_requests_enough_legs_and_preserves_starting_month_count(self):
        prices = {code: 10 + index for index, code in enumerate(self.codes)}
        with patch.object(canvas, "consecutive_contracts", return_value=self.codes) as contracts, \
             patch.object(canvas, "live_prices", return_value=(prices, "connected")) as live, \
             patch.object(canvas, "database_signature", return_value=None), patch.object(canvas, "set_props"):
            figure = canvas.update_chart(2, 1, 0, 0, [], "NG", "fly", 2)
        contracts.assert_called_once_with(6, product="NG")
        live.assert_called_once_with(product="NG")
        self.assertEqual(len(figure.data[0].y), 2)
        self.assertIn("NG:fly:2:", figure.layout.uirevision)
        self.assertIn("Butterfly", figure.layout.yaxis.title.text)

    def test_outright_mode_preserves_values_and_product_precision(self):
        self.assertEqual(canvas.structure_expressions(self.codes[:3], "outright", 12), self.codes[:3])
        figure = canvas.make_figure(self.codes[:3], {"Z26": .0935, "G27": .0941}, product="HO", structure="outright")
        self.assertEqual(len(figure.data), 1)
        self.assertEqual(list(figure.data[0].y), [.0935, None, .0941])
        self.assertEqual(figure.layout.yaxis.tickformat, ".4f")
        self.assertEqual(canvas.spread_values({"Z26": 3.123}, self.codes[:1], "outright", product="NG"), [3.123])

    def test_outright_callback_uses_one_leg_per_point_and_skips_unavailable_history(self):
        with patch.object(canvas, "consecutive_contracts", return_value=self.codes[:3]) as contracts, \
             patch.object(canvas, "live_prices", return_value=(self.prices, "connected")), \
             patch.object(canvas, "_historical_changes") as history, \
             patch.object(canvas, "database_signature", return_value=("v1",)), patch.object(canvas, "set_props"):
            figure = canvas.update_chart(3, 0, 0, 0, [], "CL", "outright", 12)
        contracts.assert_called_once_with(2, product="CL")
        history.assert_not_called()
        self.assertEqual(list(figure.data[0].y), [10, 9, 7])


if __name__ == "__main__":
    unittest.main()

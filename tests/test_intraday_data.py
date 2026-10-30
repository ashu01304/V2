"""Shared minute queries used by both standalone figures and the main popup."""

from tests.helpers import memory_reader
import unittest

import duckdb
import pandas as pd

from market_data.workflow_client import load_candles


class IntradayQueryTests(unittest.TestCase):
    def test_candles_align_legs_before_aggregation_and_limit_newest_bars(self):
        connection = duckdb.connect(":memory:")
        try:
            connection.execute("CREATE TABLE spread_prices(timestamp TIMESTAMPTZ, instrument VARCHAR, price DOUBLE)")
            times = pd.to_datetime([
                "2026-10-15T10:00Z", "2026-10-15T10:01Z",
                "2026-10-15T10:15Z", "2026-10-15T10:16Z",
            ], utc=True)
            rows = []
            for timestamp, cl, co in zip(times, [10, 11, 12, 13], [9, 9.5, 10, 10.5]):
                rows.extend([(timestamp.to_pydatetime(), "CLZ26-F27", cl),
                             (timestamp.to_pydatetime(), "LCOZ6-F7", co)])
            # This unmatched leg must not create an artificial high.
            rows.append((pd.Timestamp("2026-10-15T10:02Z").to_pydatetime(), "CLZ26-F27", 99))
            connection.executemany("INSERT INTO spread_prices VALUES (?, ?, ?)", rows)
            db = memory_reader(connection)
            candles = load_candles("CL-CO", "Z26-F27", 15, max_candles=2, database=db)
            self.assertEqual(candles["open"].tolist(), [1, 2])
            self.assertEqual(candles["high"].tolist(), [1.5, 2.5])
            self.assertEqual(candles["low"].tolist(), [1, 2])
            self.assertEqual(candles["close"].tolist(), [1.5, 2.5])
            latest = load_candles("CL-CO", "Z26-F27", 15, max_candles=1, database=db)
            self.assertEqual(len(latest), 1)
            self.assertEqual(latest.index[0], pd.Timestamp("2026-10-15T10:15Z"))
        finally:
            connection.close()

    def test_exact_minute_snapshots_require_all_legs_and_do_not_fill_gaps(self):
        connection = duckdb.connect(":memory:")
        reader = memory_reader(connection)
        try:
            connection.execute("CREATE TABLE spread_prices(timestamp TIMESTAMPTZ, instrument VARCHAR, price DOUBLE)")
            connection.executemany("INSERT INTO spread_prices VALUES (?, ?, ?)", [
                ("2026-10-19T12:45:00Z", "CLZ26-F27", .5),
                ("2026-10-19T12:45:00Z", "LCOZ6-F7", .2),
                ("2026-10-19T12:30:00Z", "CLZ26-F27", .4),
                ("2026-10-19T12:31:00Z", "LCOZ6-F7", .1),
                ("2026-10-19T12:00:00Z", "CLZ26-F27", .3),
                ("2026-10-19T12:00:00Z", "LCOZ6-F7", .15),
                ("2026-10-19T12:45:00Z", "CLF27-G27", .8),
                ("2026-10-19T12:45:00Z", "LCOF7-G7", float("nan")),
            ])
            targets = list(pd.to_datetime(["2026-10-19T12:45Z", "2026-10-19T12:30Z", "2026-10-19T12:00Z"], utc=True))
            result = reader.expression_snapshots([
                ("first", "CL-CO", "Z26-F27"), ("second", "CL-CO", "F27-G27")], [*targets, targets[0]])
            self.assertEqual(len(result), 3)
            self.assertAlmostEqual(result[targets[0]]["first"], .3)
            self.assertEqual(result[targets[1]], {})
            self.assertAlmostEqual(result[targets[2]]["first"], .15)
            self.assertNotIn("second", result[targets[0]])
            self.assertEqual(reader.expression_snapshots([], targets), {target: {} for target in targets})
            with self.assertRaisesRegex(ValueError, "timezone-aware"):
                reader.expression_snapshots([], ["2026-10-19T12:45:00"])
            with self.assertRaisesRegex(ValueError, "whole minutes"):
                reader.expression_snapshots([], ["2026-10-19T12:45:10Z"])
        finally:
            reader.close()


class IdeaLivePlotTests(unittest.TestCase):
    def setUp(self):
        import idea_dashboard
        self.canvas = idea_dashboard

    def test_single_live_line_keeps_month_positions_rounding_and_zoom(self):
        figure = self.canvas.make_figure(["Z26", "F27", "G27", "H27"],
                                        {"Z26-F27": {"value": .133,"bid": .13,"ask": .15}})
        self.assertEqual(len(figure.data), 2)
        self.assertEqual(figure.data[0].name, "Best bid")
        self.assertEqual(list(figure.data[0].y), [.13, None, None])
        self.assertEqual(list(figure.data[0].x), [0, 1, 2])
        self.assertEqual(list(figure.layout.xaxis.ticktext),
                         ["Dec 26-Jan 27", "Jan 27-Feb 27", "Feb 27-Mar 27"])
        self.assertFalse(figure.data[0].connectgaps)
        self.assertTrue(figure.layout.showlegend)
        self.assertEqual(figure.layout.dragmode, "zoom")
        self.assertEqual(figure.layout.uirevision, "CL-CO:spread:1:Z26:F27:G27:H27")
        self.assertEqual(figure.layout.yaxis.tickformat, ".2f")

    def test_missing_live_prices_show_an_empty_plot_message(self):
        for contracts in ([], ["Z26", "F27"]):
            figure = self.canvas.make_figure(contracts, {})
            self.assertEqual(len(figure.data), 2)
            self.assertEqual(figure.layout.annotations[0].text, "No matching bid/ask quotes available")

    def test_month_selection_skips_calendar_months_across_year_boundary(self):
        from datetime import date
        from unittest.mock import patch
        expiry = [("Z26", date(2026, 11, 20)), ("F27", date(2026, 12, 20))]
        with patch.object(self.canvas, "load_expiries", return_value=expiry):
            self.assertEqual(self.canvas.consecutive_contracts(2, date(2026, 10, 26), 1),
                             ["F27", "G27", "H27"])

    def test_live_reference_values_supply_plot_from_complete_quote(self):
        from unittest.mock import patch
        with patch.object(self.canvas, "LiveMarketDataClient") as factory:
            client = factory.return_value
            client.quotes.return_value = {"Z26-F27": {"value": .1,"bid": .09,"ask": .11}}
            reference, _ = self.canvas.live_prices(expressions=["Z26-F27"], with_sides=True)
            figure = self.canvas.make_figure(["Z26", "F27"], reference)
            self.assertEqual(list(figure.data[0].y), [.09])
            client.quotes.assert_called_once_with("CL-CO", ["Z26-F27"], best_sides=True)
            client.snapshot.assert_not_called()
            client.close.assert_called_once()

    def test_missing_resolved_price_leaves_curve_gap(self):
        from unittest.mock import patch
        with patch.object(self.canvas, "LiveMarketDataClient") as factory:
            factory.return_value.quotes.return_value = {"Z26-F27": {"value": None, "error": "No fresh quoted components"}}
            reference, _ = self.canvas.live_prices(expressions=["Z26-F27"], with_sides=True)
            figure = self.canvas.make_figure(["Z26", "F27"], reference)
            self.assertEqual(list(figure.data[0].y), [None])

    def test_directional_history_is_batched_and_cached_for_all_four_times(self):
        from unittest.mock import Mock, patch
        canvas = self.canvas
        db = Mock()
        db.expression_snapshots.side_effect = lambda selections, targets: {target: {"Z26-F27": .08} for target in targets}
        canvas._historical_changes.cache_clear()
        try:
            with patch.object(canvas, "MarketData", return_value=db):
                first = canvas._historical_changes(("Z26", "F27", "G27"), "2026-10-26T12:00:00+00:00", ("v1",))
                self.assertIs(first, canvas._historical_changes(("Z26", "F27", "G27"), "2026-10-26T12:00:00+00:00", ("v1",)))
                self.assertEqual(set(first), {15, 30, 60, 120})
                self.assertEqual(first[15], [.08, None])
                targets = db.expression_snapshots.call_args.args[1]
                self.assertEqual(targets[-1], pd.Timestamp("2026-10-26T10:00Z"))
                db.expression_snapshots.assert_called_once()
                db.close.assert_called_once()
        finally:
            canvas._historical_changes.cache_clear()

    def test_changes_round_prices_and_leave_missing_data_blank(self):
        from unittest.mock import patch
        canvas = self.canvas
        with patch.object(canvas, "database_signature", return_value=("v1",)), \
             patch.object(canvas, "_historical_changes", return_value={15: [.084, None], 30: [.12, .1]}):
            rows = canvas.change_rows(["Z26", "F27", "G27"], {"Z26-F27": .1})
        self.assertEqual(rows[0]["change-15"], .02)
        self.assertEqual(rows[0]["change-30"], -.02)
        self.assertIsNone(rows[0]["change-120"])
        self.assertIsNone(rows[1]["change-30"])

    def test_direction_boxes_require_strict_majority(self):
        canvas = self.canvas
        rows = [{"change-15": .01, "change-30": -.01, "change-60": .01, "change-120": None},
                {"change-15": .01, "change-30": -.01, "change-60": -.01, "change-120": None},
                {"change-15": -1., "change-30": 1., "change-60": None, "change-120": None}]
        boxes = canvas.trend_boxes(rows)
        self.assertEqual([box.children[1].children for box in boxes], ["Up", "Down", "Mixed", "No data"])
        self.assertEqual(boxes[0].children[2].children, "2 contracts in the same direction")

    def test_simple_dropdown_fits_selected_curves_with_rounded_points(self):
        canvas = self.canvas
        codes = ["Z26", "F27", "G27", "H27", "J27", "K27"]
        prices = {f"{first}-{second}": {"bid": float(2*index+1)-.01, "ask": float(2*index+1)+.01}
                  for index, (first, second) in enumerate(zip(codes, codes[1:]))}
        for curve in canvas.FIT_OPTIONS:
            figure = canvas.make_figure(codes, prices, [curve])
            self.assertEqual(len(figure.data), 3)
            self.assertEqual(figure.data[2].name, canvas.FIT_OPTIONS[curve])
            self.assertTrue(all(value == round(value, 2) for value in figure.data[2].y))
        figure = canvas.make_figure(codes, prices, ["linear", "quadratic", "linear"])
        self.assertEqual(len(figure.data), 4)
        quadratic = figure.data[3]
        self.assertAlmostEqual(quadratic.y[0], 1.)
        self.assertAlmostEqual(quadratic.y[-1], 9.)
        short = canvas.make_figure(codes[:2], prices, ["cubic", "spline"])
        self.assertEqual(len(short.data), 2)
        self.assertEqual(len(short.layout.annotations), 2)

    def test_dashboard_callback_has_only_live_plot_controls(self):
        from unittest.mock import patch
        canvas = self.canvas
        inputs = [("idea-months", "value", 2), ("idea-skip-months", "value", 3),
                  ("idea-refresh", "n_clicks", 0), ("idea-tick", "n_intervals", 1), ("idea-curves", "value", []),
                  ("idea-product", "value", "CL-CO"), ("idea-structure", "value", "spread"), ("idea-gap", "value", 1)]
        with patch.object(canvas, "consecutive_contracts", return_value=["Z26", "F27", "G27"]) as contracts, \
             patch.object(canvas, "live_prices", return_value=({"Z26": .3, "F27": .2, "G27": .1}, "connected")) as live, \
             patch.object(canvas, "database_signature", return_value=None):
            client = canvas.app.server.test_client()
            response = client.post("/_dash-update-component", json={
                "output": "idea-chart.figure", "outputs": {"id": "idea-chart", "property": "figure"},
                "inputs": [{"id": key, "property": prop, "value": value} for key, prop, value in inputs],
                "state": [], "changedPropIds": ["idea-tick.n_intervals"]})
            self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
            contracts.assert_called_once_with(5, product="CL-CO")
            live.assert_called_once_with(product="CL-CO", expressions=["Z26-F27", "F27-G27"], with_sides=True)
            self.assertEqual(len(response.get_json()["response"]["idea-chart"]["figure"]["data"]), 2)
            self.assertEqual(len(response.get_json()["sideUpdate"]["idea-changes-table"]["data"]), 2)
            self.assertEqual(len(response.get_json()["sideUpdate"]["idea-trend-boxes"]["children"]), 4)
            self.assertEqual(set(canvas.app.callback_map), {"idea-chart.figure"})
            layout = client.get("/_dash-layout")
            self.assertEqual(layout.status_code, 200)
            text = layout.get_data(as_text=True)
            for removed in ("idea-families", "idea-errors", "idea-entries-table"):
                self.assertNotIn(removed, text)
            self.assertIn('"persistence_type":"local"', text)
            self.assertIn('"scrollZoom":false', text)


if __name__ == "__main__":
    unittest.main()

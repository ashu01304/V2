"""Trading-session alignment, gap handling and zero-base regression checks."""

from tests.helpers import memory_reader
import unittest
from unittest.mock import patch

import duckdb
import numpy as np
import pandas as pd

from market_data.intraday import load_session_minutes, session_window
from analysis.session_overlay import (
    make_session_overlay, session_lines,
)


class SessionOverlayTests(unittest.TestCase):
    def setUp(self):
        self.index = pd.to_datetime([
            "2026-10-13T22:00Z", "2026-10-13T22:01Z", "2026-10-13T22:03Z",
            "2026-10-14T21:00Z", "2026-10-14T22:00Z", "2026-10-14T22:01Z",
        ], utc=True)
        self.minutes = pd.DataFrame({"price": [7, 7.5, 8, 8.5, 9, 9.5]}, index=self.index)

    def test_sessions_cross_midnight_and_exclude_maintenance_hour(self):
        lines = session_lines(self.minutes)
        self.assertEqual([day.date().isoformat() for day, *_ in lines],
                         ["2026-10-14", "2026-10-15"])
        self.assertEqual(lines[0][1].notna().sum(), 3)
        self.assertTrue(np.isnan(lines[0][1].iloc[2]))

    def test_zero_base_uses_each_sessions_first_actual_observation(self):
        minutes = self.minutes.iloc[[1, 2, 4, 5]]
        zero = make_session_overlay(minutes, "CL-CO", "Z26-F27", True)
        level = make_session_overlay(minutes, "CL-CO", "Z26-F27", False)
        for trace in zero.data:
            values = np.asarray(trace.y, dtype=float)
            self.assertEqual(values[np.isfinite(values)][0], 0)
            self.assertTrue(trace.connectgaps)
        self.assertEqual(level.data[0].y[1], 7.5)
        self.assertTrue(np.isnan(zero.data[0].y[0]))
        self.assertTrue(np.isnan(zero.data[0].y[2]))
        self.assertEqual(zero.layout.xaxis.ticktext[0], "17:00")
        self.assertEqual(zero.layout.xaxis.ticktext[-1], "16:00")

    def test_live_dot_uses_matching_session_baseline_and_local_time(self):
        quote = {"timestamp": "2026-10-14T22:02:30Z", "value": 10}
        zero = make_session_overlay(self.minutes, "CL", "Z26", live_quote=quote)
        level = make_session_overlay(self.minutes, "CL", "Z26", False, live_quote=quote)
        self.assertEqual(zero.data[-1].x[0], 2.5)
        self.assertEqual(zero.data[-1].y[0], 1)
        self.assertEqual(level.data[-1].y[0], 10)
        for invalid in [
            {"timestamp": "2026-10-15T22:02Z", "value": 10},
            {"timestamp": "2026-10-14T21:30Z", "value": 10},
            {"timestamp": "invalid", "value": 10},
            {"timestamp": "2026-10-14T22:02Z", "value": float("nan")},
        ]:
            self.assertEqual(len(make_session_overlay(
                self.minutes, "CL", "Z26", live_quote=invalid).data), 2)

    def test_actual_points_and_live_dot_follow_product_precision_without_changing_source(self):
        minutes = pd.DataFrame(
            {"price": [-.0144, -.0258, -.0356]},
            index=pd.date_range("2026-10-14T22:00Z", periods=3, freq="min"),
        )
        original = minutes.copy(deep=True)
        quote = {"timestamp": "2026-10-14T22:02:30Z", "value": -.0152}
        for product in ["CL", "CO", "CL-CO", "NG", "OTHER"]:
            decimals = 2 if product in {"CL", "CO", "CL-CO"} else 3
            expected = minutes.price.round(decimals).to_numpy()
            level = make_session_overlay(minutes, product, "Z26-F27", False, live_quote=quote)
            zero = make_session_overlay(minutes, product, "Z26-F27", True, live_quote=quote)
            np.testing.assert_allclose(level.data[0].y[:3], expected)
            np.testing.assert_allclose(zero.data[0].y[:3], np.round(expected - expected[0], decimals))
            live = float(np.round(quote["value"], decimals))
            self.assertEqual(level.data[-1].y[0], live)
            self.assertAlmostEqual(zero.data[-1].y[0], float(np.round(live - expected[0], decimals)))
            self.assertEqual(level.data[0].customdata[0][1], expected[0])
            self.assertTrue(np.isnan(level.data[0].y[3]))
        pd.testing.assert_frame_equal(minutes, original)
        self.assertEqual(quote["value"], -.0152)

    def test_session_trace_ids_are_stable_unique_and_safe_for_plotly_selectors(self):
        first = make_session_overlay(self.minutes, "CL-CO", "Z26-F27")
        repeated = make_session_overlay(self.minutes, "CL-CO", "Z26-F27")
        other = make_session_overlay(self.minutes, "CL-CO", "Z26-2*F27+G27")
        ids = [trace.uid for trace in first.data]
        self.assertEqual(ids, [trace.uid for trace in repeated.data])
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(set(ids).isdisjoint(trace.uid for trace in other.data))
        for trace in [*first.data, *other.data]:
            self.assertRegex(trace.uid, r"^[A-Za-z][A-Za-z0-9_-]*$")

    def test_daylight_saving_keeps_local_open_at_zero(self):
        minutes = pd.DataFrame({"price": [1, 2]}, index=pd.to_datetime([
            "2026-10-29T22:00Z", "2026-11-01T23:00Z",
        ], utc=True))
        lines = session_lines(minutes)
        self.assertEqual(len(lines), 2)
        self.assertTrue(all(line[3] == 0 for line in lines))

    def test_weekend_sessions_are_excluded(self):
        minutes = pd.DataFrame({"price": [1, 2]}, index=pd.to_datetime([
            "2026-10-16T22:00Z", "2026-10-18T22:00Z",
        ], utc=True))
        lines = session_lines(minutes)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0][0].date().isoformat(), "2026-10-19")

    def test_calendar_window_and_invalid_day_counts(self):
        start, end = session_window(2, pd.Timestamp("2026-10-15T12:00Z"))
        self.assertEqual(start, pd.Timestamp("2026-10-13T22:00Z"))
        for invalid in [None, 0, -1, 1.5]:
            with self.assertRaises(ValueError):
                session_window(invalid)
        with self.assertRaises(ValueError):
            session_window(2, pd.Timestamp("2026-10-15"))

    def test_loader_aligns_spread_legs_inside_requested_window(self):
        connection = duckdb.connect(":memory:")
        try:
            connection.execute("CREATE TABLE spread_prices(timestamp TIMESTAMPTZ, instrument VARCHAR, price DOUBLE)")
            rows = []
            for index, timestamp in enumerate(self.index):
                rows.extend([(timestamp.to_pydatetime(), "CLZ26-F27", 10 + index),
                             (timestamp.to_pydatetime(), "LCOZ6-F7", 3 + index * .5)])
            connection.executemany("INSERT INTO spread_prices VALUES (?, ?, ?)", rows)
            # A leg without its counterpart must not create a synthetic price.
            connection.execute("INSERT INTO spread_prices VALUES (?, ?, ?)",
                               [pd.Timestamp("2026-10-14T22:02Z").to_pydatetime(), "CLZ26-F27", 99])
            result = load_session_minutes(
                "CL-CO", "Z26-F27", 2, now=pd.Timestamp("2026-10-15T12:00Z"),
                database=memory_reader(connection),
            )
            np.testing.assert_allclose(result.price, self.minutes.price)
            self.assertEqual(len(result), len(self.minutes))
        finally:
            connection.close()

    def test_integrated_overlay_defers_requests_and_reuses_data_for_zero_base(self):
        with patch("market_data.queries.MarketData.__init__",
                   side_effect=AssertionError("Unexpected database request")):
            from dashboard_components import minute_charts as charts
            self.assertIsNone(charts.load_overlay_data(None, 16))
            raw = {
                "selection": {"symbol": "CL-CO", "contract": "Z26", "strategy": "1MS",
                              "expression": "Z26-F27"},
                "past_days": 16,
                "timestamps": [t.isoformat() for t in self.minutes.index],
                "prices": self.minutes.price.tolist(),
            }
            with patch.object(charts, "load_session_minutes",
                              side_effect=AssertionError("Zero-base requested data")):
                self.assertEqual(len(charts.update_overlay_chart(raw, []).data), 2)
                self.assertEqual(len(charts.update_overlay_chart(raw, ["yes"]).data), 2)


if __name__ == "__main__":
    unittest.main()

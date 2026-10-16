"""Integration checks for the main popup's on-demand minute charts."""

import ast
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

import pandas as pd
from dash import Dash, dcc, html, no_update

from dashboard_components import minute_charts as charts


class PopupMinuteChartTests(unittest.TestCase):
    def setUp(self):
        self.selection = {"symbol": "CL-CO", "expression": "Z26-F27",
                          "contract": "Z26", "strategy": "1MS"}
        self.minutes = pd.DataFrame({"price": [1.0, 1.1, 1.2]},
                                    index=pd.date_range("2026-10-14T22:00Z", periods=3, freq="min"))
        self.candles = pd.DataFrame({"open": [1], "high": [1.2], "low": [1], "close": [1.2]},
                                    index=self.minutes.index[:1])
        self.app = Dash(__name__)
        source = ast.parse(Path("dashboard.py").read_text(encoding="utf-8"))
        control = next(n for n in source.body if isinstance(n, ast.FunctionDef) and n.name == "control")
        layout = next(n for n in source.body if isinstance(n, ast.Assign) and any(
            isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name)
            and t.value.id == "app" and t.attr == "layout" for t in n.targets))
        namespace = {"app": self.app, "html": html, "dcc": dcc,
                     "products": ["CL", "CO", "CL-CO", "NG"], "columns": ["1MS", "1MF"],
                     "minute_charts_layout": charts.minute_charts_layout}
        exec(compile(ast.Module(body=[control, layout], type_ignores=[]), "dashboard.py", "exec"), namespace)
        charts.register_minute_chart_callbacks(self.app)
        self.app.server.config["TESTING"] = True

    def request(self, output, inputs, changed):
        if output in {"popup-candle-chart.figure", "popup-overlay-data.data"}:
            if not any(name == "popup-minute-publication" for name, _, _ in inputs):
                inputs = [*inputs, ("popup-minute-publication", "data", None)]
        component, prop = output.rsplit(".", 1)
        with self.app.server.test_client() as client:
            return client.post("/_dash-update-component", json={
                "output": output, "outputs": {"id": component, "property": prop},
                "inputs": [{"id": name, "property": field, "value": value}
                           for name, field, value in inputs],
                "state": [], "changedPropIds": [changed],
            })

    def test_layout_places_the_two_charts_after_existing_plots(self):
        modal = next(child for child in self.app.layout.children
                     if getattr(child, "id", None) == "chart-modal")
        panel = modal.children[1]
        self.assertEqual(panel.children[-1].id, "popup-minute-charts")
        self.assertEqual([graph.id for graph in panel.children[-2].children],
                         ["rollover-chart", "forward-curve-chart"])
        row = panel.children[-1]
        self.assertEqual(len(row.children), 2)
        self.assertIsInstance(row.children[0].children[1], html.Div)
        self.assertIsInstance(row.children[1].children[1], html.Div)
        self.assertEqual(row.children[0].children[1].children.id, "popup-candle-chart")
        overlay_loading = row.children[1].children[1]
        self.assertEqual(overlay_loading.children[1].id, "popup-overlay-chart")
        for graph in [row.children[0].children[1].children, overlay_loading.children[1]]:
            self.assertFalse(graph.config["scrollZoom"])

    def test_no_fetch_without_a_selected_cell_including_control_changes(self):
        with patch.object(charts, "load_candles") as candles, \
             patch.object(charts, "load_session_minutes") as minutes, \
             patch("market_data.live_client.LiveMarketDataClient") as live:
            response = self.request("popup-candle-chart.figure", [
                ("selected-cell", "data", None), ("popup-candle-timeframe", "value", 30),
            ], "popup-candle-timeframe.value")
            self.assertIn(response.status_code, (200, 204))
            if response.status_code == 200:
                self.assertEqual(response.get_json()["response"], {})
            response = self.request("popup-overlay-data.data", [
                ("selected-cell", "data", None), ("popup-overlay-days", "value", 8),
            ], "selected-cell.data")
            self.assertEqual(response.status_code, 200)
            self.assertIsNone(response.get_json()["response"]["popup-overlay-data"]["data"])
            self.assertIs(charts.update_overlay_chart(None, ["yes"]), no_update)
            candles.assert_not_called()
            minutes.assert_not_called()
            live.assert_not_called()

    def test_candle_click_loads_only_selected_expression(self):
        client = Mock()
        client.expression.return_value = None
        with patch.object(charts, "load_candles", return_value=self.candles) as candles, \
             patch.object(charts, "load_session_minutes") as minutes, \
             patch("market_data.live_client.LiveMarketDataClient", return_value=client):
            response = self.request("popup-candle-chart.figure", [
                ("selected-cell", "data", self.selection), ("popup-candle-timeframe", "value", 15),
            ], "selected-cell.data")
            self.assertEqual(response.status_code, 200)
            figure = response.get_json()["response"]["popup-candle-chart"]["figure"]
            self.assertEqual(figure["data"][0]["type"], "candlestick")
            self.assertFalse(figure["layout"]["showlegend"])
            candles.assert_called_once_with("CL-CO", "Z26-F27", 15)
            minutes.assert_not_called()
            client.close.assert_called_once()

    def test_overlay_day_count_loads_only_overlay_and_zero_base_reuses_minutes(self):
        with patch.object(charts, "load_session_minutes", return_value=self.minutes) as minutes, \
             patch.object(charts, "load_candles") as candles, \
             patch.object(charts, "fetch_live_quote", return_value={
                 "timestamp": "2026-10-14T22:02:30Z", "value": 1.3}) as live:
            response = self.request("popup-overlay-data.data", [
                ("selected-cell", "data", self.selection), ("popup-overlay-days", "value", 8),
            ], "popup-overlay-days.value")
            self.assertEqual(response.status_code, 200)
            data = response.get_json()["response"]["popup-overlay-data"]["data"]
            minutes.assert_called_once_with("CL-CO", "Z26-F27", 8)
            candles.assert_not_called()
            live.assert_called_once_with("CL-CO", "Z26-F27")
        with patch.object(charts, "load_session_minutes") as minutes, \
             patch.object(charts, "load_candles") as candles:
            zero = charts.update_overlay_chart(data, ["yes"])
            level = charts.update_overlay_chart(data, [])
            self.assertEqual(zero.data[0].y[0], 0)
            self.assertEqual(level.data[0].y[0], 1)
            self.assertAlmostEqual(zero.data[-1].y[0], 0.3)
            self.assertAlmostEqual(level.data[-1].y[0], 1.3)
            self.assertEqual(zero.data[-1].marker.color, "#22c55e")
            self.assertFalse(zero.data[-1].showlegend)
            self.assertEqual(zero.data[0].name, "15-10")
            self.assertEqual(zero.layout.legend.orientation, "h")
            self.assertIsNone(zero.layout.xaxis.title.text)
            self.assertIsNone(zero.layout.yaxis.title.text)
            self.assertTrue(zero.data[0].connectgaps)
            candles.assert_not_called()
            minutes.assert_not_called()

    def test_candle_prices_and_refreshing_live_dots_use_product_precision(self):
        candles = pd.DataFrame(
            {"open": [1.23456], "high": [1.23986], "low": [1.22123], "close": [1.23876]},
            index=self.minutes.index[:1],
        )
        original = candles.copy(deep=True)
        quote = {"timestamp": "2026-10-14T22:02:30Z", "value": 1.23766}
        for product, decimals in [("CL-CO", 2), ("NG", 3), ("OTHER", 3)]:
            figure = charts.make_intraday_figure(candles, product, "Z26-F27", live_quote=quote)
            for field in ["open", "high", "low", "close"]:
                self.assertAlmostEqual(getattr(figure.data[0], field)[0],
                                       candles[field].round(decimals).iloc[0])
            self.assertAlmostEqual(figure.data[-1].y[0], round(quote["value"], decimals))
        pd.testing.assert_frame_equal(candles, original)
        data = {"selection": self.selection, "past_days": 8,
                "timestamps": [t.isoformat() for t in self.minutes.index],
                "prices": self.minutes.price.tolist()}
        overlay = charts.update_overlay_chart(data, ["yes"]).to_plotly_json()
        with patch.object(charts, "fetch_live_quote", return_value=quote):
            payload, _ = charts.refresh_minute_live_markers(
                1, self.selection, data, ["yes"], {"data": [{"type": "candlestick"}]}, overlay)
        self.assertEqual(payload["candle"]["y"], [1.24])
        self.assertEqual(payload["overlay"]["y"], [.24])

    def test_live_timer_returns_only_marker_data_without_history_queries(self):
        data = {"selection": self.selection, "timestamps": [t.isoformat() for t in self.minutes.index],
                "prices": self.minutes.price.tolist(), "past_days": 8}
        candle = {"data": [{"type": "candlestick"}, {"name": "Live price", "x": [], "y": []}]}
        overlay = charts.update_overlay_chart(data, ["yes"]).to_plotly_json()
        with patch.object(charts, "fetch_live_quote", return_value={
                "timestamp": "2026-10-14T22:02:30Z", "value": 1.3}) as live, \
             patch.object(charts, "load_candles") as candles, \
             patch.object(charts, "load_session_minutes") as minutes:
            payload, publication = charts.refresh_minute_live_markers(
                1, self.selection, data, ["yes"], candle, overlay)
            self.assertEqual(payload["candle"]["y"], [1.3])
            self.assertEqual(payload["overlay"]["marker"]["color"], "#22c55e")
            self.assertAlmostEqual(payload["overlay"]["y"][0], .3)
            self.assertEqual(payload["overlay"]["x"], [2.5])
            self.assertTrue(payload["zero_base"])
            self.assertNotIn("layout", payload)
            live.assert_called_once_with("CL-CO", "Z26-F27")
            candles.assert_not_called()
            minutes.assert_not_called()
        with patch.object(charts, "fetch_live_quote") as live:
            self.assertEqual(charts.refresh_minute_live_markers(2, None, data, [], {}, {}),
                             (None, no_update))
            live.assert_not_called()

    def test_unavailable_quote_clears_dot_and_later_quote_restores_it(self):
        data = {"selection": self.selection, "timestamps": [t.isoformat() for t in self.minutes.index],
                "prices": self.minutes.price.tolist(), "past_days": 8}
        figure = charts.update_overlay_chart(data, ["yes"]).to_plotly_json()
        with patch.object(charts, "fetch_live_quote", return_value=None):
            payload, _ = charts.refresh_minute_live_markers(
                1, self.selection, data, ["yes"], {}, figure)
        self.assertEqual(payload["overlay"]["x"], [])
        with patch.object(charts, "fetch_live_quote", return_value={
                "timestamp": "2026-10-14T22:02:30Z", "value": 1.4}):
            payload, _ = charts.refresh_minute_live_markers(
                2, self.selection, data, [], {}, figure)
        self.assertEqual(payload["overlay"]["y"], [1.4])

    def test_browser_round_trip_does_not_trigger_false_publications(self):
        import json
        stat = Mock(st_mtime_ns=1792082335894253001, st_size=1237594112,
                    st_ino=987654321012345678)
        with patch("market_data.queries.DATABASE_PATH") as path:
            path.stat.return_value = stat
            signature = charts.database_signature()
        # Simulate JavaScript Number rounding when a JSON integer exceeds 2**53.
        browser_signature = json.loads(json.dumps(signature),
                                       parse_int=lambda value: int(float(value)))
        self.assertEqual(browser_signature, signature)
        self.assertTrue(all(isinstance(value, str) for value in signature))
        data = {"selection": self.selection, "past_days": 8, "database_signature": browser_signature,
                "timestamps": [t.isoformat() for t in self.minutes.index],
                "prices": self.minutes.price.tolist()}
        candle = {"layout": {"meta": {"selection": self.selection,
                                     "minute_database_signature": browser_signature}}}
        with patch.object(charts, "database_signature", return_value=signature), \
             patch.object(charts, "fetch_live_quote", return_value=None), \
             patch.object(charts, "load_candles") as candles, \
             patch.object(charts, "load_session_minutes") as minutes:
            for tick in range(5):
                result = charts.refresh_minute_live_markers(
                    tick, self.selection, data, ["yes"], candle, {})
                self.assertIs(result[1], no_update)
            candles.assert_not_called()
            minutes.assert_not_called()

    def test_publication_detection_reloads_history_only_after_database_changes(self):
        data = {"selection": self.selection, "past_days": 8, "database_signature": [1, 2, 3],
                "timestamps": [t.isoformat() for t in self.minutes.index],
                "prices": self.minutes.price.tolist()}
        with patch.object(charts, "database_signature", return_value=[1, 2, 3]), \
             patch.object(charts, "fetch_live_quote", return_value=None), \
             patch.object(charts, "load_candles") as candles, \
             patch.object(charts, "load_session_minutes") as minutes:
            self.assertIs(charts.refresh_minute_live_markers(
                1, self.selection, data, [], {}, {})[1], no_update)
            candles.assert_not_called()
            minutes.assert_not_called()
        with patch.object(charts, "database_signature", return_value=[4, 5, 6]), \
             patch.object(charts, "fetch_live_quote", return_value=None):
            self.assertEqual(charts.refresh_minute_live_markers(
                2, self.selection, data, [], {}, {})[1], [4, 5, 6])
        with patch.object(charts, "load_session_minutes", return_value=self.minutes) as minutes, \
             patch.object(charts, "fetch_live_quote", return_value=None):
            response = self.request("popup-overlay-data.data", [
                ("selected-cell", "data", self.selection), ("popup-overlay-days", "value", 8),
                ("popup-minute-publication", "data", [4, 5, 6]),
            ], "popup-minute-publication.data")
            self.assertEqual(response.status_code, 200)
            minutes.assert_called_once_with("CL-CO", "Z26-F27", 8)

    def test_live_refresh_dispatch_has_all_states_and_store_outputs(self):
        callback_key = next(name for name in self.app.callback_map if name.startswith(".."))
        state = [
            ("selected-cell", "data", None), ("popup-overlay-data", "data", None),
            ("popup-overlay-zero-base", "value", ["yes"]),
            ("popup-candle-chart", "figure", {}), ("popup-overlay-chart", "figure", {}),
        ]
        with self.app.server.test_client() as client:
            response = client.post("/_dash-update-component", json={
                "output": callback_key, "outputs": [
                    {"id": "popup-minute-live", "property": "data"},
                    {"id": "popup-minute-publication", "property": "data"},
                ],
                "inputs": [{"id": "live-chart-refresh", "property": "n_intervals", "value": 1}],
                "state": [{"id": name, "property": prop, "value": value} for name, prop, value in state],
                "changedPropIds": ["live-chart-refresh.n_intervals"],
            })
        self.assertIn(response.status_code, (200, 204))
        if response.status_code == 200:
            self.assertIsNone(response.get_json()["response"]["popup-minute-live"]["data"])

    def test_only_marker_callback_uses_timer_and_history_stays_on_demand(self):
        self.assertEqual(len(self.app.callback_map), 5)
        timers = []
        for name, callback in self.app.callback_map.items():
            if any(i["property"] == "n_intervals" for i in callback["inputs"]):
                timers.append(callback)
            else:
                self.assertFalse(any(i["id"] == "data-version" for i in callback["inputs"]))
        self.assertEqual(len(timers), 1)
        self.assertEqual([o.component_id for o in timers[0]["output"]],
                         ["popup-minute-live", "popup-minute-publication"])
        self.assertEqual(timers[0]["inputs"], [{"id": "live-chart-refresh", "property": "n_intervals"}])


if __name__ == "__main__":
    unittest.main()

"""Conditional histograms align minute timestamps and contract years before pooling."""

import unittest
from unittest.mock import patch

import duckdb
import pandas as pd

import idea_dashboard_2 as canvas
from market_data.contracts import price_recipe
from tests.helpers import memory_reader


class ConditionalHistogramTests(unittest.TestCase):
    def test_central_95_trims_by_observation_frequency_and_keeps_live_dot(self):
        frame = pd.DataFrame({"month": [1]*4, "price": [-1., .1, .2, 2.], "frequency": [1, 49, 49, 1]})
        selected = canvas.central_95_prices(frame)
        self.assertEqual(selected.price.tolist(), [.1, .2])
        self.assertEqual(selected.frequency.sum(), 98)
        self.assertEqual(len(frame), 4)
        row = {"month": 1, "live": 200, "live_asof": pd.Timestamp.now(tz="UTC").isoformat()}
        chart = canvas.histogram_cards(frame, [row])[0].figure
        self.assertEqual(list(chart.data[0].x), [10, 20])
        self.assertEqual(list(chart.data[1].x), [200])
        self.assertIn("central 95%", chart.layout.title.text)
        single = frame.iloc[:1]
        self.assertEqual(len(canvas.central_95_prices(single)), 1)
        self.assertTrue(canvas.central_95_prices(frame.iloc[:0]).empty)

    def test_removed_conditional_callbacks_are_not_registered(self):
        self.assertFalse(any(item['inputs'] and item['inputs'][0]['id'] == 'filter-run'
                             for item in canvas.app.callback_map.values()))

    def test_results_cache_tracks_ranges_publication_and_reference_minute(self):
        frame = pd.DataFrame(columns=["month", "price", "frequency"])
        with patch.object(canvas, "MarketData") as factory:
            factory.return_value.calendar_filter_histograms.return_value = (frame, {"samples": 0, "minutes": 0, "years": 0})
            args = (((1, (.09, .15)),), 30, "2026-10-26T12:00:00+00:00", ("version1",))
            canvas.histogram_results(*args)
            canvas.histogram_results(*args)
            self.assertEqual(factory.call_count, 1)
            canvas.histogram_results(((1, (.1, .16)),), *args[1:])
            canvas.histogram_results(*args[:3], ("version2",))
            canvas.histogram_results(*args[:2], "2026-10-26T12:01:00+00:00", args[3])
            self.assertEqual(factory.call_count, 4)
            self.assertEqual(factory.return_value.close.call_count, 4)

    def test_skip_removes_front_months_and_preserves_remaining_k_values(self):
        pairs = [{"month": month, "pair": canvas.PAIRS[month], "contract": str(month)} for month in [12, *range(1, 12)]]
        with patch.object(canvas, "rolling_pairs", return_value=pairs):
            self.assertEqual([row["month"] for row in canvas.visible_pairs()], [pair["month"] for pair in pairs[3:]])
            with patch.object(canvas, "live_month_ticks", return_value=({}, "offline")):
                rows, _ = canvas.refresh_live(0, 3, [{**pair, "k": pair["month"]} for pair in pairs])
            self.assertEqual([row["month"] for row in rows], [pair["month"] for pair in pairs[3:]])
            self.assertEqual(rows[0]["k"], 3)
            self.assertFalse(any("minimum" in row or "maximum" in row for row in rows))
            frame = pd.DataFrame(columns=["month", "price", "frequency"])
            cards = canvas.histogram_cards(frame)
            self.assertEqual(len(cards), 8)
            self.assertEqual([card.id["month"] for card in cards], [pair["month"] for pair in pairs[3:11]])
            hidden = {**pairs[0], "k": -1}
            with patch.object(canvas, "MarketData") as factory:
                factory.return_value.calendar_filter_histograms.return_value = (frame, {"samples": 0, "minutes": 0, "years": 0})
                _, status = canvas.apply_filters(1, [hidden], 30, 3)
                self.assertEqual(factory.return_value.calendar_filter_histograms.call_args.args[0], {})
                self.assertIn("0 active filters", status)

    def test_live_dot_uses_tick_price_without_implying_a_frequency(self):
        now = pd.Timestamp.now(tz="UTC")
        row = {"month": 1, "contract": "Jan 27-Feb 27", "live": -12, "live_asof": now.isoformat()}
        frame = pd.DataFrame({"month": [1], "price": [.03], "frequency": [4]})
        figure = canvas.histogram_cards(frame, [row])[0].figure
        self.assertEqual(len(figure.data), 2)
        self.assertEqual(list(figure.data[1].x), [-12])
        self.assertEqual(figure.data[1].yaxis, "y2")
        self.assertEqual(figure.data[1].marker.color, "#22c55e")
        with patch.object(canvas, "MarketData") as database:
            update = canvas.refresh_histogram_live([row], {"month": 1})
            database.assert_not_called()
        ops = update.to_plotly_json()["operations"]
        self.assertEqual([op["location"] for op in ops], [["data", 1, "x"], ["data", 1, "y"]])
        self.assertEqual(ops[0]["params"]["value"], [-12])
        row["live_asof"] = (now-pd.Timedelta(minutes=3)).isoformat()
        self.assertEqual(canvas.live_marker(row), ([], []))
        row["live"] = None
        self.assertEqual(canvas.live_marker(row), ([], []))

    def test_k_uses_live_ticks_and_overrides_explicit_limits(self):
        now = pd.Timestamp("2026-10-26T12:00Z")
        row = {"month": 1, "live": 12, "live_asof": now.isoformat(), "k": 3, "minimum": 100, "maximum": -100}
        self.assertEqual(canvas.parse_filters([row], now), {1: (.09, .15)})
        row.update(live=-12, k=0)
        self.assertEqual(canvas.parse_filters([row], now), {1: (-.12, -.12)})
        row["live_asof"] = "2026-10-26T11:50Z"
        with self.assertRaisesRegex(ValueError, "fresh live"):
            canvas.parse_filters([row], now)
        row.update(live_asof=now.isoformat(), k=-1)
        with self.assertRaisesRegex(ValueError, "zero or positive"):
            canvas.parse_filters([row], now)
        for value in (.5, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                canvas.parse_filters([{"month": 1, "k": value}])

    def test_live_refresh_only_patches_quote_fields(self):
        row = {"month": 1, "minimum": -5, "maximum": 20, "k": 3}
        with patch.object(canvas, "live_month_ticks", return_value=({1: {"live": 12, "contract": "Jan 27-Feb 27", "live_asof": "now"}}, "ok")):
            with patch.object(canvas, "visible_pairs", return_value=[{"month": 1, "contract": "Jan 27-Feb 27"}]):
                update, status = canvas.refresh_live(0, 0, [row])
        operations = update.to_plotly_json()["operations"]
        self.assertEqual({operation["location"][-1] for operation in operations}, {"live", "contract", "live_asof"})
        self.assertEqual(row["minimum"], -5)
        self.assertEqual(row["k"], 3)
        self.assertEqual(status, "ok")

    def test_live_calendar_prices_use_complete_box_quotes(self):
        from datetime import date
        now = pd.Timestamp("2026-10-26T12:00Z")
        expiry = [("F27", date(2026, 12, 20)), ("G27", date(2027, 1, 20))]
        with patch.object(canvas, "load_expiries", return_value=expiry), patch.object(canvas, "LiveMarketDataClient") as factory:
            client = factory.return_value
            client.quotes.return_value = {"F27-G27": {"value": .05, "timestamp": now.isoformat()}}
            quotes, _ = canvas.live_month_ticks(now)
            self.assertEqual(quotes[1]["live"], 5)
            self.assertEqual(quotes[1]["contract"], "Jan 27-Feb 27")
            client.snapshot.assert_not_called()
            client.quotes.return_value = {"F27-G27": {"value": None, "error": "No fresh quote"}}
            quotes, _ = canvas.live_month_ticks(now)
            self.assertNotIn(1, quotes)
            self.assertEqual(client.close.call_count, 2)

    def test_histograms_display_tick_prices(self):
        frame = pd.DataFrame({"month": [1, 1], "price": [-.12, .03], "frequency": [2, 4]})
        figure = canvas.histogram_cards(frame, [{"month": 1, "contract": "Jan 27-Feb 27"}])[0].figure
        self.assertEqual(list(figure.data[0].x), [-12, 3])
        self.assertEqual(list(figure.data[0].y), [2, 4])
        self.assertIsNone(figure.layout.xaxis.title.text)
        self.assertIsNone(figure.layout.yaxis.title.text)

    def test_front_month_orders_table_and_histograms_across_year_boundary(self):
        from datetime import date
        expiry = [("X26", date(2026, 10, 20)), ("Z26", date(2026, 11, 20)),
                  ("F27", date(2026, 12, 20)), ("G27", date(2027, 1, 20))]
        with patch.object(canvas, "load_expiries", return_value=expiry):
            pairs = canvas.rolling_pairs(pd.Timestamp("2026-10-26T12:00Z"))
        self.assertEqual([pair["month"] for pair in pairs], [12, *range(1, 12)])
        self.assertEqual(pairs[0]["contract"], "Dec 26-Jan 27")
        self.assertEqual(pairs[1]["contract"], "Jan 27-Feb 27")
        self.assertEqual(pairs[-1]["contract"], "Nov 27-Dec 27")
        frame = pd.DataFrame({"month": [1, 12], "price": [.03, .12], "frequency": [4, 2]})
        cards = canvas.histogram_cards(frame, pairs)
        self.assertTrue(cards[0].figure.layout.title.text.startswith("Dec 26-Jan 27"))
        self.assertEqual(cards[0].figure.data[0].x[0], 12)
        self.assertTrue(cards[1].figure.layout.title.text.startswith("Jan 27-Feb 27"))
        with patch.object(canvas, "rolling_pairs", return_value=pairs):
            page = canvas.app.server.test_client().get("/_dash-layout").get_json()
        def find_table(node):
            if isinstance(node, dict):
                if node.get("props", {}).get("id") == "filter-ranges":
                    return node["props"]
                for value in node.values():
                    found = find_table(value)
                    if found is not None:
                        return found
            elif isinstance(node, list):
                for value in node:
                    found = find_table(value)
                    if found is not None:
                        return found
        self.assertIsNone(find_table(page))

    def setUp(self):
        canvas.histogram_results.cache_clear()
        self.reader = memory_reader(duckdb.connect(":memory:"))
        self.reader._connection.execute("CREATE TABLE spread_prices(timestamp TIMESTAMPTZ, instrument VARCHAR, price DOUBLE)")
        self.reader._connection.execute("CREATE TABLE spread_instruments(instrument VARCHAR)")
        self.start = pd.Timestamp("2026-10-01T00:00Z")
        self.end = self.start + pd.Timedelta(minutes=4)

    def tearDown(self):
        canvas.histogram_results.cache_clear()
        self.reader.close()

    def add(self, minute, expression, price, missing_co=False):
        records = []
        for instrument, weight in price_recipe("CL-CO", expression).items():
            if missing_co and weight < 0:
                continue
            records.append((self.start + pd.Timedelta(minutes=minute), instrument, price + 1 if weight > 0 else 1.))
            self.reader._connection.execute("INSERT INTO spread_instruments VALUES (?)", [instrument])
        self.reader._connection.executemany("INSERT INTO spread_prices VALUES (?, ?, ?)", records)

    def test_all_filters_same_minute_and_year_with_missing_targets(self):
        for minute in (0, 1, 2, 3, 4):
            self.add(minute, "F26-G26", .1 if minute != 1 else .5)
            if minute != 2:
                self.add(minute, "G26-H26", .2)
            self.add(minute, "H26-J26", .3 if minute != 3 else .31)
        for minute, feb in ((0, .9), (1, .2)):
            self.add(minute, "F27-G27", .1)
            self.add(minute, "G27-H27", feb)
            self.add(minute, "H27-J27", .7, missing_co=True)
        frame, stats = self.reader.calendar_filter_histograms({1: (.1, .1), 2: (.2, .2)}, self.start, self.end)
        self.assertEqual(stats, {"samples": 3, "minutes": 3, "years": 2})
        jan = frame.loc[frame.month == 1]
        self.assertEqual(jan.frequency.sum(), 3)
        march = frame.loc[frame.month == 3]
        self.assertEqual(dict(zip(march.price, march.frequency)), {.3: 1, .31: 1})

    def test_no_filters_pool_each_contract_year_and_december_rolls(self):
        self.add(0, "F26-G26", .1)
        self.add(0, "F27-G27", .2)
        self.add(0, "Z26-F27", -.12)
        frame, stats = self.reader.calendar_filter_histograms({}, self.start, self.end)
        self.assertEqual(stats, {"samples": 2, "minutes": 1, "years": 2})
        self.assertEqual(frame.loc[frame.month == 12, "price"].tolist(), [-.12])
        frame, stats = self.reader.calendar_filter_histograms({12: (-.12, -.12)}, self.start, self.end)
        self.assertEqual(stats["samples"], 1)
        self.assertEqual(frame.loc[frame.month == 1, "price"].tolist(), [.1])

    def test_filters_cannot_match_using_different_contract_years(self):
        self.add(0, "F26-G26", .1)
        self.add(0, "G26-H26", .9)
        self.add(0, "F27-G27", .9)
        self.add(0, "G27-H27", .2)
        frame, stats = self.reader.calendar_filter_histograms({1: (.1, .1), 2: (.2, .2)}, self.start, self.end)
        self.assertTrue(frame.empty)
        self.assertEqual(stats["samples"], 0)

    def test_no_match_invalid_bounds_and_missing_instruments(self):
        frame, stats = self.reader.calendar_filter_histograms({}, self.start, self.end)
        self.assertTrue(frame.empty)
        self.assertEqual(stats["samples"], 0)
        self.add(0, "F26-G26", .1)
        frame, stats = self.reader.calendar_filter_histograms({2: (0., 1.)}, self.start, self.end)
        self.assertTrue(frame.empty)
        self.assertEqual(stats["samples"], 0)
        for filters in ({1: (2., 1.)}, {1: (float("nan"), None)}, {13: (0., 1.)}):
            with self.assertRaises(ValueError):
                self.reader.calendar_filter_histograms(filters, self.start, self.end)

    def test_seac_layout_does_not_load_removed_conditional_tab(self):
        with patch.object(canvas, 'MarketData') as factory:
            client = canvas.app.server.test_client()
            for endpoint in ('/', '/_dash-layout', '/_dash-dependencies'):
                self.assertEqual(client.get(endpoint).status_code, 200)
            self.assertNotIn('filter-ranges', client.get('/_dash-layout').get_data(as_text=True))
            factory.assert_not_called()

    def test_filter_validation_and_empty_bounds(self):
        self.assertEqual(canvas.parse_filters([{"month": 1, "minimum": "", "maximum": None}]), {})
        self.assertEqual(canvas.parse_filters([{"month": 1, "minimum": -10, "maximum": 0}]), {})
        with patch.object(canvas, "MarketData") as factory:
            graphs, status = canvas.apply_filters(1, [{"month": pair["month"], "k": -1} for pair in canvas.visible_pairs()], 30)
            self.assertEqual(graphs, [])
            self.assertIn("zero or positive", status)
            factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()

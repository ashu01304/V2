"""Bookmarked session range correctness and on-demand loading."""
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import duckdb
import pandas as pd
from dash import Dash, html

from dashboard_components import bookmark_ranges as ranges


class BookmarkRangeTests(unittest.TestCase):
    def setUp(self):
        self.key = json.dumps(["CL-CO", "Z26", "1MS"])
        self.selections = [(self.key, "CL-CO", "Z26-F27")]
        self.now = pd.Timestamp("2026-10-15T12:00:00Z")

    def test_relative_level_extremes_cache_and_session_reset(self):
        service = ranges.BookmarkRanges()
        quote = {"timestamp": self.now.isoformat(), "value": 3}
        with patch.object(ranges, "DATABASE_PATH") as path, \
             patch.object(service, "load_ranges", return_value={self.key: (1, 5)}) as load, \
             patch.object(ranges, "fetch_live_quote", return_value=quote):
            path.stat.return_value = SimpleNamespace(st_mtime_ns=1, st_size=100)
            self.assertEqual(service.positions(self.selections, self.now)[self.key], .5)
            quote["value"] = 7
            self.assertEqual(service.positions(self.selections, self.now)[self.key], 1)
            quote["value"] = 4
            self.assertEqual(service.positions(self.selections, self.now)[self.key], .5)
            self.assertEqual(load.call_count, 1)
            path.stat.return_value.st_mtime_ns = 2
            service.positions(self.selections, self.now)
            self.assertEqual(load.call_count, 2)
            tomorrow = self.now + pd.Timedelta(days=1)
            quote.update(timestamp=tomorrow.isoformat(), value=3)
            self.assertEqual(service.positions(self.selections, tomorrow)[self.key], .5)
            self.assertEqual(load.call_count, 3)

    def test_missing_flat_stale_and_closed_session(self):
        service = ranges.BookmarkRanges()
        with patch.object(ranges, "DATABASE_PATH") as path, \
             patch.object(service, "load_ranges", return_value={self.key: None}), \
             patch.object(ranges, "fetch_live_quote") as live:
            path.stat.return_value = SimpleNamespace(st_mtime_ns=1, st_size=100)
            live.return_value = None
            self.assertEqual(service.positions(self.selections, self.now), {})
            live.return_value = {"timestamp": self.now.isoformat(), "value": 3}
            self.assertEqual(service.positions(self.selections, self.now)[self.key], .5)
            live.return_value["timestamp"] = (self.now - pd.Timedelta(days=1)).isoformat()
            self.assertEqual(service.positions(self.selections, self.now), {})
            live.reset_mock()
            self.assertEqual(service.positions(self.selections, pd.Timestamp("2026-10-15T21:30:00Z")), {})
            self.assertEqual(service.positions(self.selections, pd.Timestamp("2026-10-17T12:00:00Z")), {})
            live.assert_not_called()

    def test_batched_history_requires_matching_legs_and_requested_session(self):
        connection = duckdb.connect(":memory:")
        try:
            connection.execute("CREATE TABLE spread_prices(timestamp TIMESTAMPTZ, instrument VARCHAR, price DOUBLE)")
            connection.executemany("INSERT INTO spread_prices VALUES (?, ?, ?)", [
                ("2026-10-14T22:00:00Z", "CLZ26-F27", 10),
                ("2026-10-14T22:00:00Z", "LCOZ6-F7", 7),
                ("2026-10-15T10:00:00Z", "CLZ26-F27", 12),
                ("2026-10-15T10:00:00Z", "LCOZ6-F7", 7),
                ("2026-10-15T11:00:00Z", "CLZ26-F27", 100),
                ("2026-10-13T10:00:00Z", "CLZ26-F27", -100),
                ("2026-10-13T10:00:00Z", "LCOZ6-F7", 7),
            ])
            db = Mock(connection=connection)
            with patch.object(ranges, "MarketData", return_value=db):
                start, end = ranges.session_window(1, self.now)
                result = ranges.BookmarkRanges.load_ranges(self.selections, start, end)
                self.assertEqual(result[self.key], (3, 5))
                db.close.assert_called_once()
        finally:
            connection.close()

    def test_endpoint_does_no_work_without_valid_bookmarks(self):
        app = Dash(__name__)
        app.layout = html.Div()
        ranges.register_bookmark_ranges(app, {"contracts": {"Z26": {"1MS": "Z26-F27"}}}, str)
        with app.server.test_client() as client, \
             patch.object(ranges.BookmarkRanges, "positions", return_value={self.key: .75}) as positions:
            for body in ({}, {"keys": []}, {"keys": ["invalid"]}):
                self.assertEqual(client.post("/_bookmark-ranges", json=body).get_json(), {})
            positions.assert_not_called()
            self.assertEqual(client.post("/_bookmark-ranges", json={"keys": [self.key]}).get_json(),
                             {self.key: .75})
            positions.assert_called_once_with(self.selections)


if __name__ == "__main__":
    unittest.main()

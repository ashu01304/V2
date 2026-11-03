"""8060 candles must never incorporate a partial structure observation."""

import unittest

import duckdb
import pandas as pd

from candle_dashboard import plot_candles, percentile_candles
from market_data.data_client import DataClient
from tests.helpers import memory_reader


class CompleteLegCandleTests(unittest.TestCase):
    def test_missing_and_invalid_legs_cannot_create_extreme_candles(self):
        connection = duckdb.connect(":memory:")
        reader = memory_reader(connection)
        try:
            connection.execute("CREATE TABLE spread_instruments(instrument VARCHAR)")
            connection.execute("INSERT INTO spread_instruments VALUES ('CLZ26-F27'), ('LCOZ6-F7')")
            connection.execute("CREATE TABLE spread_prices(timestamp TIMESTAMPTZ, instrument VARCHAR, price DOUBLE)")
            connection.execute("CREATE TABLE live_spread_keys(timestamp TIMESTAMPTZ, instrument VARCHAR)")
            rows = [
                ("10:00", "CLZ26-F27", 10), ("10:00", "LCOZ6-F7", 9),
                ("10:01", "CLZ26-F27", 12), ("10:01", "LCOZ6-F7", 10),
                ("10:02", "CLZ26-F27", 10), ("10:02", "LCOZ6-F7", 9),
                ("10:06", "CLZ26-F27", 12), ("10:06", "LCOZ6-F7", 10),
                # A missing leg, null leg, and infinite leg must all be skipped.
                ("10:03", "CLZ26-F27", 9999),
                ("10:04", "CLZ26-F27", 9999), ("10:04", "LCOZ6-F7", None),
                ("10:05", "CLZ26-F27", 9999), ("10:05", "LCOZ6-F7", float("inf")),
                # Different minutes in the same candle do not count as aligned.
                ("10:15", "CLZ26-F27", 9999), ("10:16", "LCOZ6-F7", 9),
            ]
            connection.executemany("INSERT INTO spread_prices VALUES (?, ?, ?)", [
                (pd.Timestamp(f"2026-10-30 {time}", tz="UTC").to_pydatetime(), instrument, price)
                for time, instrument, price in rows
            ])
            with DataClient(database=reader) as client:
                figure, status, _ = plot_candles(
                    "CL-CO", "Z26", "MS", 1, "", "2026-10-30", "2026-10-30", "15min", client=client)
            candle = figure.data[0]
            self.assertEqual(list(candle.open), [1])
            self.assertEqual(list(candle.high), [2])
            self.assertEqual(list(candle.low), [1])
            self.assertEqual(list(candle.close), [2])
            self.assertIn("4 minutes with all legs available", status)
            self.assertIn("Next fetched price, live fallback", status)
        finally:
            reader.close()

    def test_percentile_filter_uses_each_candle_and_retained_time_order(self):
        values = [0, 8, 2, 3, 4, 5, 6, 7, 1, 100, 1000, 1008, 1002, 1003, 1004, 1005, 1006, 1007, 1001, 1100]
        series = pd.Series(values, index=pd.date_range('2026-10-30', periods=20, freq='min', tz='UTC'))
        candles, count = percentile_candles(series, '5min')
        self.assertEqual(count, 12)
        # First bucket [0,8,2,3,4] retains [2,3,4], not a clipped value.
        self.assertEqual(candles.iloc[0][['open','high','low','close']].tolist(), [2,4,2,4])
        # Separate price regime receives its own percentile bounds.
        self.assertEqual(candles.iloc[2][['open','high','low','close']].tolist(), [1002,1004,1002,1004])
        self.assertEqual(series.iloc[0], 0)

    def test_sparse_buckets_and_removed_one_minute_interval(self):
        series = pd.Series([1., 2., 7., 7.], index=pd.to_datetime([
            '2026-10-30T00:00Z','2026-10-30T00:01Z','2026-10-30T00:05Z','2026-10-30T00:06Z']))
        candles, count = percentile_candles(series, '5min')
        self.assertEqual(count, 2)
        self.assertEqual(candles.open.tolist(), [7])
        with self.assertRaisesRegex(ValueError, 'at least 5 minutes'):
            percentile_candles(series, '1min')


if __name__ == "__main__":
    unittest.main()

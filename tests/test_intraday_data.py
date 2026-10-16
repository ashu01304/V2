"""Shared minute queries used by both standalone figures and the main popup."""

from tests.helpers import memory_reader
import unittest

import duckdb
import pandas as pd

from market_data.intraday import load_candles


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


if __name__ == "__main__":
    unittest.main()

"""Historical imports replace live minutes without turning estimates into history."""

import unittest

import pandas as pd

from market_data.database import MasterDatabase
from tests.helpers import memory_reader


class FetchedGapFillTests(unittest.TestCase):
    def test_fill_next_preserving_trailing_live_values_and_provenance(self):
        database = MasterDatabase(":memory:")
        try:
            # Gaps here mean existing provisional rows, not absent minutes.
            database._connection.execute("""INSERT INTO spread_prices VALUES
                ('2026-10-30 09:59:00+00','CLZ26-F27',999),
                ('2026-10-30 10:00:00+00','CLZ26-F27',1),
                ('2026-10-30 10:01:00+00','CLZ26-F27',888),
                ('2026-10-30 10:02:00+00','CLZ26-F27',777),
                ('2026-10-30 10:03:00+00','CLZ26-F27',2),
                ('2026-10-30 10:04:00+00','CLZ26-F27',666),
                ('2026-10-30 10:01:00+00','CLF27-G27',555)
            """)
            database._connection.execute("""INSERT INTO live_spread_keys
                SELECT timestamp,instrument FROM spread_prices WHERE price>100""")
            # The 8060 read path follows the same rule, including anchors
            # outside the selected dates and live fallback at the trailing edge.
            reader = memory_reader(database._connection)
            frame = reader.structure_minutes(
                {'CLZ26-F27': 1}, pd.Timestamp('2026-10-30 09:00Z').to_pydatetime(),
                pd.Timestamp('2026-10-30 11:00Z').to_pydatetime(),
                fetched_only=True, fill_live_gaps=True)
            self.assertEqual(frame.price.tolist(), [1, 1, 2, 2, 2, 666])
            frame = reader.structure_minutes(
                {'CLZ26-F27': 1}, pd.Timestamp('2026-10-30 10:01Z').to_pydatetime(),
                pd.Timestamp('2026-10-30 10:03Z').to_pydatetime(), fill_live_gaps=True)
            self.assertEqual(frame.price.tolist(), [2, 2])
            self.assertEqual(database.fill_live_spread_gaps(), 3)
            prices = database._connection.execute("""SELECT price FROM spread_prices
                WHERE instrument='CLZ26-F27' ORDER BY timestamp""").fetchall()
            self.assertEqual(prices, [(1,), (1,), (2,), (2,), (2,), (666,)])
            # No fetched anchor for the other instrument: never borrow its price.
            self.assertEqual(database._connection.execute("SELECT price FROM spread_prices WHERE instrument='CLF27-G27'").fetchone()[0], 555)
            self.assertEqual(database._connection.execute("SELECT COUNT(*) FROM live_spread_keys").fetchone()[0], 5)
            self.assertEqual(database.fill_live_spread_gaps(), 0)
            # An exact fetched observation replaces the estimate directly.
            database.save_spreads(pd.DataFrame({
                'timestamp': [pd.Timestamp('2026-10-30 10:01:00Z')],
                'instrument': ['CLZ26-F27'], 'price': [1.5],
            }))
            self.assertEqual(database.fill_live_spread_gaps(), 0)
            prices = database._connection.execute("SELECT price FROM spread_prices WHERE instrument='CLZ26-F27' ORDER BY timestamp").fetchall()
            self.assertEqual(prices, [(1,), (1,), (1.5,), (2,), (2,), (666,)])
            self.assertEqual(database._connection.execute("SELECT COUNT(*) FROM live_spread_keys").fetchone()[0], 4)
            # A later fetch now supplies an anchor for the trailing live point.
            database.save_spreads(pd.DataFrame({
                'timestamp': [pd.Timestamp('2026-10-30 10:05:00Z')],
                'instrument': ['CLZ26-F27'], 'price': [3],
            }))
            self.assertEqual(database.fill_live_spread_gaps(), 1)
            self.assertEqual(database._connection.execute("SELECT price FROM spread_prices WHERE instrument='CLZ26-F27' AND timestamp='2026-10-30 10:04:00+00'").fetchone()[0], 3)
        finally:
            database.close()


if __name__ == '__main__':
    unittest.main()

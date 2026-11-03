"""A forced minute refresh ignores incremental cutoffs but honors its date bounds."""

import unittest

import pandas as pd

from market_data.database import MasterDatabase
from market_data.force_refresh_minutes import import_minutes, refresh_window


class ForceMinuteRefreshTests(unittest.TestCase):
    def test_two_calendar_month_window(self):
        start, end = refresh_window("2026-11-02T12:30:00Z")
        self.assertEqual(start, pd.Timestamp("2026-09-02T00:00Z"))
        self.assertEqual(end, pd.Timestamp("2026-11-02T12:30Z"))

    def test_overwrite_fetched_and_live_prices_only_inside_window(self):
        database = MasterDatabase(":memory:")
        try:
            database._connection.execute("""INSERT INTO spread_prices VALUES
                ('2026-09-01 00:00:00+00','CLZ26-F27',111),
                ('2026-09-02 00:00:00+00','CLZ26-F27',222),
                ('2026-10-01 00:00:00+00','CLZ26-F27',333),
                ('2026-10-02 00:00:00+00','CLZ26-F27',444),
                ('2026-11-02 12:30:00+00','CLZ26-F27',555)
            """)
            database._connection.execute("INSERT INTO live_spread_keys VALUES ('2026-10-01 00:00:00+00','CLZ26-F27')")
            database._connection.execute("INSERT INTO seac_settlements VALUES ('CL','Z26','2026-10-01',70)")
            start, end = refresh_window("2026-11-02T12:30Z")
            rows = [( ["CLZ26-F27", "CLF27-G27"], None)]
            for stamp, values in [
                ("2026-09-01T00:00Z", [1, 2]),
                ("2026-09-02T00:00Z", [3, 4]),
                ("2026-10-01T00:00Z", [5, 6]),
                ("2026-10-02T00:00Z", [-100, float('nan')]),
                ("2026-11-02T12:30Z", [7, 8]),
            ]:
                rows.append((pd.Timestamp(stamp).to_pydatetime(), values))
            self.assertEqual(import_minutes(database, iter(rows), start, end), 4)
            actual = database._connection.execute("SELECT price FROM spread_prices WHERE instrument='CLZ26-F27' ORDER BY timestamp").fetchall()
            self.assertEqual(actual, [(111,), (3,), (5,), (444,), (555,)])
            self.assertEqual(database._connection.execute("SELECT COUNT(*) FROM live_spread_keys").fetchone()[0], 0)
            self.assertEqual(database._connection.execute("SELECT price FROM seac_settlements").fetchone()[0], 70)
            self.assertEqual(database._connection.execute("SELECT COUNT(*) FROM spread_prices WHERE instrument='CLF27-G27'").fetchone()[0], 2)
        finally:
            database.close()

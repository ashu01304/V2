"""Offline checks: python -m unittest tests.test_import_filters"""

from datetime import datetime, timezone
import json
import unittest
from unittest.mock import patch

import pandas as pd

from market_data.config import MINUTE_PRODUCTS, SEAC_PRODUCTS, minute_instrument_allowed
from market_data.database import MasterDatabase, spread_frames
from market_data.settlements import rows as settlement_rows

STAMP = datetime(2026, 10, 28, 10, tzinfo=timezone.utc)


class ImportFilterTests(unittest.TestCase):
    def test_exact_symbol_selection_and_compressed_years(self):
        self.assertEqual(set(SEAC_PRODUCTS), {"CL", "CO", "G", "HO", "NG", "RB"})
        self.assertEqual(MINUTE_PRODUCTS, {"CL", "LCO", "LGO", "HO", "NG", "RB", "WTCL-LCO"})
        for code in ("CLZ26-F27", "LCOZ6-F7", "LGOZ6-F7", "HOZ26-F27", "NGZ26-F27", "RBZ26-F27", "WTCL-LCOZ6"):
            with self.subTest(code=code):
                self.assertTrue(minute_instrument_allowed(code))
        for code in ("GCZ26-F27", "NGBFZ6-F7-G7", "HOBFZ6-F7-G7", "RBBFZ6-F7-G7",
                     "NG-HHZ26", "PWTCLZ6-F7", "CLINVALID", "COZ26-F27", None):
            with self.subTest(code=code):
                self.assertFalse(minute_instrument_allowed(code))

    def test_spread_columns_retain_original_price_positions(self):
        source = [(["CLZ26-F27", "GCZ26-F27", "WTCL-LCOZ6", "NGBFZ6-F7-G7", "LGOZ6-F7"], None),
                  (STAMP, [1.0, 999.0, 2.0, 888.0, 3.0])]
        batches = list(spread_frames(source, set(), None))
        self.assertEqual(batches[0], ("headers", ["CLZ26-F27", "WTCL-LCOZ6", "LGOZ6-F7"]))
        self.assertEqual(list(batches[1][1][["instrument", "price"]].itertuples(index=False, name=None)),
                         [("CLZ26-F27", 1.0), ("WTCL-LCOZ6", 2.0), ("LGOZ6-F7", 3.0)])

    def test_settlement_parser_excludes_other_products(self):
        payload = {symbol: {"2026-10-28": {"Z26": 10}} for symbol in (*SEAC_PRODUCTS, "GC", "FCPO", "NGHH")}
        with patch("pathlib.Path.read_text", return_value="var data=" + json.dumps(payload) + ";"):
            parsed = list(settlement_rows("unused.js"))
        self.assertEqual({row[0] for row in parsed}, set(SEAC_PRODUCTS))

    def test_writer_guards_historical_live_and_seac_paths(self):
        db = MasterDatabase(":memory:")
        try:
            symbols = ["CLZ26-F27", "GCZ26-F27", "WTCL-LCOZ6"]
            db.register_spreads(symbols)
            self.assertEqual(set(db.registered_spreads()), {"CLZ26-F27", "WTCL-LCOZ6"})
            frame = pd.DataFrame([(STAMP, code, float(index)) for index, code in enumerate(symbols)],
                                 columns=["timestamp", "instrument", "price"])
            self.assertEqual(db.save_spreads(frame), 2)
            live = frame.copy()
            live["timestamp"] += pd.Timedelta(minutes=1)
            self.assertEqual(db.save_live_spreads(live), 2)
            settlements = pd.DataFrame([(code, "Z26", STAMP.date(), 1.0) for code in ("CL", "CO", "G", "GC")],
                                       columns=["symbol", "contract_code", "trading_date", "price"])
            self.assertEqual(db.save_settlements(settlements), 3)
            self.assertEqual(db._connection.execute("SELECT count(*) FROM spread_prices WHERE instrument='GCZ26-F27'").fetchone()[0], 0)
            self.assertEqual(db._connection.execute("SELECT count(*) FROM live_spread_keys WHERE instrument='GCZ26-F27'").fetchone()[0], 0)
            self.assertEqual(db._connection.execute("SELECT count(*) FROM seac_settlements WHERE symbol='GC'").fetchone()[0], 0)
        finally:
            db.close()

    def test_filters_leave_existing_unwanted_data_unchanged(self):
        db = MasterDatabase(":memory:")
        try:
            db._connection.execute("INSERT INTO spread_prices VALUES (?, 'GCZ26-F27', 10)", [STAMP])
            rejected = pd.DataFrame([(STAMP, "GCZ26-F27", 999)], columns=["timestamp", "instrument", "price"])
            self.assertEqual(db.save_spreads(rejected), 0)
            self.assertEqual(db.save_live_spreads(rejected), 0)
            self.assertEqual(db._connection.execute("SELECT price FROM spread_prices").fetchall(), [(10.0,)])
        finally:
            db.close()

    def test_unwanted_newer_history_does_not_advance_import_cutoff(self):
        db = MasterDatabase(":memory:")
        try:
            db.register_spreads(["CLZ26-F27"])
            db.save_spreads(pd.DataFrame([(STAMP, "CLZ26-F27", 1)],
                                        columns=["timestamp", "instrument", "price"]))
            db._connection.execute("INSERT INTO spread_prices VALUES (?, 'GCZ26-F27', 10)",
                                   [STAMP + pd.Timedelta(days=1)])
            known, cutoff = db.spread_state(3)
            self.assertEqual(known, {"CLZ26-F27"})
            self.assertEqual(cutoff, STAMP - pd.Timedelta(minutes=3))
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()

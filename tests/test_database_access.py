"""Master database access stays behind its named read and write interfaces."""

import ast
from datetime import date, datetime, timezone
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

import duckdb

from tests.helpers import memory_reader
from market_data.queries import database_status, database_signature
from analysis.contract_universe import latest_market_date


class ReadInterfaceTests(unittest.TestCase):
    def setUp(self):
        self.connection = duckdb.connect(":memory:")
        self.connection.execute("""
            CREATE TABLE seac_settlements(
                symbol VARCHAR, contract_code VARCHAR, trading_date DATE, price DOUBLE
            )
        """)
        self.connection.executemany("INSERT INTO seac_settlements VALUES (?, ?, ?, ?)", [
            ("CL", "Z26", "2026-10-15", 70),
            ("CL", "Z26", "2026-10-16", 71),
            ("CL", "F27", "2026-10-16", None),
            ("CO", "Z26", "2026-10-15", 68),
            ("CO", "Z26", "2026-10-16", 68.5),
            ("CO", "F27", "2026-10-16", 69),
            ("NG", "Z26", "2026-10-17", 3.5),
        ])
        self.reader = memory_reader(self.connection)

    def tearDown(self):
        self.reader.close()

    def test_metadata_queries_and_cl_co_mapping(self):
        self.assertEqual(self.reader.symbols(), ["CL", "CO", "NG"])
        self.assertEqual(self.reader.latest_settlement_date(), date(2026, 10, 17))
        self.assertEqual(self.reader.latest_settlement_date("CL-CO"), date(2026, 10, 16))
        self.assertEqual(self.reader.contract_codes("CL-CO"), ["F27", "Z26"])
        self.assertEqual(self.reader.contract_codes("CL", "Z%"), ["Z26"])
        self.assertEqual(self.reader.settlement_dates("CL-CO"), [date(2026, 10, 16), date(2026, 10, 15)])
        self.assertEqual(self.reader.contract_codes("UNKNOWN"), [])
        self.assertIsNone(self.reader.latest_settlement_date("UNKNOWN"))

    def test_daily_snapshots_match_contracts_and_exclude_null_prices(self):
        frame = self.reader.settlements_on("CL-CO", date(2026, 10, 16))
        self.assertEqual(frame.contract_code.tolist(), ["Z26"])
        self.assertEqual(frame.price.tolist(), [2.5])
        self.assertEqual(self.reader.settlements_on("CL", date(2026, 10, 16)).contract_code.tolist(), ["Z26"])
        history = self.reader.get_contract_history("CL-CO", ["Z26"])["Z26"]
        self.assertEqual(history.Close.tolist(), [2, 2.5])

    def test_reader_has_no_public_raw_connection_or_cursor(self):
        for name in ("connection", "cursor", "conn"):
            self.assertFalse(hasattr(self.reader, name))
        with self.assertRaises(ValueError):
            self.reader.settlement_dates("CL", limit=0)
        self.assertEqual(self.reader.expression_ranges([], None, None), {})


class DatabaseStatusTests(unittest.TestCase):
    def test_status_is_lightweight_and_version_changes_on_replacement(self):
        stat = Mock(st_mtime_ns=1792082335894253001, st_size=1237594112,
                    st_ino=987654321012345678)
        with patch("market_data.queries.DATABASE_PATH") as path, \
             patch("market_data.queries.acquire_snapshot") as snapshot:
            path.stat.return_value = stat
            status = database_status()
            self.assertEqual(status.signature, [str(stat.st_mtime_ns), str(stat.st_size), str(stat.st_ino)])
            self.assertEqual(status.updated_at.tzinfo, timezone.utc)
            self.assertEqual(status.updated_at.date(), datetime.fromtimestamp(
                stat.st_mtime_ns / 1_000_000_000, timezone.utc).date())
            stat.st_ino += 1
            self.assertNotEqual(database_signature(), status.signature)
            snapshot.assert_not_called()

    def test_missing_or_unavailable_database_does_not_open_a_reader(self):
        with patch("market_data.queries.DATABASE_PATH") as path, \
             patch("market_data.queries.acquire_snapshot") as snapshot:
            path.stat.side_effect = FileNotFoundError
            self.assertIsNone(database_status())
            self.assertIsNone(database_signature())
            self.assertIsNone(latest_market_date())
            path.stat.side_effect = PermissionError
            self.assertIsNone(database_signature())
            with self.assertRaises(PermissionError):
                database_status()
            snapshot.assert_not_called()


class DatabaseBoundaryTests(unittest.TestCase):
    def test_dashboards_and_analysis_do_not_import_database_files_or_internals(self):
        root = Path(__file__).resolve().parents[1]
        paths = [root / "dashboard.py", root / "main.py"]
        for folder in ("analysis", "dashboard_components", "testing", "backtesting"):
            paths.extend((root / folder).glob("*.py"))
        violations = []
        internal = {"market_data.database", "market_data.publication", "market_data.reader_snapshots"}
        for path in paths:
            tree = ast.parse(path.read_text(encoding="utf-8-sig"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    if node.module in internal or (node.module == "market_data.config"
                            and any(alias.name in {"DATABASE_PATH", "*"} for alias in node.names)):
                        violations.append(f"{path.relative_to(root)}:{node.lineno}")
                elif isinstance(node, ast.Import):
                    if any(alias.name in internal for alias in node.names):
                        violations.append(f"{path.relative_to(root)}:{node.lineno}")
        self.assertEqual(violations, [])

    def test_application_modules_do_not_open_or_query_master_connections(self):
        root = Path(__file__).resolve().parents[1]
        paths = [root / "dashboard.py", root / "main.py"]
        for folder in ("analysis", "dashboard_components", "market_data", "testing", "backtesting"):
            paths.extend((root / folder).glob("*.py"))
        interfaces = {"market_data/queries.py", "market_data/database.py"}
        violations = []
        for path in paths:
            relative = path.relative_to(root).as_posix()
            if relative in interfaces:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8-sig"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                # live_history's execute calls belong only to its private SQLite spool.
                if node.func.attr in {"execute", "executemany"} and relative != "market_data/live_history.py":
                    violations.append(f"{relative}:{node.lineno} executes SQL")
                if ast.unparse(node.func) == "duckdb.connect":
                    violations.append(f"{relative}:{node.lineno} opens DuckDB")
        self.assertEqual(violations, [])


if __name__ == "__main__":
    unittest.main()

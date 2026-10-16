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


class DashboardLiveRefreshTests(unittest.TestCase):
    def setUp(self):
        import threading
        import pandas as pd
        import numpy as np
        from analysis.seasonality import Seasonality
        from analysis.expression import parse_expression
        self.pd = pd
        self.Seasonality = Seasonality
        old = pd.DataFrame({"value": [8., 9.], "date": pd.to_datetime(["2025-10-14", "2025-10-15"]),
                            "is_live": [False, False]}, index=[-2, -1])
        current = pd.DataFrame({"value": [10.], "date": pd.to_datetime(["2026-10-14"]),
                                "is_live": [False]}, index=[-2])
        self.result = Seasonality._package_working_days(None, {2025: old, 2026: current}, [], 400)
        self.result["dashboard_context"] = {"year": 2026, "settlement": 10.,
            "last_settlement": pd.Timestamp("2026-10-14"),
            "expiry": pd.Timestamp("2026-10-16"), "window_days": 400}
        frames = {"values": pd.DataFrame({"Contract": ["Z26"], "1MS": [10.]}),
                  "changes": pd.DataFrame({"Contract": ["Z26"], "1MS": [0.]})}
        self.key = ("CL", "Z26-F27", 4)
        self.state = {"frames": {"CL": frames}, "results": {}, "cache": {self.key: ((10., 0.), self.result)},
            "cache_version": (("1", "100", "10"), pd.Timestamp.now(tz="UTC").date()),
            "job_id": 1, "processing": True, "version": 0}
        client = Mock()
        client.expression.return_value = {"value": 12., "timestamp": "2026-10-15T10:00:00Z"}
        self.client = client
        self.ns = {"pd": pd, "np": np, "Seasonality": Seasonality, "state_lock": threading.Lock(),
            "dashboard_state": self.state, "LiveMarketDataClient": Mock(return_value=client),
            "MarketData": Mock(), "database_signature": Mock(return_value=["1", "100", "10"]),
            "expression_metrics": Mock(return_value=((10., 0.), self.result)),
            "latest_settlement_date": Mock(return_value="2026-10-14"),
            "log_dashboard": Mock(), "publish_dashboard_snapshot": Mock(),
            "RequestException": __import__("requests").RequestException, "empty_frames": frames,
            "parse_expression": parse_expression, "make_display": Mock()}
        tree = ast.parse(Path("dashboard.py").read_text(encoding="utf-8"))
        functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                     and node.name in {"copy_frames", "product_frames", "calculate_dashboard",
                                       "expression_metrics", "start_dashboard_job"}]
        metrics_mock = self.ns["expression_metrics"]
        exec(compile(ast.Module(body=functions, type_ignores=[]), "dashboard.py", "exec"), self.ns)
        self.real_metrics = self.ns["expression_metrics"]
        self.ns["expression_metrics"] = metrics_mock

    def test_initial_analysis_caches_the_actual_common_settlement_baseline(self):
        seasonality = Mock()
        index = self.pd.to_datetime(["2026-10-14"])
        seasonality.db.get_contract_history.return_value = {
            "Z26": self.pd.DataFrame({"Close": [12.]}, index=index),
            "F27": self.pd.DataFrame({"Close": [2.]}, index=index)}
        seasonality.official_or_last_date.return_value = self.pd.Timestamp("2026-10-16")
        seasonality.working_day_expression_seasonality.return_value = self.result
        metrics, result = self.real_metrics(seasonality, "CL", "Z26-F27", 4)
        self.assertEqual(metrics, (10., 0.))
        self.assertEqual(result["dashboard_context"]["settlement"], 10.)
        self.assertEqual(result["dashboard_context"]["last_settlement"], index[0])

    def test_starting_live_job_keeps_cache_and_skips_while_busy(self):
        cache = self.state["cache"]
        self.assertIsNone(self.ns["start_dashboard_job"](["CL"], ["1MS"], True))
        self.state["processing"] = False
        self.assertEqual(self.ns["start_dashboard_job"](["CL"], ["1MS"], True), 2)
        self.assertIs(self.state["cache"], cache)
        self.assertIn(self.key, cache)

    def run_job(self, years=4):
        self.ns["calculate_dashboard"]({"contracts": {"Z26": {"1MS": "Z26-F27"}}},
            ["1MS"], ["CL"], {"CL": years}, 1, True)

    def test_live_refresh_uses_cache_without_opening_or_querying_database(self):
        self.run_job()
        self.ns["MarketData"].assert_not_called()
        self.ns["expression_metrics"].assert_not_called()
        metrics, result = self.state["cache"][self.key]
        self.assertEqual(metrics, (12., 2.))
        self.assertIs(result["series"][2025], self.result["series"][2025])
        self.assertEqual(len(self.result["series"][2026]), 1)
        self.client.close.assert_called_once()
        self.ns["publish_dashboard_snapshot"].assert_called_once()

    def test_publication_new_day_and_year_settings_invalidate_history(self):
        # Avoid constructing expiry lookups: this test exercises job coordination.
        self.ns["Seasonality"] = Mock(refresh_live_result=self.Seasonality.refresh_live_result)
        self.ns["database_signature"].return_value = ["2", "100", "10"]
        self.run_job()
        self.assertEqual(self.ns["MarketData"].call_count, 1)
        self.run_job()
        self.assertEqual(self.ns["MarketData"].call_count, 1)
        self.state["cache_version"] = (("2", "100", "10"), date(2000, 1, 1))
        self.run_job()
        self.assertEqual(self.ns["MarketData"].call_count, 2)
        self.run_job(years=5)
        self.assertEqual(self.ns["MarketData"].call_count, 3)

    def test_missing_quote_retains_last_value_and_settlement_baseline(self):
        self.run_job()
        self.client.expression.side_effect = self.ns["RequestException"]("offline")
        self.run_job()
        self.assertEqual(self.state["cache"][self.key][0], (12., 2.))
        self.ns["MarketData"].assert_not_called()

    def test_superseded_job_cannot_write_cache_or_publish(self):
        def supersede(*args):
            self.state["job_id"] = 2
            return {"value": 99., "timestamp": "2026-10-15T10:00:00Z"}
        self.client.expression.side_effect = supersede
        self.run_job()
        self.assertEqual(self.state["cache"][self.key][0], (10., 0.))
        self.ns["publish_dashboard_snapshot"].assert_not_called()
        self.client.close.assert_called_once()

    def test_live_result_replaces_previous_point_and_rejects_invalid_quotes(self):
        refresh = self.Seasonality.refresh_live_result
        args = (2026,)
        result = refresh(self.result, *args, {"value": 12, "timestamp": "2026-10-15T10:00Z"},
                         "2026-10-14", "2026-10-16", 400)
        newer = refresh(result, *args, {"value": 13, "timestamp": "2026-10-16T10:00Z"},
                        "2026-10-14", "2026-10-16", 400)
        self.assertEqual(newer["series"][2026]["value"].tolist(), [10., 13.])
        expected = self.Seasonality._package_working_days(None, newer["series"], [], 400)
        self.pd.testing.assert_frame_equal(newer["raw_combined"], expected["raw_combined"])
        self.pd.testing.assert_frame_equal(newer["combined"], expected["combined"])

        for quote in ({"value": float("nan"), "timestamp": "2026-10-16"},
                      {"value": 14, "timestamp": "2026-10-17"},
                      {"value": 14, "timestamp": "2026-10-15"},
                      {"value": 14, "timestamp": "2026-10-14"},
                      {"value": 14, "timestamp": None}):
            self.assertIs(refresh(newer, *args, quote, "2026-10-14", "2026-10-16", 400), newer)


if __name__ == "__main__":
    unittest.main()

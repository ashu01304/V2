"""Regression checks for minute recording, writer coordination and official precedence."""

from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import duckdb
import pandas as pd

from market_data.database import MasterDatabase
from market_data.live_history import LiveMinuteRecorder, spread_closes, PUBLISH_SECONDS
from market_data.live_service import PriceStore
from market_data.publication import writer_lock
from market_data.queries import MarketData
from market_data.sync import update

UTC = timezone.utc
BASE = datetime(2026, 10, 15, 10, 0, tzinfo=UTC)


def spread_frame(timestamp, price, instrument="CLZ26-F27"):
    return pd.DataFrame([(timestamp, instrument, price)],
                        columns=["timestamp", "instrument", "price"])


class MinuteHistoryTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="live_history_test_")
        self.root = Path(self.folder.name).resolve()
        self.assertTrue(self.root.is_relative_to(Path(tempfile.gettempdir()).resolve()))
        self.master = self.root / "master.duckdb"
        self.spool = self.root / "buffer.sqlite3"
        database = MasterDatabase(self.master)
        try:
            database.register_spreads(["CLZ26-F27", "LCOZ6-F7", "NGZ26-F27"])
            database.save_spreads(spread_frame(BASE, 1.0))
            database.finish()
        finally:
            database.close()
        self.recorder = LiveMinuteRecorder(self.master, self.spool)

    def tearDown(self):
        self.recorder.close()
        self.folder.cleanup()

    def query(self, sql, args=None):
        connection = duckdb.connect(str(self.master), read_only=True)
        try:
            return connection.execute(sql, args or []).fetchall()
        finally:
            connection.close()

    def record_pair(self, minute=10, first=70.0, second=69.0):
        when = BASE + timedelta(minutes=minute, seconds=58)
        self.recorder.record("CL", "Z26", first, when)
        self.recorder.record("CL", "F27", second, when)

    def publish(self, minute=12):
        with closing(self.recorder._connect()) as connection:
            self.recorder._persist(connection)
            self.recorder._publish(connection, BASE + timedelta(minutes=minute, seconds=3))

    def test_last_valid_update_and_only_completed_minutes(self):
        self.record_pair()
        latest = BASE + timedelta(minutes=10, seconds=59)
        self.recorder.record("CL", "Z26", 70.25, latest)
        self.recorder.record("CL", "Z26", 99, latest - timedelta(seconds=20))
        for price in (None, float("nan"), float("inf"), -100):
            self.recorder.record("CL", "Z26", price, latest)
        self.record_pair(minute=12, first=80)
        self.publish()
        rows = self.query("SELECT timestamp, price FROM spread_prices ORDER BY timestamp")
        self.assertEqual(len(rows), 2)
        self.assertAlmostEqual(rows[-1][1], 1.25)
        self.assertEqual(rows[-1][0], BASE + timedelta(minutes=10))
        with closing(self.recorder._connect()) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM minute_closes").fetchone()[0], 2)

    def test_no_cross_minute_or_missing_leg_fill(self):
        self.recorder.record("CL", "Z26", 70, BASE + timedelta(minutes=10, seconds=58))
        self.recorder.record("CL", "F27", 69, BASE + timedelta(minutes=11, seconds=58))
        self.publish()
        self.assertEqual(self.query("SELECT COUNT(*) FROM spread_prices")[0][0], 1)

    def test_buffer_survives_restart(self):
        self.record_pair()
        with closing(self.recorder._connect()) as connection:
            self.recorder._persist(connection)
        restarted = LiveMinuteRecorder(self.master, self.spool)
        with closing(restarted._connect()) as connection:
            restarted._publish(connection, BASE + timedelta(minutes=12, seconds=3))
        self.assertEqual(self.query("SELECT COUNT(*) FROM live_spread_keys")[0][0], 1)

    def test_failed_publish_retains_buffer_and_master(self):
        self.record_pair()
        signature = hashlib.sha256(self.master.read_bytes()).hexdigest()
        with closing(self.recorder._connect()) as connection:
            self.recorder._persist(connection)
            with patch("market_data.live_history.atomic_publish", side_effect=PermissionError("busy")):
                with self.assertRaises(PermissionError):
                    self.recorder._publish(connection, BASE + timedelta(minutes=12, seconds=3))
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM minute_closes").fetchone()[0], 2)
            self.assertEqual(hashlib.sha256(self.master.read_bytes()).hexdigest(), signature)
            self.recorder._publish(connection, BASE + timedelta(minutes=12, seconds=3))
        self.assertEqual(self.query("SELECT COUNT(*) FROM live_spread_keys")[0][0], 1)
        self.assertEqual(list(self.root.glob(".live_*.duckdb")), [])

    def test_download_overrides_live_and_replay_cannot_overwrite_download(self):
        self.record_pair()
        self.publish()
        stamp = BASE + timedelta(minutes=10)
        database = MasterDatabase(self.master)
        try:
            database.save_spreads(spread_frame(stamp, 2.5))
            self.assertEqual(database.save_live_spreads(spread_frame(stamp, 99)), 0)
            database.finish()
        finally:
            database.close()
        self.assertEqual(self.query("SELECT price FROM spread_prices WHERE timestamp=?", [stamp]), [(2.5,)])
        self.assertEqual(self.query("SELECT COUNT(*) FROM live_spread_keys"), [(0,)])

    def test_reimport_starts_before_oldest_unreconciled_live_minute(self):
        database = MasterDatabase(self.master)
        try:
            older = BASE - timedelta(days=2)
            database.save_live_spreads(spread_frame(older, 0.5))
            database.save_spreads(spread_frame(BASE + timedelta(days=1), 2))
            _known, cutoff = database.spread_state(3)
            self.assertEqual(cutoff, older)
        finally:
            database.close()

    def test_daily_sync_replaces_older_live_rows_and_keeps_newer_minutes(self):
        self.record_pair(minute=1)
        self.record_pair(minute=10, first=72)
        self.publish()
        downloaded = [["CLZ26-F27"], [
            [BASE.isoformat().replace("+00:00", "Z"), 1.0],
            [(BASE + timedelta(minutes=1)).isoformat().replace("+00:00", "Z"), 1.8],
        ]]
        def download(path):
            Path(path).write_text(json.dumps(downloaded), encoding="utf-8")
        with patch("market_data.sync.spreads.download", side_effect=download):
            update(self.master, include_settlements=False)
        rows = self.query("SELECT timestamp, price FROM spread_prices ORDER BY timestamp")
        self.assertEqual([row[1] for row in rows], [1.0, 1.8, 3.0])
        self.assertEqual(self.query("SELECT timestamp FROM live_spread_keys"),
                         [(BASE + timedelta(minutes=10),)])

    def test_writer_lock_defers_live_without_consuming_buffer(self):
        self.record_pair()
        with closing(self.recorder._connect()) as connection:
            self.recorder._persist(connection)
            with writer_lock(self.master, timeout=0):
                with self.assertRaises(TimeoutError):
                    self.recorder._publish(connection, BASE + timedelta(minutes=12, seconds=3))
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM minute_closes").fetchone()[0], 2)
            self.recorder._publish(connection, BASE + timedelta(minutes=12, seconds=3))

    def test_snapshots_can_read_during_publication_and_reopen_for_new_minutes(self):
        reader = MarketData(self.master)
        try:
            self.record_pair()
            self.publish()
            self.assertEqual(len(reader.spread("CLZ26-F27")), 1)
            fresh = MarketData(self.master)
            try:
                self.assertEqual(len(fresh.spread("CLZ26-F27")), 2)
            finally:
                fresh.close()
        finally:
            reader.close()

    def test_service_snapshot_is_not_recorded_as_new_price(self):
        recorder = Mock()
        store = PriceStore({"TT-1": ("CL", "Z26")}, recorder=recorder)
        fields = {"AdminPrice": "70.12345"}
        store.update("TT-1", fields, record=False)
        recorder.record.assert_not_called()
        store.update("TT-1", fields)
        self.assertEqual(recorder.record.call_args.args[:3], ("CL", "Z26", 70.12345))

    def test_source_aliases_and_ambiguous_decades(self):
        minute = int(BASE.timestamp())
        records = [(minute, "CO", "Z26", 70, 1),
                   (minute, "CO", "F27", 69, 1),
                   (minute, "NG", "Z26", 3.1234, 1),
                   (minute, "NG", "F27", 3.0012, 1)]
        frame = spread_closes(records, ["LCOZ6-F7", "NGZ26-F27"])
        self.assertEqual(len(frame), 2)
        self.assertAlmostEqual(frame.loc[frame.instrument == "NGZ26-F27", "price"].iloc[0], 0.1222)
        records.append((minute, "CO", "Z36", 90, 1))
        self.assertNotIn("LCOZ6-F7", spread_closes(records, ["LCOZ6-F7"])["instrument"].tolist())

    def test_expiry_calendar_resolves_near_and_far_decade_without_mixing_prices(self):
        from market_data.live_history import load_contract_expiries
        minute = int(BASE.timestamp())
        records = [(minute, "CO", "Z26", 70, 1),
                   (minute, "CO", "F27", 69, 1),
                   (minute, "CO", "Z36", 90, 1),
                   (minute, "CO", "F37", 80, 1)]
        expiries = load_contract_expiries()
        frame = spread_closes(records, ["LCOZ6-F7"], expiries)
        self.assertEqual(frame.price.tolist(), [1.0])
        # Missing the current-decade leg must never substitute a far-decade price.
        missing = [row for row in records if row[2] != "Z26"]
        self.assertTrue(spread_closes(missing, ["LCOZ6-F7"], expiries).empty)

    def test_published_co_minutes_include_current_decade_when_feed_has_both(self):
        when = BASE + timedelta(minutes=10, seconds=58)
        for contract, price in [("Z26", 70), ("F27", 69), ("Z36", 90), ("F37", 80)]:
            self.recorder.record("CO", contract, price, when)
        self.publish()
        self.assertEqual(self.query("SELECT price FROM spread_prices WHERE instrument='LCOZ6-F7'"),
                         [(1.0,)])

    def test_worker_publishes_and_second_recorder_cannot_share_buffer(self):
        self.assertEqual(PUBLISH_SECONDS, 600)
        self.recorder.publish_seconds = 0.05
        when = datetime.now(UTC) - timedelta(minutes=2)
        self.recorder.record("CL", "Z26", 71, when)
        self.recorder.record("CL", "F27", 69, when)
        self.recorder.start()
        other = LiveMinuteRecorder(self.master, self.spool)
        with self.assertRaises(TimeoutError):
            other.start()
        deadline = time.monotonic() + 5
        while self.recorder.health()["last_publish_at"] is None and time.monotonic() < deadline:
            time.sleep(0.02)
        self.recorder.close()
        self.assertIsNotNone(self.recorder.health()["last_publish_at"])
        self.assertIsNone(self.recorder.health()["last_error"])

    def test_watch_runs_once_by_default_and_compatibility_flag(self):
        import market_data.watch as watch
        for argv in (["watch.py"], ["watch.py", "--once"]):
            lock = Mock()
            with patch.object(watch, "configure_logging"), patch.object(watch.faulthandler, "enable"), \
                 patch.object(watch, "acquire_lock", return_value=lock), \
                 patch.object(watch, "update", return_value={"spread_added": 1, "settlement_added": 2}) as updater, \
                 patch("sys.argv", argv):
                self.assertEqual(watch.main(), 0)
                updater.assert_called_once_with(include_settlements=True)
                lock.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()

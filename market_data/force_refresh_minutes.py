"""One-time reimport of fetched minute prices for the last two calendar months."""

import argparse
from contextlib import closing
import logging
from math import isfinite
from pathlib import Path
import shutil
import sys
from uuid import uuid4

import pandas as pd

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from market_data import spreads
from market_data.config import DATABASE_PATH
from market_data.database import MasterDatabase, spread_frames
from market_data.publication import atomic_publish, writer_lock


logger = logging.getLogger(__name__)


def refresh_window(now=None):
    end = pd.Timestamp.now(tz="UTC") if now is None else pd.Timestamp(now)
    end = end.tz_localize("UTC") if end.tzinfo is None else end.tz_convert("UTC")
    return end.normalize() - pd.DateOffset(months=2), end.ceil("min")


def window_rows(rows, start, end):
    """Retain headers, but bound every instrument, including newly seen ones."""
    for stamp, prices in rows:
        if prices is None or start <= pd.Timestamp(stamp) < end:
            yield stamp, prices


def import_minutes(database, rows, start, end):
    processed = 0
    with closing(window_rows(rows, start, end)) as selected:
        with closing(spread_frames(selected, set(), start.to_pydatetime())) as frames:
            for kind, value in frames:
                if kind == "headers":
                    database.register_spreads(value)
                else:
                    value = value.loc[value.price.map(isfinite)]
                    processed += database.save_spreads(value)
                    logger.info("Fetched minute rows reimported: %s", f"{processed:,}")
    return processed


def refresh(database_path=DATABASE_PATH, *, now=None):
    """Overwrite matching fetched minutes; preserve unmatched rows and settlements."""
    target = Path(database_path).resolve()
    if not target.is_file():
        raise FileNotFoundError(f"Master database does not exist: {target}")
    start, end = refresh_window(now)
    token = uuid4().hex
    download = target.parent / f".force_minutes_{token}.json"
    working = target.parent / f".force_minutes_{token}.duckdb"
    backup = target.with_name(f"{target.stem}.before_minute_refresh_{token}.duckdb")
    logger.info("One-time minute refresh: %s to %s UTC (end exclusive)", start, end)
    try:
        # The source is a full-history file; only the requested window is imported.
        logger.info("Downloading fetched minute history...")
        spreads.download(download)
        with writer_lock(target):
            shutil.copy2(target, backup)
            logger.info("Original database backed up to %s", backup)
            shutil.copy2(backup, working)
            database = MasterDatabase(working)
            try:
                with closing(spreads.rows(download)) as rows:
                    count = import_minutes(database, rows, start, end)
                if count == 0:
                    raise ValueError("No valid fetched minute prices in this window; master was not changed.")
                # Do not fill gaps here: this repair imports actual fetched data only.
                database.finish()
            finally:
                database.close()
            atomic_publish(working, target)
        logger.info("Refresh complete: %s fetched rows processed. Matching prices overwritten; "
                    "unmatched minutes, older data and settlements retained.", f"{count:,}")
        return {"processed": count, "start": start, "end": end, "backup": backup}
    finally:
        for path in (download, working, Path(str(working) + ".wal")):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                logger.warning("Could not remove temporary file: %s", path, exc_info=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    log_path = Path(DATABASE_PATH).parent / "force_refresh_minutes.log"
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout),
                                  logging.FileHandler(log_path, encoding="utf-8")])
    try:
        refresh()
        return 0
    except Exception:
        logger.exception("One-time refresh failed; the master database was not published.")
        return 1


if __name__ == "__main__":
    sys.exit(main())

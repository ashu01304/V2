from contextlib import closing
import logging
from datetime import datetime
from pathlib import Path
import shutil
from uuid import uuid4

import pandas as pd

from .config import BATCH_SIZE, DATABASE_PATH, DATA_DIR, SPREAD_OVERLAP_MINUTES
from .database import MasterDatabase, spread_frames
from . import spreads, settlements
from .publication import writer_lock, atomic_publish


logger = logging.getLogger(__name__)


def _temporary(name, directory=DATA_DIR):
    return Path(directory) / f".{name}_{uuid4().hex}.tmp"


def update(database_path=DATABASE_PATH, include_settlements=True):
    """One explicit historical import, coordinated with the live-minute writer."""
    with writer_lock(database_path):
        return _update_locked(database_path, include_settlements)


def _update_locked(database_path, include_settlements):
    database_path = Path(database_path).resolve()
    database_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_database = _temporary("master_database", database_path.parent)
    spread_file = _temporary("spreads", database_path.parent)
    settlement_file = _temporary("settlements", database_path.parent)
    published = False
    try:
        if database_path.exists():
            logger.info("Copying existing database to %s", temporary_database)
            shutil.copy2(database_path, temporary_database)
        else:
            logger.info("First import: final database will appear after the full import succeeds.")

        logger.info("Downloading spread history...")
        spreads.download(spread_file)
        logger.info("Spread download complete: %.1f MB", spread_file.stat().st_size / 1_000_000)
        if include_settlements:
            logger.info("Downloading SEAC settlements...")
            settlements.download(settlement_file)
            logger.info("Settlement download complete: %.1f MB", settlement_file.stat().st_size / 1_000_000)

        logger.info("Opening temporary database: %s", temporary_database)
        database = MasterDatabase(temporary_database)
        try:
            spreads_before, settlements_before = database.row_counts()
            known, cutoff = database.spread_state(SPREAD_OVERLAP_MINUTES)
            logger.info("Importing spreads | cutoff: %s", cutoff or "all history")
            spread_count = 0
            # Close both generators before cleanup, including on import errors.
            with closing(spreads.rows(spread_file)) as source_rows:
                with closing(spread_frames(source_rows, known, cutoff)) as frames:
                    for kind, value in frames:
                        if kind == "headers":
                            database.register_spreads(value)
                            logger.info("Registered %s spread instruments", len(value))
                        else:
                            spread_count += database.save_spreads(value)
                            logger.info("Spread rows processed: %s", f"{spread_count:,}")
                        del value

            if include_settlements:
                logger.info("Importing SEAC settlements...")
                batch = []
                settlement_count = 0
                for row in settlements.rows(settlement_file):
                    batch.append(row)
                    if len(batch) >= BATCH_SIZE:
                        settlement_count += database.save_settlements(pd.DataFrame(
                            batch, columns=["symbol", "contract_code", "trading_date", "price"]
                        ))
                        logger.info("Settlement rows processed: %s", f"{settlement_count:,}")
                        batch.clear()
                if batch:
                    settlement_count += database.save_settlements(pd.DataFrame(
                        batch, columns=["symbol", "contract_code", "trading_date", "price"]
                    ))
                logger.info("Settlement import finished: %s rows processed", f"{settlement_count:,}")
            logger.info("Checkpointing database...")
            database.finish()
            spreads_after, settlements_after = database.row_counts()
        finally:
            database.close()

        incoming = database_path.with_suffix(".duckdb.incoming")
        logger.info("Publishing database to %s", database_path)
        shutil.copy2(temporary_database, incoming)
        try:
            atomic_publish(incoming, database_path)
        except PermissionError as error:
            raise PermissionError(
                f"Cannot publish {database_path}. Close dashboards or other programs "
                f"using it and check folder permissions. Completed copy: {incoming}"
            ) from error
        published = True
        spread_added = spreads_after - spreads_before
        settlement_added = settlements_after - settlements_before
        updated_at = datetime.now().astimezone()
        logger.info("Last update: %s", updated_at.strftime("%Y-%m-%d %H:%M:%S %Z"))
        logger.info("Total spread points: %s | total settlement points: %s",
                    f"{spreads_after:,}", f"{settlements_after:,}")
        return {
            "spread_added": spread_added,
            "settlement_added": settlement_added,
            "updated_at": updated_at,
        }
    finally:
        for path in (spread_file, settlement_file):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                logger.warning("Could not remove downloaded file: %s", path, exc_info=True)
        if published:
            try:
                temporary_database.unlink(missing_ok=True)
            except OSError:
                logger.warning("Could not remove published temporary DB: %s", temporary_database, exc_info=True)
        elif temporary_database.exists():
            logger.warning("Import was not published. Temporary DB retained: %s", temporary_database)

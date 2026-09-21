from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pandas as pd

from .config import DATABASE_PATH, DATA_DIR, SPREAD_OVERLAP_MINUTES
from .database import MasterDatabase, spread_frames
from . import spreads, settlements


def _temporary(name):
    return DATA_DIR / f".{name}_{uuid4().hex}.tmp"


def _clean_stale_downloads():
    cutoff = datetime.now().timestamp() - timedelta(days=1).total_seconds()
    for pattern in (".spreads_*.tmp", ".settlements_*.tmp"):
        for path in DATA_DIR.glob(pattern):
            if path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)


def update(database_path=DATABASE_PATH, include_settlements=True):
    """Download first, then update the master database in one transaction."""
    database_path = Path(database_path)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    _clean_stale_downloads()
    spread_file = _temporary("spreads")
    settlement_file = _temporary("settlements")
    try:
        spreads.download(spread_file)
        if include_settlements:
            settlements.download(settlement_file)

        database = MasterDatabase(database_path)
        try:
            spreads_before, settlements_before = database.row_counts()
            known, cutoff = database.spread_state(SPREAD_OVERLAP_MINUTES)
            database.connection.execute("BEGIN")
            try:
                for kind, value in spread_frames(spreads.rows(spread_file), known, cutoff):
                    if kind == "headers":
                        database.register_spreads(value)
                    else:
                        database.save_spreads(value)

                if include_settlements:
                    batch = []
                    for row in settlements.rows(settlement_file):
                        batch.append(row)
                        if len(batch) >= 500_000:
                            database.save_settlements(pd.DataFrame(
                                batch, columns=["symbol", "contract_code", "trading_date", "price"]
                            ))
                            batch.clear()
                    if batch:
                        database.save_settlements(pd.DataFrame(
                            batch, columns=["symbol", "contract_code", "trading_date", "price"]
                        ))
                database.connection.execute("COMMIT")
            except Exception:
                database.connection.execute("ROLLBACK")
                raise
            database.finish()
            spreads_after, settlements_after = database.row_counts()
        finally:
            database.close()

        spread_added = spreads_after - spreads_before
        settlement_added = settlements_after - settlements_before
        updated_at = datetime.now().astimezone()
        print(f"Last update: {updated_at:%Y-%m-%d %H:%M:%S %Z}")
        print(f"New spread points added: {spread_added:,}")
        if include_settlements:
            print(f"New settlement points added: {settlement_added:,}")
        else:
            print("SEAC settlement update skipped for this cycle")
        print(f"Total spread points: {spreads_after:,}")
        print(f"Total settlement points: {settlements_after:,}")
        return {
            "spread_added": spread_added,
            "settlement_added": settlement_added,
            "updated_at": updated_at,
        }
    finally:
        for path in (spread_file, settlement_file):
            path.unlink(missing_ok=True)

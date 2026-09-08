import os
from datetime import datetime
from pathlib import Path
import shutil
from uuid import uuid4

import pandas as pd

from .config import DATABASE_PATH, DATA_DIR, SPREAD_OVERLAP_MINUTES
from .database import MasterDatabase, spread_frames
from . import spreads, settlements


def _temporary(name):
    return DATA_DIR / f".{name}_{uuid4().hex}.tmp"


def update(database_path=DATABASE_PATH, include_settlements=True):
    """Update a temporary copy and replace the master only after full success."""
    database_path = Path(database_path)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temporary_database = _temporary("master_database")
    spread_file = _temporary("spreads")
    settlement_file = _temporary("settlements")
    if database_path.exists():
        shutil.copy2(database_path, temporary_database)

    published = False
    try:
        spreads.download(spread_file)
        if include_settlements:
            settlements.download(settlement_file)

        database = MasterDatabase(temporary_database)
        try:
            spreads_before, settlements_before = database.row_counts()
            known, cutoff = database.spread_state(SPREAD_OVERLAP_MINUTES)
            spread_count = 0
            for kind, value in spread_frames(spreads.rows(spread_file), known, cutoff):
                if kind == "headers":
                    database.register_spreads(value)
                else:
                    spread_count += database.save_spreads(value)

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
            database.finish()
            spreads_after, settlements_after = database.row_counts()
        finally:
            database.close()

        incoming = database_path.with_suffix(".duckdb.incoming")
        shutil.copy2(temporary_database, incoming)
        os.replace(incoming, database_path)
        published = True
        spread_added = spreads_after - spreads_before
        settlement_added = settlements_after - settlements_before
        updated_at = datetime.now().astimezone()
        print(f"Last update: {updated_at:%Y-%m-%d %H:%M:%S %Z}")
        print(f"New spread points added: {spread_added:,}")
        if include_settlements:
            print(f"New settlement points added: {settlement_added:,}")
        else:
            print("SEAC settlement update skipped for hourly cycle")
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
        if published:
            temporary_database.unlink(missing_ok=True)
        elif temporary_database.exists():
            print(f"Update failed; master is unchanged. Temporary DB: {temporary_database}")

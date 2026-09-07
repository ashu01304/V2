import os
from pathlib import Path
import shutil
from uuid import uuid4

import pandas as pd

from . import settlements
from .config import DATABASE_PATH, DATA_DIR
from .database import MasterDatabase


def update_settlements(database_path=DATABASE_PATH):
    """Download and publish SEAC settlements without processing spread data."""
    database_path = Path(database_path)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    token = uuid4().hex
    source_path = DATA_DIR / f".seac_{token}.tmp"
    temporary_database = DATA_DIR / f".seac_database_{token}.tmp"
    incoming = database_path.with_suffix(".duckdb.incoming")
    published = False

    if database_path.exists():
        shutil.copy2(database_path, temporary_database)

    try:
        print("Downloading SEAC source...", flush=True)
        settlements.download(source_path)
        database = MasterDatabase(temporary_database)
        try:
            before_count = database.connection.execute(
                "SELECT COUNT(*) FROM seac_settlements"
            ).fetchone()[0]
            previous_latest = database.connection.execute(
                "SELECT MAX(trading_date) FROM seac_settlements"
            ).fetchone()[0]

            processed = 0
            source_latest = None
            batch = []
            for row in settlements.rows(source_path):
                batch.append(row)
                trading_date = pd.Timestamp(row[2]).date()
                source_latest = max(source_latest, trading_date) if source_latest else trading_date
                if len(batch) >= 250_000:
                    processed += database.save_settlements(pd.DataFrame(
                        batch, columns=["symbol", "contract_code", "trading_date", "price"]
                    ))
                    batch.clear()
                    print(f"SEAC rows processed: {processed:,}", end="\r", flush=True)
            if batch:
                processed += database.save_settlements(pd.DataFrame(
                    batch, columns=["symbol", "contract_code", "trading_date", "price"]
                ))

            if source_latest is None:
                raise ValueError("Downloaded SEAC source contained no settlement rows")
            if previous_latest is not None and source_latest < previous_latest:
                raise ValueError(
                    f"Downloaded SEAC source is older ({source_latest}) than database "
                    f"({previous_latest}); master was not replaced"
                )
            database.finish()
            after_count = database.connection.execute(
                "SELECT COUNT(*) FROM seac_settlements"
            ).fetchone()[0]
            saved_latest = database.connection.execute(
                "SELECT MAX(trading_date) FROM seac_settlements"
            ).fetchone()[0]
        finally:
            database.close()

        shutil.copy2(temporary_database, incoming)
        os.replace(incoming, database_path)
        published = True
        result = {
            "processed": processed,
            "new_rows": after_count - before_count,
            "previous_latest": previous_latest,
            "source_latest": source_latest,
            "saved_latest": saved_latest,
        }
        print("\nSEAC-only update completed.")
        for key, value in result.items():
            print(f"{key}: {value}")
        return result
    finally:
        source_path.unlink(missing_ok=True)
        if published:
            temporary_database.unlink(missing_ok=True)
        elif temporary_database.exists():
            print(f"Update failed; master unchanged. Temporary DB: {temporary_database}")


def main():
    update_settlements()


if __name__ == "__main__":
    main()

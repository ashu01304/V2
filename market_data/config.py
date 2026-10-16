from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"
DATABASE_PATH = DATA_DIR / "master_database.duckdb"

SPREAD_URL = (
    "https://insight.corp.hertshtengroup.com/technicals/"
    "rangebound/historical/alldatahistoricalenergy.js"
)
SEAC_URL = (
    "https://insight.corp.hertshtengroup.com/structured/"
    "compressed/allseacdata.js"
)

SPREAD_OVERLAP_MINUTES = 3
# Keep Python batches small while DuckDB also holds indexes and query buffers.
BATCH_SIZE = 50_000
DUCKDB_MEMORY_LIMIT = '1GB'

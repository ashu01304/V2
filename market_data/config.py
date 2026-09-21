from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"
DATABASE_PATH = DATA_DIR / "master_database.duckdb"
CACHE_PATH = DATA_DIR / "live_price_cache.sqlite"

SPREAD_URL = (
    "https://insight.corp.hertshtengroup.com/technicals/"
    "rangebound/historical/alldatahistoricalenergy.js"
)
SEAC_URL = (
    "https://insight.corp.hertshtengroup.com/structured/"
    "compressed/allseacdata.js"
)

SPREAD_OVERLAP_MINUTES = 180
BATCH_SIZE = 500_000
CACHE_HOURS = 3
SPREAD_UPDATE_MINUTES = 30
SEAC_UPDATE_HOURS = 6

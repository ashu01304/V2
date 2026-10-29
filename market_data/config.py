from pathlib import Path
import re


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"
DATABASE_PATH = DATA_DIR / "master_database.duckdb"

# Product names exposed by settlements and the live bridge.
LIVE_PRODUCTS = ("CL", "CO", "G", "HO", "NG", "RB")
MINUTE_PRODUCT_NAMES = {"CO": "LCO", "G": "LGO"}
SEAC_PRODUCTS = LIVE_PRODUCTS
MINUTE_PRODUCTS = frozenset(
    [MINUTE_PRODUCT_NAMES.get(product, product) for product in SEAC_PRODUCTS] + ["WTCL-LCO"]
)
_MINUTE_CODE = re.compile(r"([^0-9]+)[FGHJKMNQUVXZ]\d{1,2}(?:-[FGHJKMNQUVXZ]\d{1,2})*")


def minute_instrument_allowed(instrument):
    """Match complete product names, avoiding NG-HH/NGBF and similar prefixes."""
    match = _MINUTE_CODE.fullmatch(instrument) if isinstance(instrument, str) else None
    return bool(match and match[1] in MINUTE_PRODUCTS)

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

from pathlib import Path


BASE_URL = "https://qh-api.corp.hertshtengroup.com/api"
TOKEN_URL = f"{BASE_URL}/token/"
REFRESH_URL = f"{BASE_URL}/token/refresh/"
API_V2 = f"{BASE_URL}/v2"
MAX_CONTRACTS_PER_CALL = 50
MAX_BARS_PER_CALL = 10_000

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TOKEN_FILE = PROJECT_ROOT / "data" / "tokens.json"

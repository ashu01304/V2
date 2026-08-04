from pathlib import Path
import pandas as pd

EXPIRY_DATES_PATH = Path(__file__).parent.parent / "expiry_dates.csv"

class OfficialExpiryLookup:
    def __init__(self, path=EXPIRY_DATES_PATH):
        self.path = Path(path)
        self._lookup = None

    def get(self, symbol, contract_code):
        if self._lookup is None:
            expiry_dates = pd.read_csv(self.path, parse_dates=["expiry_date"])
            self._lookup = expiry_dates.set_index(["symbol", "contract_code"])["expiry_date"]
        return self._lookup.get((symbol, contract_code))

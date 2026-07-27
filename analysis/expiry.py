import pandas as pd

MONTH_CODES = {'F':1,'G':2,'H':3,'J':4,'K':5,'M':6,'N':7,'Q':8,'U':9,'V':10,'X':11,'Z':12}

class ExpiryEstimator:
    def __init__(self, db):
        self.db = db
        self._global_max_date = None

    def _get_global_max_date(self, symbol):
        if self._global_max_date is None:
            self.db.cursor.execute(
                "SELECT MAX(trading_date) FROM seac_settlements WHERE symbol = ?", (symbol,)
            )
            self._global_max_date = pd.Timestamp(self.db.cursor.fetchone()[0])
        return self._global_max_date

    def _delivery_first_day(self, month_letter, year):
        return pd.Timestamp(year=year, month=MONTH_CODES[month_letter], day=1)

    def estimate_expiry(self, symbol, contract_code, live_buffer_days=10, min_samples=2):
        """
        Estimate expiry for `contract_code` using historical last-trade-date
        offsets from other contracts sharing the same month letter.
        """
        month_letter = contract_code[0]
        target_yy = int(contract_code[1:])
        target_year = 2000 + target_yy
        global_max = self._get_global_max_date(symbol)

        self.db.cursor.execute(
            "SELECT DISTINCT contract_code FROM seac_settlements WHERE symbol = ? AND contract_code LIKE ?",
            (symbol, f"{month_letter}%")
        )
        candidates = [r[0] for r in self.db.cursor.fetchall() if r[0] != contract_code]

        offsets = []
        for code in candidates:
            yy = int(code[1:])
            year = 2000 + yy
            self.db.cursor.execute(
                "SELECT MAX(trading_date) FROM seac_settlements WHERE symbol = ? AND contract_code = ?",
                (symbol, code)
            )
            last_date = self.db.cursor.fetchone()[0]
            if not last_date:
                continue
            last_date = pd.Timestamp(last_date)

            # Skip contracts still live (last date too close to the DB's overall latest date)
            if (global_max - last_date).days < live_buffer_days:
                continue

            delivery_day1 = self._delivery_first_day(month_letter, year)
            offsets.append((delivery_day1 - last_date).days)

        if len(offsets) < min_samples:
            return None, offsets  # not enough history to trust an estimate

        avg_offset = sum(offsets) / len(offsets)
        target_delivery_day1 = self._delivery_first_day(month_letter, target_year)
        estimated_expiry = target_delivery_day1 - pd.Timedelta(days=avg_offset)
        return estimated_expiry, offsets
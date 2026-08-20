import numpy as np
import pandas as pd

from analysis.expression import evaluate_expression, parse_expression, shift_contract_year
from analysis.expiry import OfficialExpiryLookup


class StrategyRollover:
    def __init__(self, db):
        self.db = db
        self.official_expiry = OfficialExpiryLookup()

    def calculate(self, symbol, expression, periods=6):
        series, warnings = {}, []
        self.db.cursor.execute(
            "SELECT MAX(trading_date) FROM seac_settlements WHERE symbol = ?", (symbol,)
        )
        latest_market_date = pd.Timestamp(self.db.cursor.fetchone()[0]).normalize()
        self.latest_market_date = latest_market_date
        ordered = self._contract_sequence(symbol)
        position = {contract: index for index, (_, contract) in enumerate(ordered)}
        legs = parse_expression(expression)
        if not legs or any(contract not in position for _, contract in legs):
            return {"series": {}, "warnings": ["selected expression is outside expiry sequence"]}

        front = next((index for index, (expiry, _) in enumerate(ordered)
                      if expiry >= latest_market_date), None)
        anchor = position[legs[0][1]]
        if front is None or anchor < front:
            return {"series": {}, "warnings": ["could not determine current contract distance"]}
        distance = anchor - front

        for shift in range(min(periods, min(position[contract] for _, contract in legs) + 1)):
            shifted_legs = [
                (coefficient, ordered[position[contract] - shift][1])
                for coefficient, contract in legs
            ]
            shifted_expression = self._format_expression(shifted_legs)
            shifted_anchor = anchor - shift
            rollover_index = shifted_anchor - distance - 1
            if rollover_index <= 0 or rollover_index + 1 >= len(ordered):
                continue
            start_expiry = ordered[rollover_index - 1][0]
            rollover_expiry = ordered[rollover_index][0]
            end_expiry = ordered[rollover_index + 1][0]

            values = self._expression_values(symbol, shifted_expression)
            values = values[(values.index >= start_expiry) & (values.index <= end_expiry)]
            if values.empty:
                warnings.append(f"{shifted_expression}: no data inside monthly window")
                continue
            before = values[values.index <= rollover_expiry]
            base = before.iloc[-1] if not before.empty else values.iloc[0]
            dates = pd.DatetimeIndex(values.index).normalize()
            data = pd.DataFrame({
                "value": values.values - base,
                "date": dates,
                "side": np.where(dates <= rollover_expiry,
                                 "Before monthly rollover", "After monthly rollover"),
            }, index=np.busday_count(
                rollover_expiry.to_datetime64().astype("datetime64[D]"),
                dates.values.astype("datetime64[D]"),
            ))
            series[shifted_expression] = {
                "expression": shifted_expression,
                "active": rollover_expiry <= latest_market_date < end_expiry,
                "data": data,
                "current_expiry_day": int(np.busday_count(
                    rollover_expiry.to_datetime64().astype("datetime64[D]"),
                    end_expiry.to_datetime64().astype("datetime64[D]"),
                )),
            }
        return {"series": series, "warnings": warnings}

    def _contract_sequence(self, symbol):
        self.db.cursor.execute(
            "SELECT DISTINCT contract_code FROM seac_settlements WHERE symbol = ?", (symbol,)
        )
        return sorted(
            (expiry, contract)
            for contract, in self.db.cursor.fetchall()
            for expiry in [self._official_expiry(symbol, contract)]
            if expiry is not None
        )

    @staticmethod
    def _format_expression(legs):
        parts = []
        for index, (coefficient, contract) in enumerate(legs):
            sign = "-" if coefficient < 0 else "+" if index else ""
            size = abs(coefficient)
            multiplier = "" if size == 1 else f"{size:g}*"
            parts.append(f"{sign}{multiplier}{contract}")
        return "".join(parts)

    def _expression_values(self, symbol, expression):
        legs = parse_expression(expression)
        codes = list(dict.fromkeys(code for _, code in legs))
        histories = self.db.get_contract_history(symbol, codes)
        if any(code not in histories or histories[code].empty for code in codes):
            return pd.Series(dtype=float)
        data = pd.DataFrame({code: histories[code]["Close"] for code in codes}).dropna()
        return evaluate_expression(expression, data)

    def _official_expiry(self, symbol, contract):
        if not contract:
            return None
        official = self.official_expiry.get(symbol, contract)
        if official is not None:
            return pd.Timestamp(official).normalize()
        if 2000 + int(contract[1:]) > self.latest_market_date.year:
            return None
        history = self.db.get_contract_history(symbol, [contract]).get(contract)
        if history is None or history.empty:
            return None
        last_date = history.index.max().normalize()
        if (2000 + int(contract[1:]) == self.latest_market_date.year
                and last_date >= self.latest_market_date - pd.offsets.BDay(3)):
            return None
        return last_date

    def _previous_contract(self, symbol, expiry):
        self.db.cursor.execute(
            "SELECT DISTINCT contract_code FROM seac_settlements WHERE symbol = ?", (symbol,)
        )
        candidates = []
        for contract, in self.db.cursor.fetchall():
            contract_expiry = self._official_expiry(symbol, contract)
            if contract_expiry is not None and contract_expiry < expiry:
                candidates.append((contract_expiry, contract))
        return max(candidates)[1] if candidates else None

    def _previous_expression(self, symbol, expression):
        self.db.cursor.execute(
            "SELECT DISTINCT contract_code FROM seac_settlements WHERE symbol = ?", (symbol,)
        )
        ordered = sorted(
            (expiry, contract)
            for contract, in self.db.cursor.fetchall()
            for expiry in [self._official_expiry(symbol, contract)]
            if expiry is not None
        )
        position = {contract: index for index, (_, contract) in enumerate(ordered)}
        shifted = []
        for index, (coefficient, contract) in enumerate(parse_expression(expression)):
            contract_index = position.get(contract, 0)
            if contract_index == 0:
                return None
            sign = "-" if coefficient < 0 else "+" if index else ""
            size = abs(coefficient)
            multiplier = "" if size == 1 else f"{size:g}*"
            shifted.append(f"{sign}{multiplier}{ordered[contract_index - 1][1]}")
        return "".join(shifted)


def shift_expression(expression, years):
    shifted = []
    for index, (coefficient, contract) in enumerate(parse_expression(expression)):
        sign = "-" if coefficient < 0 else "+" if index else ""
        size = abs(coefficient)
        multiplier = "" if size == 1 else f"{size:g}*"
        shifted.append(f"{sign}{multiplier}{shift_contract_year(contract, years)}")
    return "".join(shifted)

import re

from analysis.expression import parse_expression
from market_data import LiveMarketDataClient, MarketData


MONTH_ORDER = {month: index for index, month in enumerate("FGHJKMNQUVXZ", 1)}


class ForwardCurve:
    def __init__(self, live=None, db=None):
        self.live = live or LiveMarketDataClient()
        self.db = db or MarketData()

    @staticmethod
    def _format(legs):
        parts = []
        for index, (coefficient, contract) in enumerate(legs):
            sign = "-" if coefficient < 0 else "+" if index else ""
            size = abs(coefficient)
            parts.append(f"{sign}{'' if size == 1 else f'{size:g}*'}{contract}")
        return "".join(parts)

    @staticmethod
    def _prices(rows, contract_key="contract"):
        prices = {}
        for row in rows:
            contract, price = row.get(contract_key, ""), row.get("price")
            match = re.fullmatch(r"([FGHJKMNQUVXZ])(\d{2})", contract)
            if match and price is not None:
                month, year = match.groups()
                ordinal = (2000 + int(year)) * 12 + MONTH_ORDER[month] - 1
                prices[ordinal] = (contract, float(price))
        return prices

    def calculate(self, symbol, expression):
        legs = parse_expression(expression)
        if not legs:
            return {"live": [], "settlements": []}
        first_month, first_year = legs[0][1][0], int(legs[0][1][1:]) + 2000
        first_ordinal = first_year * 12 + MONTH_ORDER[first_month] - 1
        offsets = []
        for coefficient, contract in legs:
            ordinal = (2000 + int(contract[1:])) * 12 + MONTH_ORDER[contract[0]] - 1
            offsets.append((coefficient, ordinal - first_ordinal))

        def build(prices, anchors=None):
            points = []
            for anchor in sorted(prices if anchors is None else anchors):
                shifted = [(coefficient, prices[anchor + offset][0])
                           for coefficient, offset in offsets if anchor + offset in prices]
                if len(shifted) != len(offsets):
                    continue
                value = sum(coefficient * prices[anchor + offset][1]
                            for coefficient, offset in offsets)
                points.append({"contract": self._format(shifted),
                               "label": shifted[0][1],
                               "price": round(value, 2)
                               if symbol in {"CL", "CO"} else value,
                               "order": anchor})
            return points

        live = build(self._prices(self.live.snapshot([symbol], raw=True)))
        anchors = {point["order"] for point in live}
        dates = [row[0] for row in self.db.connection.execute("""
            SELECT DISTINCT trading_date FROM seac_settlements
            WHERE symbol=? ORDER BY trading_date DESC LIMIT 2
        """, [symbol]).fetchall()]
        settlements = []
        for date in dates:
            frame = self.db.connection.execute("""
                SELECT contract_code, price FROM seac_settlements
                WHERE symbol=? AND trading_date=? AND price IS NOT NULL
            """, [symbol, date]).fetchdf()
            rows = [{"contract_code": row.contract_code, "price": row.price}
                    for row in frame.itertuples(index=False)]
            settlements.append({"date": str(date),
                                "points": build(self._prices(rows, "contract_code"), anchors)})
        return {"live": live, "settlements": settlements}

    def close(self):
        self.live.close()
        self.db.close()

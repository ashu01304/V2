"""Expression-universe availability for range testing."""

from analysis.contract_universe import load_universe
from analysis.expression import parse_expression
from market_data import MarketData


UNIVERSE = load_universe()
STRATEGIES = ("1MF",)


def available_contract_codes():
    database = MarketData()
    try:
        rows = database.cursor.execute(
            "SELECT DISTINCT contract_code FROM seac_settlements WHERE symbol = ?",
            ["CL"],
        ).fetchall()
        return {code for code, in rows}
    finally:
        database.close()


def expression_rows():
    available, rows = available_contract_codes(), []
    for contract, expressions in UNIVERSE["contracts"].items():
        expression = expressions["1MF"]
        if all(code in available for _, code in parse_expression(expression)):
            rows.append({"Contract": contract, "1MF": " "})
    return rows

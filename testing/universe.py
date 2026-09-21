"""Expression-universe availability for range testing."""

from analysis.contract_universe import load_universe
from analysis.expression import parse_expression
from market_data import MarketData


UNIVERSE = load_universe()
STRATEGIES = UNIVERSE["columns"]


def available_contract_codes(symbol):
    database = MarketData()
    try:
        rows = database.cursor.execute(
            "SELECT DISTINCT contract_code FROM seac_settlements WHERE symbol = ?",
            ["CL" if symbol == "CL-CO" else symbol],
        ).fetchall()
        return {code for code, in rows}
    finally:
        database.close()


def expression_rows(symbol):
    available, rows = available_contract_codes(symbol), []
    for contract, expressions in UNIVERSE["contracts"].items():
        row = {"Contract": contract}
        for strategy, expression in expressions.items():
            if all(code in available for _, code in parse_expression(expression)):
                row[strategy] = " "
        if len(row) > 1:
            rows.append(row)
    return rows


def visible_expression_rows(rows_by_symbol, symbol, selected_strategies):
    selected = set(selected_strategies or [])
    return [row for row in rows_by_symbol.get(symbol, [])
            if any(strategy in row for strategy in selected)]

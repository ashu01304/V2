import re

import pandas as pd


CONTRACT_PATTERN = re.compile(r"([+-]?)(?:(\d+(?:\.\d+)?)\*)?([FGHJKMNQUVXZ]\d{2})")
MONTH_NAMES = dict(zip("FGHJKMNQUVXZ", "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()))


def format_contract_label(expression):
    """Show readable contract months without changing data identifiers."""
    return re.sub(r"\b([FGHJKMNQUVXZ])(\d{2})\b",
                  lambda match: f"{MONTH_NAMES[match[1]]} {match[2]}", expression)


def parse_expression(expression):
    terms = []
    for sign, coefficient, contract in CONTRACT_PATTERN.findall(expression.replace(" ", "")):
        value = float(coefficient) if coefficient else 1.0
        terms.append((-value if sign == "-" else value, contract))
    return terms


def contract_codes(expression):
    return list(dict.fromkeys(contract for _, contract in parse_expression(expression)))


def shift_contract_year(contract, years):
    return f"{contract[0]}{(int(contract[1:]) + years) % 100:02d}"


def evaluate_expression(expression, leg_data):
    result = pd.Series(0.0, index=leg_data.index)
    for coefficient, contract in parse_expression(expression):
        result += coefficient * leg_data[contract]
    return result.dropna()

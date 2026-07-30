from datetime import datetime


MONTH_CODES = "FGHJKMNQUVXZ"
HORIZON = 14


def get_contracts():
    today = datetime.today()
    contracts = []

    for offset in range(1, HORIZON + 1):
        month_index = today.month + offset
        month = (month_index - 1) % 12 + 1
        year = today.year + (month_index - 1) // 12
        contracts.append(f"{MONTH_CODES[month - 1]}{year % 100:02d}")

    return contracts


def months_to_expression(months):
    terms = []

    for coefficient, contract in zip(months, get_contracts()):
        if coefficient == 0:
            continue

        sign = "+" if coefficient > 0 else "-"
        value = abs(coefficient)
        term = contract if value == 1 else f"{value}*{contract}"
        terms.append(f"{sign} {term}")

    return " ".join(terms).removeprefix("+ ")

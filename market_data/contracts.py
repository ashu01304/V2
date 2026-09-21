"""Build zero-sum synthetic contracts entirely from stored spread prices."""

import re

import pandas as pd


TERM = re.compile(
    r"([+-]?)(?:(\d+(?:\.\d+)?)\*)?([FGHJKMNQUVXZ]\d{1,2})"
)


def _stored_contract_code(product, contract):
    """Convert contract codes to the format used by a product's spread data."""
    if product == "LCO":
        return f"{contract[0]}{contract[-1]}"
    return contract


def parse(expression):
    expression = expression.replace(" ", "")
    legs = []
    position = 0
    for match in TERM.finditer(expression):
        if match.start() != position:
            raise ValueError(f"Could not parse expression near {expression[position:]!r}")
        sign, size, contract = match.groups()
        coefficient = float(size or 1)
        if sign == "-":
            coefficient *= -1
        legs.append((coefficient, contract))
        position = match.end()
    if not legs or position != len(expression):
        raise ValueError(f"Invalid contract expression: {expression!r}")
    if abs(sum(coefficient for coefficient, _ in legs)) > 1e-10:
        raise ValueError(
            "The expression is not zero-sum and cannot be built from spreads alone"
        )
    return legs


def spread_recipe(product, expression):
    """Return [(weight, stored spread symbol)] for a linear expression."""
    legs = parse(expression)
    recipe = []
    cumulative = 0.0
    for index in range(len(legs) - 1):
        cumulative += legs[index][0]
        if abs(cumulative) <= 1e-10:
            continue
        first = _stored_contract_code(product, legs[index][1])
        second = _stored_contract_code(product, legs[index + 1][1])
        recipe.append((cumulative, f"{product}{first}-{second}"))
    return recipe


def minute_series(loader, product, expression, start=None, end=None):
    recipe = spread_recipe(product, expression)
    frames = []
    for weight, symbol in recipe:
        frame = loader(symbol, start=start, end=end)
        if frame.empty:
            raise ValueError(f"No stored spread data for {symbol}")
        frames.append(
            frame.rename(columns={"price": symbol}).set_index("timestamp")[[symbol]]
        )

    aligned = pd.concat(frames, axis=1, join="inner").dropna()
    if aligned.empty:
        raise ValueError("Required spreads have no common minute timestamps")

    synthetic = sum(
        weight * aligned[symbol]
        for weight, symbol in recipe
    )
    return synthetic.rename("price").reset_index()


def ohlc(loader, product, expression, interval="30min", start=None, end=None):
    minute = minute_series(loader, product, expression, start, end)
    return (
        minute.set_index("timestamp")["price"]
        .resample(interval)
        .ohlc()
        .dropna()
    )

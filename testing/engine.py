"""Range calculation, backtesting, and chart construction."""

from functools import lru_cache
from threading import Lock

import numpy as np
from scipy.signal import find_peaks

from analysis.seasonality import Seasonality
from market_data import MarketData
from product_config import PRODUCT_CONFIG
from testing.config import *
from testing.universe import UNIVERSE, STRATEGIES, expression_rows
from testing.universe import visible_expression_rows as filter_expression_rows

SERIES_CACHE = {}
CACHE_LOCK = Lock()


def visible_expression_rows(symbol, selected_strategies):
    return filter_expression_rows(
        EXPRESSION_ROWS, symbol, selected_strategies
    )


EXPRESSION_ROWS = {symbol: expression_rows(symbol) for symbol in TEST_SYMBOLS}
DEFAULT_ROWS = EXPRESSION_ROWS[DEFAULT_SYMBOL]

DEFAULT_ROW = 0
DEFAULT_STRATEGY = next(
    strategy for strategy in STRATEGIES if DEFAULT_ROWS[DEFAULT_ROW].get(strategy)
)


def load_expression_series(symbol, expression, current_year, history_years):
    start_year = max(2000, current_year - history_years)
    key = (symbol, expression, start_year, current_year)
    with CACHE_LOCK:
        cached = SERIES_CACHE.get(key)
    if cached is not None:
        return cached
    database = MarketData()
    try:
        result = Seasonality(database).working_day_expression_seasonality(
            symbol, expression, start_year, current_year, WINDOW_DAYS,
        )
    finally:
        database.close()
    if not result["series"]:
        warnings = "; ".join(result.get("warnings", [])) or "no seasonality data"
        raise ValueError(warnings)
    series = {year: frame.sort_index() for year, frame in result["series"].items()}
    with CACHE_LOCK:
        SERIES_CACHE[key] = series
    return series


def ranked_extrema_level(values, lookback, rank, peaks=True):
    if lookback < 1 or rank < 1 or rank > lookback:
        raise ValueError("Rank must be between 1 and its lookback")
    array = values.to_numpy(dtype=float)
    positions = find_peaks(array if peaks else -array)[0]
    confirmed = {position + 1: array[position] for position in positions}
    recent = []
    level = np.full(len(array), np.nan)
    for position in range(len(array)):
        if position in confirmed:
            recent.append(confirmed[position])
            recent = recent[-lookback:]
        if len(recent) == lookback:
            level[position] = sorted(recent, reverse=peaks)[rank - 1]
    return level


def ranked_daily_level(values, lookback, rank, highest=True):
    if lookback < 1 or rank < 1 or rank > lookback:
        raise ValueError("Rank must be between 1 and its lookback")
    index = lookback - rank if highest else rank - 1
    return values.rolling(lookback, min_periods=lookback).apply(
        lambda window: np.partition(window, index)[index], raw=True
    ).to_numpy()


def boundary(values, mode, lookback, rank, upper):
    if mode == "extrema":
        return ranked_extrema_level(values, lookback, rank, peaks=upper)
    if mode == "days":
        return ranked_daily_level(values, lookback, rank, highest=upper)
    raise ValueError(f"Unknown calculation mode: {mode}")


def strongest_recent_swing_ticks(values, tick_size, swing_lookback, swing_top_count):
    """Return the causal transformed average of the strongest recent swings."""
    array = values.to_numpy(dtype=float)
    peak_positions = set(find_peaks(array)[0])
    valley_positions = set(find_peaks(-array)[0])
    confirmed = {}
    for position in peak_positions | valley_positions:
        # A local turning point is knowable only after the following settlement.
        confirmed[position + 1] = (position, array[position])

    indicator = np.full(len(array), np.nan)
    recent_differences = []
    previous_turning_position = None
    previous_turning_value = None
    current_average = np.nan
    for confirmation_position in range(len(array)):
        turning_point = confirmed.get(confirmation_position)
        if turning_point is not None:
            turning_position, turning_value = turning_point
            if (previous_turning_position is not None
                    and turning_position > previous_turning_position):
                recent_differences.append(
                    abs(turning_value - previous_turning_value)
                )
                recent_differences = recent_differences[-swing_lookback:]
                if len(recent_differences) == swing_lookback:
                    strongest = sorted(recent_differences, reverse=True)[
                        :swing_top_count
                    ]
                    average_ticks = np.mean(strongest) / tick_size
                    current_average = float(np.ceil(average_ticks ** 0.9))
            previous_turning_position = turning_position
            previous_turning_value = turning_value
        indicator[confirmation_position] = current_average
    return indicator


def entry_candidates(values, upper, lower):
    """Return causal crossings of boundaries known at the prior settlement."""
    prices = values.to_numpy(dtype=float)
    candidates = {}
    for index in range(1, len(prices)):
        previous_price = prices[index - 1]
        price = prices[index]
        known_upper = upper[index - 1]
        known_lower = lower[index - 1]
        if not np.isfinite(known_upper) or not np.isfinite(known_lower):
            continue
        if previous_price < known_upper <= price:
            candidates[index] = (-1, known_upper)
        elif previous_price > known_lower >= price:
            candidates[index] = (1, known_lower)
    return candidates


def backtest_range(values, upper, lower, swing_ticks, max_parallel_positions,
                   max_consecutive_direction, no_trade_days, max_trade_days,
                   minimum_swing_ticks, tick_size, include_entries=False):
    prices = values.to_numpy(dtype=float)
    candidates = entry_candidates(values, upper, lower)
    outcomes, entries, trades = [], [], []
    positions = []
    last_direction = None
    consecutive_direction = 0
    for index, price in enumerate(prices):
        remaining = []
        for trade in positions:
            direction, target, stop = trade["direction"], trade["target"], trade["stop"]
            target_hit = price >= target if direction == 1 else price <= target
            stop_hit = price <= stop if direction == 1 else price >= stop
            if target_hit:
                outcomes.append("TP")
                trade.update(outcome="TP", exit_index=index, exit_price=target,
                             pnl_ticks=trade["distance_ticks"])
            elif stop_hit:
                outcomes.append("SL")
                trade.update(outcome="SL", exit_index=index, exit_price=stop,
                             pnl_ticks=-trade["distance_ticks"])
            else:
                remaining.append(trade)
        positions = remaining

        candidate = candidates.get(index)
        if candidate is not None:
            day = float(values.index[index])
            if day < -max_trade_days or day >= -no_trade_days:
                candidate = None
        if candidate is not None and len(positions) < max_parallel_positions:
            direction, entry = candidate
            if (direction == last_direction
                    and consecutive_direction >= max_consecutive_direction):
                continue
            distance_ticks = swing_ticks[index - 1]
            if not np.isfinite(distance_ticks) or distance_ticks <= minimum_swing_ticks:
                continue
            distance = distance_ticks * tick_size
            target = entry + direction * distance
            stop = entry - direction * distance
            trade = {
                "direction": direction, "entry_index": index, "entry_price": entry,
                "target": target, "stop": stop, "distance_ticks": distance_ticks,
                "outcome": "OPEN", "exit_index": None, "exit_price": None,
                "pnl_ticks": None,
            }
            positions.append(trade)
            trades.append(trade)
            consecutive_direction = (consecutive_direction + 1
                                     if direction == last_direction else 1)
            last_direction = direction
            label = "LONG" if direction == 1 else "SHORT"
            entries.append((index, label, entry, target, stop))
    unresolved = len(positions)
    return ((outcomes, entries, trades, unresolved) if include_entries
            else (outcomes, trades, unresolved))


def trade_rows(trades, frame, decimals):
    rows = []
    for trade in trades:
        rows.append({
            "Direction": "LONG" if trade["direction"] == 1 else "SHORT",
            "Entry price": round(trade["entry_price"], decimals),
            "Exit price": (round(trade["exit_price"], decimals)
                           if trade["exit_price"] is not None else None),
            "Outcome": trade["outcome"],
            "Holding days": (int(frame.index[trade["exit_index"]]
                                 - frame.index[trade["entry_index"]])
                             if trade["exit_index"] is not None else None),
            "TP ticks": int(trade["distance_ticks"]),
            "SL ticks": int(trade["distance_ticks"]),
            "PnL ticks": (int(trade["pnl_ticks"])
                          if trade["pnl_ticks"] is not None else None),
        })
    return rows


def accuracy(outcomes):
    return outcomes.count("TP") / len(outcomes) if outcomes else np.nan


@lru_cache(maxsize=512)
def calculate_expression(symbol, contract, strategy, mode, peak_lookback, peak_rank,
                         valley_lookback, valley_rank, max_parallel_positions,
                         max_consecutive_direction, no_trade_days, max_trade_days,
                         swing_lookback, swing_top_count, minimum_swing_ticks,
                         history_years):
    config = PRODUCT_CONFIG["CL" if symbol == "CL-CO" else symbol]
    tick_size = config["tick_size"]
    max_parallel_positions = (
        DEFAULT_MAX_PARALLEL_POSITIONS
        if max_parallel_positions is None
        else int(max_parallel_positions)
    )
    if max_parallel_positions < 1:
        raise ValueError("Max parallel positions must be at least 1")
    max_consecutive_direction = (
        DEFAULT_MAX_CONSECUTIVE_DIRECTION
        if max_consecutive_direction is None
        else int(max_consecutive_direction)
    )
    if max_consecutive_direction < 1:
        raise ValueError("Max consecutive same-direction entries must be at least 1")
    no_trade_days = DEFAULT_NO_TRADE_DAYS if no_trade_days is None else int(no_trade_days)
    if no_trade_days < 0:
        raise ValueError("No-trade days before expiry cannot be negative")
    max_trade_days = DEFAULT_MAX_TRADE_DAYS if max_trade_days is None else int(max_trade_days)
    if max_trade_days <= no_trade_days:
        raise ValueError("Maximum days before expiry must exceed no-trade days")
    swing_lookback, swing_top_count = int(swing_lookback), int(swing_top_count)
    minimum_swing_ticks = int(minimum_swing_ticks)
    if swing_lookback < 1 or not 1 <= swing_top_count <= swing_lookback:
        raise ValueError("Swing top count must be between 1 and swing lookback")
    expression = UNIVERSE["contracts"][contract][strategy]
    current_year = 2000 + int(contract[1:])
    history_years = int(history_years)
    if history_years < 1:
        raise ValueError("Number of historical years must be at least 1")
    series_by_year = load_expression_series(
        symbol, expression, current_year, history_years
    )
    if current_year not in series_by_year:
        raise ValueError(f"No current contract-year data for {contract}")

    current = series_by_year[current_year]
    current_upper = boundary(
        current["value"], mode, int(peak_lookback), int(peak_rank), True
    )
    current_lower = boundary(
        current["value"], mode, int(valley_lookback), int(valley_rank), False
    )
    current_swing = strongest_recent_swing_ticks(
        current["value"], tick_size, swing_lookback, swing_top_count)
    current_outcomes, entries, current_trades, current_open = backtest_range(
        current["value"], current_upper, current_lower, current_swing,
        max_parallel_positions, max_consecutive_direction, no_trade_days, max_trade_days,
        minimum_swing_ticks, tick_size,
        include_entries=True,
    )
    current_trade_rows = trade_rows(current_trades, current, config["price_decimals"])
    historical_outcomes, historical_open, historical_pnl = [], 0, 0
    yearly_results = [{
        "Year": current_year, "Wins": current_outcomes.count("TP"),
        "Resolved": len(current_outcomes), "Unresolved": current_open,
        "Win rate": f"{accuracy(current_outcomes):.1%}",
    }]
    for year, frame in series_by_year.items():
        if year >= current_year:
            continue
        upper = boundary(frame["value"], mode, int(peak_lookback), int(peak_rank), True)
        lower = boundary(frame["value"], mode, int(valley_lookback),
                         int(valley_rank), False)
        outcomes, trades, unresolved = backtest_range(
            frame["value"], upper, lower,
            strongest_recent_swing_ticks(
                frame["value"], tick_size, swing_lookback, swing_top_count),
            max_parallel_positions, max_consecutive_direction, no_trade_days,
            max_trade_days,
            minimum_swing_ticks, tick_size,
        )
        historical_outcomes.extend(outcomes)
        historical_open += unresolved
        historical_pnl += sum(trade["pnl_ticks"] or 0 for trade in trades)
        yearly_results.append({
            "Year": year, "Wins": outcomes.count("TP"), "Resolved": len(outcomes),
            "Unresolved": unresolved, "Win rate": f"{accuracy(outcomes):.1%}",
        })
    yearly_results.sort(key=lambda row: row["Year"], reverse=True)
    return {
        "symbol": symbol, "expression": expression, "current_year": current_year,
        "current": current, "upper": current_upper, "lower": current_lower,
        "swing_ticks": current_swing,
        "entries": entries, "current_outcomes": current_outcomes,
        "trade_rows": current_trade_rows,
        "current_open": current_open,
        "historical_outcomes": historical_outcomes,
        "historical_open": historical_open,
        "historical_pnl": int(historical_pnl),
        "yearly_results": yearly_results,
        "max_parallel_positions": max_parallel_positions,
        "max_consecutive_direction": max_consecutive_direction,
        "no_trade_days": no_trade_days,
        "max_trade_days": max_trade_days,
        "tick_size": tick_size, "swing_lookback": swing_lookback,
        "swing_top_count": swing_top_count,
        "minimum_swing_ticks": minimum_swing_ticks,
    }

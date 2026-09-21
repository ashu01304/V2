"""Range calculation, backtesting, and chart construction."""

from functools import lru_cache
from threading import Lock

import numpy as np
import pandas as pd
from scipy.signal import find_peaks

from analysis.seasonality import Seasonality
from analysis.contract_universe import DEFAULT_EXPIRY_PATH
from market_data import MarketData
from product_config import PRODUCT_CONFIG
from CL_CO_testing.config import *
from CL_CO_testing.universe import UNIVERSE, STRATEGIES, expression_rows

SERIES_CACHE = {}
CACHE_LOCK = Lock()


def _minute_prices(database, product, expression):
    minute = database.synthetic(product, expression).dropna(
        subset=["timestamp", "price"]
    )
    if minute.empty:
        raise ValueError(f"No minute data available for {product} {expression}")
    minute["timestamp"] = pd.to_datetime(minute["timestamp"], utc=True)
    return minute.sort_values("timestamp").set_index("timestamp")["price"]


@lru_cache(maxsize=96)
def load_intraday_series(symbol, expression, timeframe_minutes):
    timeframe_minutes = int(timeframe_minutes)
    if timeframe_minutes not in TIMEFRAME_OPTIONS:
        raise ValueError(f"Unsupported timeframe: {timeframe_minutes} minutes")
    database = MarketData()
    try:
        if symbol == "CL-CO":
            cl = _minute_prices(database, "CL", expression)
            co = _minute_prices(database, "LCO", expression)
            aligned = pd.concat({"CL": cl, "CO": co}, axis=1, join="inner").dropna()
            if aligned.empty:
                raise ValueError(f"CL and CO have no common minute data for {expression}")
            prices = aligned["CL"] - aligned["CO"]
        else:
            prices = _minute_prices(
                database, "LCO" if symbol == "CO" else symbol, expression
            )
    finally:
        database.close()
    candles = prices.resample(f"{timeframe_minutes}min").ohlc().dropna()
    if candles.empty:
        raise ValueError(
            f"No {timeframe_minutes}-minute data available for {symbol} {expression}"
        )
    return candles


def fixed_band_trades(candles, upper, lower, tick_size, tp_ticks, sl_ticks,
                      minimum_trade_gap, max_parallel_trades,
                      max_consecutive_direction, first_entry_time=None,
                      last_entry_time=None):
    """Create mean-reversion trades from prior-candle bands."""
    trades = []
    open_positions = []
    last_entry_index = None
    last_direction = None
    consecutive_direction = 0
    for index in range(1, len(candles)):
        candle = candles.iloc[index]
        timestamp = candles.index[index]
        remaining_positions = []
        closed_on_candle = False
        for position in open_positions:
            if position["direction"] == "LONG":
                hit_stop = candle["low"] <= position["stop"]
                hit_target = candle["high"] >= position["target"]
            else:
                hit_stop = candle["high"] >= position["stop"]
                hit_target = candle["low"] <= position["target"]
            if hit_stop or hit_target:
                position.update(
                    exit_time=timestamp,
                    exit_price=position["stop"] if hit_stop else position["target"],
                    outcome="SL" if hit_stop else "TP",
                )
                trades.append(position)
                closed_on_candle = True
            else:
                remaining_positions.append(position)
        open_positions = remaining_positions
        if closed_on_candle:
            continue
        if ((first_entry_time is not None and timestamp < first_entry_time)
                or (last_entry_time is not None and timestamp >= last_entry_time)):
            continue

        upper_level = upper.iloc[index - 1]
        lower_level = lower.iloc[index - 1]
        if pd.isna(upper_level) or pd.isna(lower_level):
            continue
        if len(open_positions) >= max_parallel_trades:
            continue
        if (last_entry_index is not None
                and index - last_entry_index < minimum_trade_gap):
            continue
        hit_upper = candle["low"] <= upper_level <= candle["high"]
        hit_lower = candle["low"] <= lower_level <= candle["high"]
        if hit_upper == hit_lower:
            continue
        direction = "SHORT" if hit_upper else "LONG"
        next_consecutive = (
            consecutive_direction + 1 if direction == last_direction else 1
        )
        if next_consecutive > max_consecutive_direction:
            continue
        entry = round(float(upper_level if hit_upper else lower_level), 2)
        if direction == "LONG":
            target = round(entry + tp_ticks * tick_size, 2)
            stop = round(entry - sl_ticks * tick_size, 2)
        else:
            target = round(entry - tp_ticks * tick_size, 2)
            stop = round(entry + sl_ticks * tick_size, 2)
        open_positions.append({
            "direction": direction, "entry_time": timestamp, "entry_price": entry,
            "target": target, "stop": stop,
        })
        last_entry_index = index
        last_direction = direction
        consecutive_direction = next_consecutive
    for position in open_positions:
        position.update(exit_time=candles.index[-1], exit_price=None, outcome="OPEN")
        trades.append(position)
    return trades


def ranked_bands(candles, lookback, rank):
    lookback, rank = int(lookback), int(rank)
    if lookback < 1 or rank < 1 or rank > lookback:
        raise ValueError("Band rank must be between 1 and its lookback")
    upper = candles["high"].rolling(lookback).apply(
        lambda values: np.partition(values, -rank)[-rank], raw=True
    )
    lower = candles["low"].rolling(lookback).apply(
        lambda values: np.partition(values, rank - 1)[rank - 1], raw=True
    )
    return upper, lower


def ranked_chart(result, lookback, rank, bands=None):
    lookback, rank = int(lookback), int(rank)
    upper, lower = bands or ranked_bands(result["candles"], lookback, rank)
    trades = fixed_band_trades(
        result["candles"], upper, lower, PRODUCT_CONFIG["CL"]["tick_size"],
        result["tp_ticks"], result["sl_ticks"], result["minimum_trade_gap"],
        result["max_parallel_trades"], result["max_consecutive_direction"],
        result["first_entry_time"], result["last_entry_time"],
    )
    resolved = [trade for trade in trades if trade["outcome"] != "OPEN"]
    wins = sum(trade["outcome"] == "TP" for trade in resolved)
    ranked = dict(result)
    ranked.update(rank_upper=upper, rank_lower=lower,
                  rank_lookback=lookback, band_rank=rank, trades=trades,
                  total_trades=len(trades), resolved_trades=len(resolved),
                  open_trades=len(trades) - len(resolved), wins=wins,
                  win_rate=wins / len(resolved) if resolved else None,
                  total_pnl_ticks=sum(
                      result["tp_ticks"] if trade["outcome"] == "TP"
                      else -result["sl_ticks"] for trade in resolved
                  ))
    return ranked


EXPRESSION_ROWS = expression_rows()
DEFAULT_ROWS = EXPRESSION_ROWS

DEFAULT_ROW = 0
DEFAULT_STRATEGY = next(
    strategy for strategy in STRATEGIES if DEFAULT_ROWS[DEFAULT_ROW].get(strategy)
)


@lru_cache(maxsize=128)
def calculate_chart(symbol, contract, strategy, z_score_threshold,
                    z_score_lookback, timeframe_minutes, days_before_expiry,
                    no_trade_days_before_expiry, tp_ticks, sl_ticks,
                    minimum_trade_gap, max_parallel_trades,
                    max_consecutive_direction, moving_average_lookback):
    """Load intraday candles and calculate trailing candle-based Z-score bands."""
    z_score_threshold = float(z_score_threshold)
    if z_score_threshold <= 0:
        raise ValueError("Z-score threshold must be greater than zero")
    z_score_lookback = int(z_score_lookback)
    if z_score_lookback < 2:
        raise ValueError("Z-score lookback must be at least 2 candles")
    timeframe_minutes = int(timeframe_minutes)
    days_before_expiry = int(days_before_expiry)
    no_trade_days_before_expiry = int(no_trade_days_before_expiry)
    if no_trade_days_before_expiry < 0 or days_before_expiry <= no_trade_days_before_expiry:
        raise ValueError("Days before expiry must exceed no-trade days")
    tp_ticks, sl_ticks = int(tp_ticks), int(sl_ticks)
    if tp_ticks < 1 or sl_ticks < 1:
        raise ValueError("TP and SL must each be at least 1 tick")
    minimum_trade_gap = int(minimum_trade_gap)
    if minimum_trade_gap < 1:
        raise ValueError("Minimum trade gap must be at least 1 candle")
    max_parallel_trades = int(max_parallel_trades)
    max_consecutive_direction = int(max_consecutive_direction)
    if max_parallel_trades < 1 or max_consecutive_direction < 1:
        raise ValueError("Parallel and consecutive trade limits must be at least 1")
    moving_average_lookback = int(moving_average_lookback)
    if moving_average_lookback < 1:
        raise ValueError("Moving-average lookback must be at least 1 candle")
    expression = UNIVERSE["contracts"][contract][strategy]
    candles = load_intraday_series(symbol, expression, timeframe_minutes)
    expiries = pd.read_csv(DEFAULT_EXPIRY_PATH)
    expiry = expiries.loc[
        (expiries["symbol"] == "CO") & (expiries["contract_code"] == contract),
        "expiry_date",
    ]
    if expiry.empty:
        raise ValueError(f"No CO expiry date for {contract}")
    expiry = pd.Timestamp(expiry.iloc[0], tz="UTC")
    first_entry_time = expiry - pd.Timedelta(days=days_before_expiry)
    last_entry_time = expiry - pd.Timedelta(days=no_trade_days_before_expiry)
    candle_close = candles["close"]
    candle_rolling = candle_close.rolling(
        window=z_score_lookback, min_periods=z_score_lookback
    )
    candle_mean = candle_rolling.mean()
    candle_standard_deviation = candle_rolling.std(ddof=0)
    trailing_moving_average = candles["close"].shift(1).rolling(
        window=moving_average_lookback,
        min_periods=moving_average_lookback,
    ).mean().abs()
    upper = candle_mean + z_score_threshold * candle_standard_deviation
    lower = candle_mean - z_score_threshold * candle_standard_deviation
    config = PRODUCT_CONFIG["CL" if symbol == "CL-CO" else symbol]
    trades = fixed_band_trades(
        candles, upper, lower, config["tick_size"], tp_ticks, sl_ticks,
        minimum_trade_gap, max_parallel_trades, max_consecutive_direction,
        first_entry_time, last_entry_time,
    )
    resolved_trades = [trade for trade in trades if trade["outcome"] != "OPEN"]
    wins = sum(trade["outcome"] == "TP" for trade in resolved_trades)
    total_pnl_ticks = sum(
        tp_ticks if trade["outcome"] == "TP" else -sl_ticks
        for trade in resolved_trades
    )
    return {
        "symbol": symbol,
        "expression": expression,
        "z_score_threshold": z_score_threshold,
        "z_score_lookback": z_score_lookback,
        "timeframe_minutes": timeframe_minutes,
        "expiry": expiry,
        "first_entry_time": first_entry_time,
        "last_entry_time": last_entry_time,
        "tp_ticks": tp_ticks,
        "sl_ticks": sl_ticks,
        "minimum_trade_gap": minimum_trade_gap,
        "max_parallel_trades": max_parallel_trades,
        "max_consecutive_direction": max_consecutive_direction,
        "moving_average_lookback": moving_average_lookback,
        "candles": candles,
        "trailing_moving_average": trailing_moving_average,
        "z_score_upper": upper,
        "z_score_lower": lower,
        "trades": trades,
        "total_trades": len(trades),
        "resolved_trades": len(resolved_trades),
        "open_trades": len(trades) - len(resolved_trades),
        "wins": wins,
        "win_rate": wins / len(resolved_trades) if resolved_trades else None,
        "total_pnl_ticks": total_pnl_ticks,
    }


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

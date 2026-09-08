"""Parallel CL/CO parameter sweep for available 1MDF and 1MDDF expressions."""

import argparse
import math
import os
from concurrent.futures import ProcessPoolExecutor, as_completed
from itertools import product
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.signal import find_peaks

from analysis.contract_universe import load_universe
from analysis.expression import parse_expression
from analysis.seasonality import Seasonality
from market_data import MarketData
from product_config import PRODUCT_CONFIG, SYMBOLS, TICK_SIZES


HISTORY_START_YEAR, WINDOW_DAYS = 2016, 200
SYMBOL, CL_TICK_SIZE = "CO", TICK_SIZES["CO"]  # Backward-compatible imports.
# STRATEGIES = ("1MDF", "1MDDF")
STRATEGIES = ("1MDDF",)

PARAMETER_COLUMNS = [
    "lookback", "rank", "max_parallel", "max_consecutive",
    "maximum_trade_days", "no_trade_days", "swing_lookback",
    "swing_top_count", "minimum_swing_ticks",
]


def available_expressions(symbol=None, strategies=None):
    universe = load_universe()
    selected_strategies = tuple(strategies or STRATEGIES)
    database = MarketData()
    try:
        symbols = (symbol,) if symbol else SYMBOLS
        available = {
            product: {code for code, in database.cursor.execute(
                "SELECT DISTINCT contract_code FROM seac_settlements WHERE symbol = ?",
                [product]).fetchall()}
            for product in symbols
        }
    finally:
        database.close()
    jobs = []
    for product in symbols:
        for contract, structures in universe["contracts"].items():
            for strategy in selected_strategies:
                expression = structures.get(strategy)
                if expression and all(code in available[product]
                                      for _, code in parse_expression(expression)):
                    jobs.append((product, contract, strategy, expression,
                                 2000 + int(contract[1:])))
    return jobs


def extrema_level(values, lookback, rank, peaks):
    array = values.to_numpy(dtype=float)
    confirmed = {position + 1: array[position]
                 for position in find_peaks(array if peaks else -array)[0]}
    recent, level = [], np.full(len(array), np.nan)
    for position in range(len(array)):
        if position in confirmed:
            recent = (recent + [confirmed[position]])[-lookback:]
        if len(recent) == lookback:
            level[position] = sorted(recent, reverse=peaks)[rank - 1]
    return level


def swing_ticks(values, lookback, top_count, tick_size=CL_TICK_SIZE):
    array = values.to_numpy(dtype=float)
    turning_points = sorted(set(find_peaks(array)[0]) | set(find_peaks(-array)[0]))
    confirmations = {position + 1: array[position] for position in turning_points}
    result, differences, previous, current = np.full(len(array), np.nan), [], None, np.nan
    for position in range(len(array)):
        if position in confirmations:
            value = confirmations[position]
            if previous is not None:
                differences = (differences + [abs(value - previous)])[-lookback:]
                if len(differences) == lookback:
                    average = np.mean(sorted(differences, reverse=True)[:top_count])
                    current = math.ceil((average / tick_size) ** 0.9)
            previous = value
        result[position] = current
    return result


def candidates(values, upper, lower):
    prices, result = values.to_numpy(dtype=float), {}
    for index in range(1, len(prices)):
        high, low = upper[index - 1], lower[index - 1]
        if np.isfinite(high) and np.isfinite(low):
            if prices[index - 1] < high <= prices[index]:
                result[index] = (-1, high)
            elif prices[index - 1] > low >= prices[index]:
                result[index] = (1, low)
    return result


def backtest(values, entries, swings, max_parallel, max_consecutive,
             maximum_trade_days, no_trade_days, minimum_swing_ticks,
             tick_size=CL_TICK_SIZE):
    prices, days = values.to_numpy(dtype=float), values.index.to_numpy(dtype=float)
    positions, pnls, holdings = [], [], []
    last_direction, consecutive, unresolved = None, 0, 0
    for index, price in enumerate(prices):
        open_positions = []
        for direction, target, stop, entry_index, ticks in positions:
            if price >= target if direction == 1 else price <= target:
                pnls.append(ticks); holdings.append(int(days[index] - days[entry_index]))
            elif price <= stop if direction == 1 else price >= stop:
                pnls.append(-ticks); holdings.append(int(days[index] - days[entry_index]))
            else:
                open_positions.append((direction, target, stop, entry_index, ticks))
        positions = open_positions
        candidate = entries.get(index)
        if (candidate is None or days[index] < -maximum_trade_days
                or days[index] >= -no_trade_days
                or len(positions) >= max_parallel):
            continue
        direction, entry = candidate
        if direction == last_direction and consecutive >= max_consecutive:
            continue
        ticks = swings[index - 1]
        if not np.isfinite(ticks) or ticks <= minimum_swing_ticks:
            continue
        distance = ticks * tick_size
        positions.append((direction, entry + direction * distance,
                          entry - direction * distance, index, int(ticks)))
        consecutive = consecutive + 1 if direction == last_direction else 1
        last_direction = direction
    unresolved = len(positions)
    return pnls, holdings, unresolved


def wilson_lower(wins, trades, z=1.96):
    if not trades:
        return 0.0
    rate, denominator = wins / trades, 1 + z * z / trades
    centre = rate + z * z / (2 * trades)
    spread = z * math.sqrt(rate * (1 - rate) / trades + z * z / (4 * trades * trades))
    return (centre - spread) / denominator


def evaluate_expression(job):
    symbol, contract, strategy, expression, current_year = job
    config = PRODUCT_CONFIG[symbol]["buckets"][strategy]
    tick_size = TICK_SIZES[symbol]
    database = MarketData()
    try:
        result = Seasonality(database).working_day_expression_seasonality(
            symbol, expression, HISTORY_START_YEAR, current_year, WINDOW_DAYS,
        )
    finally:
        database.close()
    series = {year: frame.sort_index() for year, frame in result.get("series", {}).items()}
    if not series:
        return [], f"{contract}/{strategy}: no data"
    prepared = {}
    for year, frame in series.items():
        values = frame["value"]
        boundaries = {(lookback, rank): candidates(
            values, extrema_level(values, lookback, rank, True),
            extrema_level(values, lookback, rank, False),
        ) for lookback, rank in product(config["lookbacks"], config["ranks"])}
        swings = {(lookback, top): swing_ticks(values, lookback, top, tick_size)
                  for lookback, top in product(config["swing_lookbacks"],
                                               config["swing_top_counts"])}
        prepared[year] = (values, boundaries, swings)

    rows = []
    grid = product(config["lookbacks"], config["ranks"], config["max_parallel"],
                   config["max_consecutive"], config["to_from"],
                   config["swing_lookbacks"],
                   config["swing_top_counts"], config["minimum_swing_ticks"])
    for parameters in grid:
        (lookback, rank, parallel, consecutive, trade_window,
         swing_lookback, top, minimum) = parameters
        maximum_trade, no_trade = trade_window
        pnls, holdings, unresolved, profitable_years, tested_years = [], [], 0, 0, 0
        for values, boundaries, swings in prepared.values():
            year_pnls, year_holds, year_open = backtest(
                values, boundaries[(lookback, rank)], swings[(swing_lookback, top)],
                parallel, consecutive, maximum_trade, no_trade, minimum, tick_size,
            )
            pnls.extend(year_pnls); holdings.extend(year_holds); unresolved += year_open
            if year_pnls:
                tested_years += 1
                profitable_years += sum(year_pnls) > 0
        wins, trades, total_pnl = sum(pnl > 0 for pnl in pnls), len(pnls), sum(pnls)
        gross_profit, gross_loss = sum(max(pnl, 0) for pnl in pnls), -sum(min(pnl, 0) for pnl in pnls)
        rows.append({
            "symbol": symbol, "contract": contract, "strategy": strategy,
            "expression": expression,
            "lookback": lookback, "rank": rank,
            "max_parallel": parallel, "max_consecutive": consecutive,
            "maximum_trade_days": maximum_trade,
            "no_trade_days": no_trade,
            "swing_lookback": swing_lookback, "swing_top_count": top,
            "minimum_swing_ticks": minimum,
            "trades": trades, "wins": wins, "losses": trades - wins,
            "unresolved": unresolved, "win_rate": wins / trades if trades else 0,
            "wilson_lower_95": wilson_lower(wins, trades), "total_pnl_ticks": total_pnl,
            "average_pnl_ticks": total_pnl / trades if trades else 0,
            "profit_factor": gross_profit / gross_loss if gross_loss else np.nan,
            "average_holding_days": np.mean(holdings) if holdings else np.nan,
            "profitable_year_rate": profitable_years / tested_years if tested_years else 0,
        })
    return rows, ""


def write_symbol_report(frame, errors, output):
    if frame.empty:
        raise RuntimeError("No parameter results were produced: " + "; ".join(errors))
    frame["win_rank"] = frame.groupby(["symbol", "contract", "strategy"])["win_rate"].rank(
        method="dense", ascending=False)
    best_expression = (frame.sort_values(
        ["symbol", "contract", "strategy", "wilson_lower_95", "win_rate", "total_pnl_ticks"],
        ascending=[True, True, True, False, False, False],
    ).groupby(["symbol", "contract", "strategy"], as_index=False).head(20))
    grouped = frame.groupby(["symbol", *PARAMETER_COLUMNS], as_index=False).agg(
        expressions=("expression", "nunique"), trades=("trades", "sum"),
        wins=("wins", "sum"), total_pnl_ticks=("total_pnl_ticks", "sum"),
        mean_expression_win_rate=("win_rate", "mean"),
        mean_profitable_year_rate=("profitable_year_rate", "mean"),
    )
    grouped["win_rate"] = grouped["wins"] / grouped["trades"].replace(0, np.nan)
    grouped["wilson_lower_95"] = [wilson_lower(w, t)
                                  for w, t in zip(grouped["wins"], grouped["trades"])]
    grouped = grouped.sort_values(
        ["wilson_lower_95", "win_rate", "total_pnl_ticks"], ascending=False
    )
    parameter_summary = pd.concat([
        frame.groupby(["symbol", column], as_index=False).agg(
            configurations=("expression", "size"), trades=("trades", "sum"),
            mean_win_rate=("win_rate", "mean"), median_win_rate=("win_rate", "median"),
            mean_wilson=("wilson_lower_95", "mean"),
            total_pnl_ticks=("total_pnl_ticks", "sum"),
        ).assign(parameter=column).rename(columns={column: "value"})
        for column in PARAMETER_COLUMNS
    ], ignore_index=True)
    notes = pd.DataFrame({"Report guide": [
        "Raw_Results contains every expression/parameter combination.",
        "Best_Expression keeps the top 20 robust results per expression.",
        "Best_Overall aggregates identical settings across all expressions.",
        "Parameter_Summary isolates each parameter value's average behaviour.",
        "wilson_lower_95 is a conservative win-rate score that penalizes tiny samples.",
        "Open trades are excluded from PnL and win rate and counted as unresolved.",
        *errors,
    ]})
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        notes.to_excel(writer, sheet_name="Guide", index=False)
        grouped.to_excel(writer, sheet_name="Best_Overall", index=False)
        best_expression.to_excel(writer, sheet_name="Best_Expression", index=False)
        parameter_summary.to_excel(writer, sheet_name="Parameter_Summary", index=False)
        for number, start in enumerate(range(0, len(frame), 1_000_000), 1):
            sheet = "Raw_Results" if number == 1 else f"Raw_Results_{number}"
            frame.iloc[start:start + 1_000_000].to_excel(
                writer, sheet_name=sheet, index=False)


def write_reports(results, errors, output_dir):
    frame = pd.DataFrame(results)
    if frame.empty:
        raise RuntimeError("No parameter results were produced: " + "; ".join(errors))
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    for (symbol, bucket), bucket_frame in frame.groupby(["symbol", "strategy"]):
        output = destination / f"{symbol}_{bucket}.xlsx"
        write_symbol_report(bucket_frame.copy(), errors, output)
        print(f"Saved {len(bucket_frame):,} {symbol} {bucket} results to {output}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    parser.add_argument("--output-dir", default="data/testing_parameters")
    arguments = parser.parse_args()
    jobs, results, errors = available_expressions(), [], []
    counts = {
        (symbol, strategy): math.prod(len(bucket_config[key]) for key in (
            "lookbacks", "ranks", "max_parallel", "max_consecutive",
            "to_from", "swing_lookbacks", "swing_top_counts",
            "minimum_swing_ticks"))
        for symbol, config in PRODUCT_CONFIG.items() if symbol in SYMBOLS
        for strategy, bucket_config in config["buckets"].items()
        if strategy in STRATEGIES
    }
    print(f"Testing {len(jobs)} expressions for {', '.join(SYMBOLS)} "
          f"with {arguments.workers} workers")
    print("Settings per expression: " + ", ".join(
        f"{symbol}/{strategy}={count:,}"
        for (symbol, strategy), count in counts.items()))
    with ProcessPoolExecutor(max_workers=arguments.workers) as executor:
        futures = {executor.submit(evaluate_expression, job): job for job in jobs}
        for completed, future in enumerate(as_completed(futures), 1):
            rows, error = future.result()
            results.extend(rows)
            if error:
                errors.append(error)
            print(f"[{completed}/{len(jobs)}] {futures[future][0]} "
                  f"{futures[future][1]} {futures[future][2]}")
    write_reports(results, errors, arguments.output_dir)


if __name__ == "__main__":
    main()

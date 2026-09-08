"""Backtest SEAC-derived levels on hourly candles and minute execution data."""

import argparse
import os
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import pandas as pd

from analysis.expiry import OfficialExpiryLookup
from analysis.expression import evaluate_expression, parse_expression
from market_data import MarketData
from product_config import TICK_SIZES
from parameter_testing.testing_parameters import (
    available_expressions, extrema_level, swing_ticks,
)


def load_shortlist(path):
    source = Path(path)
    reports = sorted(source.glob("*.xlsx")) if source.is_dir() else [source]
    frame = pd.concat(
        [pd.read_excel(report, sheet_name="All_Settings") for report in reports],
        ignore_index=True,
    )
    return {(symbol, bucket): rows.to_dict("records")
            for (symbol, bucket), rows in frame.groupby(["symbol", "bucket"])}


def working_day(timestamp, expiry):
    return -int(np.busday_count(np.datetime64(timestamp.date()),
                                np.datetime64(expiry.date())))


def finish_trade(position, timestamp, outcome):
    pnl = position["ticks"] if outcome == "TP" else -position["ticks"]
    return {
        **position, "exit_time": timestamp, "outcome": outcome,
        "pnl_ticks": pnl,
        "holding_hours": (timestamp - position["entry_time"]).total_seconds() / 3600,
    }


def settlement_expression(database, symbol, expression, expiry):
    legs = parse_expression(expression)
    histories = database.get_contract_history(symbol, [code for _, code in legs])
    missing = [code for _, code in legs if code not in histories]
    if missing:
        raise ValueError(f"missing SEAC legs: {', '.join(missing)}")
    leg_data = pd.DataFrame({code: histories[code]["Close"] for _, code in legs}).dropna()
    values = evaluate_expression(expression, leg_data)
    values.index = pd.to_datetime(values.index).tz_localize(None).normalize()
    return values[values.index <= expiry].sort_index()


def simulate(minutes, candles, settlement_dates, upper, lower, swings,
             setting, tick_size):
    positions, trades = [], []
    last_direction, consecutive = None, 0
    max_parallel = int(setting["max_parallel"])
    max_consecutive = int(setting["max_consecutive"])
    minimum_swing = int(setting["minimum_swing_ticks"])
    maximum_days = int(setting["maximum_trade_days"])
    no_trade_days = int(setting["no_trade_days"])
    expiry = setting["expiry"]
    grouped = {hour: rows for hour, rows in minutes.groupby(minutes["hour"], sort=True)}

    for index in range(1, len(candles)):
        hour = candles.index[index]
        hour_minutes = grouped.get(hour)
        if hour_minutes is None:
            continue
        # A settlement dated D is first used on D+1, because its exact publication
        # time is unavailable. This prevents intraday look-ahead.
        daily_index = settlement_dates.searchsorted(
            np.datetime64(hour.tz_localize(None).date()), side="left") - 1
        if daily_index < 0:
            continue
        upper_level, lower_level = upper[daily_index], lower[daily_index]
        ticks = swings[daily_index]
        can_signal = np.isfinite(upper_level) and np.isfinite(lower_level) and np.isfinite(ticks)
        ticks = int(ticks) if np.isfinite(ticks) else 0
        continuous = hour - candles.index[index - 1] <= pd.Timedelta(hours=1)
        previous_price = float(candles["close"].iloc[index - 1]) if continuous else None
        entered = False

        for point in hour_minutes.itertuples(index=False):
            timestamp, price = point.timestamp, float(point.price)
            still_open = []
            for position in positions:
                hit_tp = price >= position["target"] if position["direction"] == "LONG" else price <= position["target"]
                hit_sl = price <= position["stop"] if position["direction"] == "LONG" else price >= position["stop"]
                if hit_tp or hit_sl:
                    trades.append(finish_trade(position, timestamp, "TP" if hit_tp else "SL"))
                else:
                    still_open.append(position)
            positions = still_open

            day = working_day(timestamp, expiry)
            direction = entry = None
            if can_signal and not entered and -maximum_days <= day < -no_trade_days:
                if previous_price is not None and previous_price < upper_level <= price:
                    direction, entry = "SHORT", float(upper_level)
                elif previous_price is not None and previous_price > lower_level >= price:
                    direction, entry = "LONG", float(lower_level)
            if (direction and ticks > minimum_swing and len(positions) < max_parallel
                    and not (direction == last_direction and consecutive >= max_consecutive)):
                sign, distance = (1 if direction == "LONG" else -1), ticks * tick_size
                position = {
                    "direction": direction, "entry_time": timestamp,
                    "days_before_expiry": -day,
                    "hours_before_expiry": int(
                        (expiry - timestamp.tz_localize(None)).total_seconds() // 3600
                    ),
                    "entry_price": entry, "target": entry + sign * distance,
                    "stop": entry - sign * distance, "ticks": ticks,
                }
                positions.append(position)
                entered = True
                consecutive = consecutive + 1 if direction == last_direction else 1
                last_direction = direction
                if (direction == "SHORT" and price >= position["stop"]) or (
                        direction == "LONG" and price <= position["stop"]):
                    positions.pop()
                    trades.append(finish_trade(position, timestamp, "SL"))
            previous_price = price
    return trades, positions


def evaluate(job):
    symbol, contract, bucket, expression, settings = job
    minute_product = {"CO": "LCO"}.get(symbol, symbol)
    expiry = OfficialExpiryLookup().get(symbol, contract)
    if expiry is None:
        raise ValueError(f"missing expiry date for {contract}")
    expiry = pd.Timestamp(expiry).normalize()
    database = MarketData()
    try:
        minute = database.synthetic(minute_product, expression)
        settlements = settlement_expression(database, symbol, expression, expiry)
    finally:
        database.close()
    minute = minute.dropna().sort_values("timestamp")
    minute["timestamp"] = pd.to_datetime(minute["timestamp"], utc=True)
    minute["hour"] = minute["timestamp"].dt.floor("h")
    candles = minute.set_index("timestamp")["price"].resample("1h").ohlc().dropna()
    if len(candles) < 3:
        raise ValueError("fewer than three hourly candles")
    candles = candles.iloc[:-1]
    minute = minute[minute["hour"].isin(candles.index)]
    if len(settlements) < 3:
        raise ValueError("fewer than three SEAC settlement points")
    settlement_dates = settlements.index.values.astype("datetime64[D]")
    results, all_trades = [], []
    boundary_cache, swing_cache = {}, {}

    for setting in settings:
        boundary_key = (int(setting["lookback"]), int(setting["rank"]))
        if boundary_key not in boundary_cache:
            boundary_cache[boundary_key] = (
                extrema_level(settlements, *boundary_key, True),
                extrema_level(settlements, *boundary_key, False),
            )
        swing_key = (int(setting["swing_lookback"]), int(setting["swing_top_count"]))
        if swing_key not in swing_cache:
            swing_cache[swing_key] = swing_ticks(
                settlements, *swing_key, TICK_SIZES[symbol])
        upper, lower = boundary_cache[boundary_key]
        trades, open_positions = simulate(
            minute, candles, settlement_dates, upper, lower, swing_cache[swing_key],
            {**setting, "expiry": expiry}, TICK_SIZES[symbol],
        )
        pnl = [trade["pnl_ticks"] for trade in trades]
        equity = np.cumsum([0, *pnl])
        drawdown = equity - np.maximum.accumulate(equity)
        wins = sum(value > 0 for value in pnl)
        base = {
            "symbol": symbol, "bucket": bucket, "contract": contract,
            "expression": expression, "shortlist_case": int(setting["shortlist_case"]),
            "start": candles.index.min(), "end": candles.index.max(),
            "hourly_bars": len(candles), "settlement_points": len(settlements),
            "settlement_start": settlements.index.min(),
            "settlement_end": settlements.index.max(),
            "resolved": len(trades), "wins": wins,
            "losses": len(trades) - wins, "unresolved": len(open_positions),
            "win_rate": wins / len(trades) if trades else np.nan,
            "total_pnl_ticks": sum(pnl),
            "average_pnl_ticks": np.mean(pnl) if pnl else np.nan,
            "maximum_drawdown_ticks": float(drawdown.min()),
            "total_holding_hours": sum(trade["holding_hours"] for trade in trades),
            "average_holding_hours": np.mean([trade["holding_hours"] for trade in trades]) if trades else np.nan,
        }
        results.append({**base, **{key: setting[key] for key in setting if key not in base}})
        all_trades.extend({"symbol": symbol, "bucket": bucket, "contract": contract,
                           "expression": expression,
                           "shortlist_case": int(setting["shortlist_case"]), **trade}
                          for trade in trades)
    return results, all_trades


def aggregate(results):
    keys = ["symbol", "bucket", "shortlist_case", "trade_threshold",
            "required_minimum_swing_ticks", "ranking_method", "lookback", "rank",
            "max_parallel", "max_consecutive", "maximum_trade_days", "no_trade_days",
            "swing_lookback", "swing_top_count", "minimum_swing_ticks"]
    present = [key for key in keys if key in results]
    summary = results.groupby(present, as_index=False).agg(
        contracts=("expression", "count"), resolved=("resolved", "sum"),
        wins=("wins", "sum"), losses=("losses", "sum"),
        unresolved=("unresolved", "sum"), total_pnl_ticks=("total_pnl_ticks", "sum"),
        total_holding_hours=("total_holding_hours", "sum"),
        maximum_drawdown_ticks=("maximum_drawdown_ticks", "sum"),
    )
    summary["win_rate"] = summary["wins"] / summary["resolved"].replace(0, np.nan)
    summary["average_pnl_ticks"] = summary["total_pnl_ticks"] / summary["resolved"].replace(0, np.nan)
    summary["average_holding_hours"] = summary["total_holding_hours"] / summary["resolved"].replace(0, np.nan)
    return summary.sort_values(["symbol", "bucket", "total_pnl_ticks"], ascending=[True, True, False])


def write_excel(results, trades, errors, output):
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    result_frame, trade_frame = pd.DataFrame(results), pd.DataFrame(trades)
    for frame in (result_frame, trade_frame):
        for column in frame.columns:
            if isinstance(frame[column].dtype, pd.DatetimeTZDtype):
                frame[column] = frame[column].dt.tz_localize(None)
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        aggregate(result_frame).to_excel(writer, sheet_name="Setting_Summary", index=False)
        result_frame.to_excel(writer, sheet_name="Expression_Results", index=False)
        if not trade_frame.empty:
            trade_frame.to_excel(writer, sheet_name="Trades", index=False)
        pd.DataFrame({"Error": errors}).to_excel(writer, sheet_name="Errors", index=False)
    print(f"Saved {len(result_frame):,} expression-setting results and "
          f"{len(trade_frame):,} trades to {output}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--input", default="data/best_parameters"
    )
    parser.add_argument("--output", default="hourly_shortlist_results.xlsx")
    parser.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    arguments = parser.parse_args()
    cases = load_shortlist(arguments.input)
    jobs = [(symbol, contract, bucket, expression, cases[(symbol, bucket)])
            for symbol in sorted({symbol for symbol, _ in cases})
            for product, contract, bucket, expression, _ in available_expressions(
                symbol, [case_bucket for case_symbol, case_bucket in cases
                         if case_symbol == symbol])
            if product == symbol and (symbol, bucket) in cases]
    results, trades, errors = [], [], []
    with ProcessPoolExecutor(max_workers=arguments.workers) as executor:
        futures = {executor.submit(evaluate, job): job for job in jobs}
        for completed, future in enumerate(as_completed(futures), 1):
            job = futures[future]
            try:
                new_results, new_trades = future.result()
                results.extend(new_results); trades.extend(new_trades)
                print(f"[{completed}/{len(jobs)}] {job[0]} {job[1]} {job[2]}")
            except Exception as error:
                message = f"{job[0]} {job[1]} {job[2]}: {error}"
                errors.append(message); print(f"[{completed}/{len(jobs)}] SKIPPED {message}")
    if results:
        write_excel(results, trades, errors, arguments.output)
    else:
        print("No reconstructable minute expressions were found.")


if __name__ == "__main__":
    main()

"""Create latest 1MDF/1MDDF boundary and TP/SL levels for all shortlist cases."""

import argparse
import os
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import pandas as pd

from analysis.expiry import OfficialExpiryLookup
from analysis.expression import evaluate_expression, parse_expression
from market_data import MarketData
from parameter_testing.select_best_parameters import BUCKETS
from parameter_testing.testing_parameters import (
    TICK_SIZES, available_expressions, extrema_level, swing_ticks,
)


def load_shortlist(path):
    source = Path(path)
    reports = sorted(source.glob("*.xlsx")) if source.is_dir() else [source]
    frame = pd.concat(
        [pd.read_excel(report, sheet_name="All_Settings") for report in reports],
        ignore_index=True,
    )
    if "use_currently" in frame:
        frame = frame[frame["use_currently"] == 1].copy()
    if frame.empty:
        raise ValueError("No shortlist settings are marked use_currently = 1")
    frame["status"] = "READY"
    return {(symbol, bucket): rows.to_dict("records")
            for (symbol, bucket), rows in frame.groupby(["symbol", "bucket"])}


def settlement_expression(database, symbol, expression, expiry):
    legs = parse_expression(expression)
    histories = database.get_contract_history(symbol, [code for _, code in legs])
    missing = [code for _, code in legs if code not in histories]
    if missing:
        raise ValueError(f"missing SEAC legs: {', '.join(missing)}")
    leg_data = pd.DataFrame({code: histories[code]["Close"] for _, code in legs}).dropna()
    values = evaluate_expression(expression, leg_data)
    values.index = pd.to_datetime(values.index).normalize()
    return values[values.index <= expiry].sort_index()


def expression_levels(task):
    symbol, contract, bucket, expression, _, cases = task
    tick_size = TICK_SIZES[symbol]
    expiry = OfficialExpiryLookup().get(symbol, contract)
    if expiry is None:
        return [{"symbol": symbol, "bucket": bucket, "contract": contract,
                 "expression": expression, **case,
                 "status": "MISSING EXPIRY DATE"} for case in cases]
    expiry = pd.Timestamp(expiry).normalize()
    database = MarketData()
    try:
        values = settlement_expression(database, symbol, expression, expiry)
    finally:
        database.close()
    base = {"symbol": symbol, "bucket": bucket, "contract": contract,
            "expression": expression}
    if values.empty:
        return [{**base, **case, "status": "NO SEAC SETTLEMENT DATA"} for case in cases]
    latest_date = pd.Timestamp(values.index[-1])
    latest_day = -int(np.busday_count(np.datetime64(latest_date.date()),
                                      np.datetime64(expiry.date())))
    boundary_cache, swing_cache, rows = {}, {}, []
    for case in cases:
        row = {
            **base, **case, "latest_date": latest_date.strftime("%Y-%m-%d"),
            "working_days_to_expiry": latest_day,
            "latest_settlement": round(float(values.iloc[-1]), 6),
        }
        if case["status"] != "READY":
            rows.append(row)
            continue
        boundary_key = (int(case["lookback"]), int(case["rank"]))
        if boundary_key not in boundary_cache:
            boundary_cache[boundary_key] = (
                extrema_level(values, *boundary_key, True),
                extrema_level(values, *boundary_key, False),
            )
        swing_key = (int(case["swing_lookback"]), int(case["swing_top_count"]))
        if swing_key not in swing_cache:
            swing_cache[swing_key] = swing_ticks(values, *swing_key, tick_size)
        upper, lower = (levels[-1] for levels in boundary_cache[boundary_key])
        ticks = swing_cache[swing_key][-1]
        if not np.isfinite(upper) or not np.isfinite(lower) or not np.isfinite(ticks):
            row["status"] = "INSUFFICIENT LATEST HISTORY"
            rows.append(row)
            continue
        ticks, distance = int(ticks), int(ticks) * tick_size
        allowed = (-int(case["maximum_trade_days"]) <= latest_day
                   < -int(case["no_trade_days"])
                   and ticks > int(case["minimum_swing_ticks"]))
        row.update({
            "upper_bound": round(float(upper), 6),
            "lower_bound": round(float(lower), 6),
            "tp_ticks": ticks, "sl_ticks": ticks,
            "short_entry": round(float(upper), 6),
            "short_tp": round(float(upper) - distance, 6),
            "short_sl": round(float(upper) + distance, 6),
            "long_entry": round(float(lower), 6),
            "long_tp": round(float(lower) + distance, 6),
            "long_sl": round(float(lower) - distance, 6),
            "trigger_allowed": allowed,
            "status": ("READY" if allowed else
                       "BLOCKED: BEFORE START WINDOW"
                       if latest_day < -int(case["maximum_trade_days"]) else
                       "BLOCKED: EXPIRY WINDOW" if latest_day >= -int(case["no_trade_days"])
                       else "BLOCKED: SWING THRESHOLD"),
        })
        rows.append(row)
    return rows


def write_excel(rows, cases, output):
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        raise ValueError(
            "No trigger rows were produced for the marked symbols and buckets"
        )
    frame = pd.DataFrame(rows).sort_values(
        ["symbol", "bucket", "expression", "shortlist_case"])
    case_frame = pd.DataFrame([{"symbol": symbol, "bucket": bucket, **case}
                               for (symbol, bucket), bucket_cases in cases.items()
                               for case in bucket_cases])
    guide = pd.DataFrame({"Guide": [
        "Each row is one expression multiplied by one unique shortlisted setting.",
        "Upper-bound trigger is SHORT; lower-bound trigger is LONG.",
        "TP and SL use transformed swing ticks multiplied by each symbol's tick size.",
        "Repeated winning settings are retained only once per symbol and bucket.",
        "NO QUALIFYING SETTING means no tested configuration met that trade threshold.",
        "Maximum parallel/consecutive rules require live position state and are not enforced here.",
    ]})
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        guide.to_excel(writer, sheet_name="Guide", index=False)
        case_frame.to_excel(writer, sheet_name="Shortlist_Cases", index=False)
        frame.to_excel(writer, sheet_name="All_Triggers", index=False)
        for symbol in TICK_SIZES:
            for bucket in BUCKETS:
                frame[(frame["symbol"] == symbol) & (frame["bucket"] == bucket)].to_excel(
                    writer, sheet_name=f"{symbol}_{bucket}", index=False)
    print("\nRows by symbol and bucket:")
    print(frame.groupby(["symbol", "bucket"]).size().to_string())
    print(f"\nSaved {len(frame):,} trigger rows to {output}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--input", default="data/best_parameters"
    )
    parser.add_argument(
        "--output", default="data/trigger_levels/latest_trigger_levels.xlsx"
    )
    parser.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    arguments = parser.parse_args()
    cases = load_shortlist(arguments.input)
    symbols = sorted({symbol for symbol, _ in cases})
    jobs = [
        (symbol, contract, strategy, expression, current_year,
         cases[(symbol, strategy)])
        for symbol in symbols
        for product, contract, strategy, expression, current_year
        in available_expressions(
            symbol, [bucket for case_symbol, bucket in cases
                     if case_symbol == symbol]
        )
        if product == symbol and (symbol, strategy) in cases
    ]
    rows = []
    with ProcessPoolExecutor(max_workers=arguments.workers) as executor:
        futures = {executor.submit(expression_levels, job): job for job in jobs}
        for completed, future in enumerate(as_completed(futures), 1):
            rows.extend(future.result())
            print(f"[{completed}/{len(jobs)}] {futures[future][1]} {futures[future][2]}")
    write_excel(rows, cases, arguments.output)


if __name__ == "__main__":
    main()

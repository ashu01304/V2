"""Generate the Chart 2 hyperparameter report. Run manually."""

import argparse
import os
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from itertools import product
from pathlib import Path

import pandas as pd

from analysis.contract_universe import DEFAULT_EXPIRY_PATH
from CL_CO_testing.config import DEFAULT_SYMBOL
from CL_CO_testing.engine import load_intraday_series, ranked_bands, ranked_chart
from CL_CO_testing.universe import UNIVERSE, expression_rows


TIMEFRAMES = (30, 60, 240)
TP_SL = (2, 3, 4)
TRADE_WINDOWS = ((150, 40), (150, 60), (130, 40),
                 (130, 60), (110, 50))
MINIMUM_GAPS = (5, 7)
MAX_PARALLEL = (4, 6)
MAX_CONSECUTIVE = (1, 2, 3)
LOOKBACKS = (35, 25)
RANKS = (5, 8, 12)
OUTPUT = Path(__file__).with_name("chart_2_parameter_report.xlsx")
CONTRACTS = [row["Contract"] for row in expression_rows()[:7]]
WORKER_INPUTS = None
WORKER_BANDS = None


def wilson_lower(wins, trades, z=1.96):
    if not trades:
        return 0.0
    rate, denominator = wins / trades, 1 + z * z / trades
    centre = rate + z * z / (2 * trades)
    spread = z * (rate * (1 - rate) / trades + z * z / (4 * trades**2)) ** 0.5
    return (centre - spread) / denominator


def drawdown(trades, tp_sl):
    pnl = [tp_sl if trade["outcome"] == "TP" else -tp_sl
           for trade in sorted(trades, key=lambda item: item["entry_time"])
           if trade["outcome"] != "OPEN"]
    equity = peak = worst = 0
    for value in pnl:
        equity += value
        peak = max(peak, equity)
        worst = max(worst, peak - equity)
    return worst


def load_inputs():
    expiry_frame = pd.read_csv(DEFAULT_EXPIRY_PATH)
    expiries = expiry_frame[expiry_frame["symbol"] == "CO"].set_index(
        "contract_code"
    )["expiry_date"]
    inputs = {}
    for timeframe, contract in product(TIMEFRAMES, CONTRACTS):
        expression = UNIVERSE["contracts"][contract]["1MF"]
        expiry = pd.Timestamp(expiries[contract], tz="UTC")
        inputs[timeframe, contract] = (
            load_intraday_series(DEFAULT_SYMBOL, expression, timeframe), expiry
        )
    return inputs


def initialize_worker():
    global WORKER_INPUTS, WORKER_BANDS
    WORKER_INPUTS = load_inputs()
    WORKER_BANDS = {}


def run_case(case):
    run_id, values = case
    timeframe, tp_sl, window, gap, parallel, consecutive, lookback, rank = values
    days_before, no_trade_days = window
    results = []
    for contract in CONTRACTS:
        candles, expiry = WORKER_INPUTS[timeframe, contract]
        band_key = timeframe, contract, lookback, rank
        if band_key not in WORKER_BANDS:
            WORKER_BANDS[band_key] = ranked_bands(candles, lookback, rank)
        result = ranked_chart({
            "candles": candles, "tp_ticks": tp_sl, "sl_ticks": tp_sl,
            "minimum_trade_gap": gap, "max_parallel_trades": parallel,
            "max_consecutive_direction": consecutive,
            "first_entry_time": expiry - pd.Timedelta(days=days_before),
            "last_entry_time": expiry - pd.Timedelta(days=no_trade_days),
        }, lookback, rank, WORKER_BANDS[band_key])
        row = {
            "run_id": run_id, "contract": contract, "timeframe": timeframe,
            "tp_sl_ticks": tp_sl, "days_before_expiry": days_before,
            "no_trade_days": no_trade_days, "minimum_trade_gap": gap,
            "max_parallel": parallel, "max_consecutive": consecutive,
            "rank_lookback": lookback, "band_rank": rank,
            "trades": result["resolved_trades"], "wins": result["wins"],
            "open_trades": result["open_trades"],
            "total_pnl_ticks": result["total_pnl_ticks"],
            "max_drawdown_ticks": drawdown(result["trades"], tp_sl),
        }
        row["win_rate"] = row["wins"] / row["trades"] if row["trades"] else None
        results.append(row)
    trades = sum(row["trades"] for row in results)
    wins = sum(row["wins"] for row in results)
    aggregate = {
        **{key: results[0][key] for key in (
            "run_id", "timeframe", "tp_sl_ticks", "days_before_expiry",
            "no_trade_days", "minimum_trade_gap", "max_parallel",
            "max_consecutive", "rank_lookback", "band_rank")},
        "contracts": len(results), "trades": trades, "wins": wins,
        "win_rate": wins / trades if trades else None,
        "wilson_lower_95": wilson_lower(wins, trades),
        "total_pnl_ticks": sum(row["total_pnl_ticks"] for row in results),
        "max_drawdown_ticks": sum(row["max_drawdown_ticks"] for row in results),
        "profitable_contract_rate": sum(row["total_pnl_ticks"] > 0
                                         for row in results) / len(results),
        "worst_contract_pnl": min(row["total_pnl_ticks"] for row in results),
    }
    return aggregate, results


def generate_report(output=OUTPUT, workers=None):
    raw, contract_rows = [], []
    cases = list(enumerate(product(
        TIMEFRAMES, TP_SL, TRADE_WINDOWS, MINIMUM_GAPS, MAX_PARALLEL,
        MAX_CONSECUTIVE, LOOKBACKS, RANKS,
    ), 1))
    workers = workers or min(8, os.cpu_count() or 1)
    started, update_every = time.perf_counter(), max(1, len(cases) // 100)
    print(f"Running {len(cases):,} parameter sets with {workers} workers...")
    with ProcessPoolExecutor(max_workers=workers,
                             initializer=initialize_worker) as executor:
        futures = [executor.submit(run_case, case) for case in cases]
        for completed, future in enumerate(as_completed(futures), 1):
            aggregate, rows = future.result()
            raw.append(aggregate)
            contract_rows.extend(rows)
            if completed % update_every == 0 or completed == len(cases):
                elapsed = time.perf_counter() - started
                rate = completed / elapsed
                eta = (len(cases) - completed) / rate
                print(f"{completed:,}/{len(cases):,} ({completed / len(cases):.0%}) | "
                      f"{rate:.1f} sets/s | elapsed {elapsed / 60:.1f}m | "
                      f"ETA {eta / 60:.1f}m", flush=True)
    raw = pd.DataFrame(raw)
    best = pd.concat([
        raw.nlargest(25, column).assign(ranking=column)
        for column in ("wilson_lower_95", "win_rate", "total_pnl_ticks",
                       "profitable_contract_rate")
    ]).drop_duplicates("run_id")
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        raw.to_excel(writer, sheet_name="Raw_Results", index=False)
        pd.DataFrame(contract_rows).to_excel(writer, sheet_name="Contract_Results",
                                             index=False)
        best.to_excel(writer, sheet_name="Best_Results", index=False)
    print(f"Saved {len(raw):,} parameter sets to {output}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=None)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    arguments = parser.parse_args()
    generate_report(arguments.output, arguments.workers)

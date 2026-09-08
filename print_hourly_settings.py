"""Print profitable settings from the hourly shortlist report."""

import argparse

import pandas as pd


PARAMETERS = [
    "lookback", "rank", "max_parallel", "max_consecutive",
    "maximum_trade_days", "no_trade_days", "swing_lookback",
    "swing_top_count", "minimum_swing_ticks",
]


def wilson_lower(wins, trades, z=1.96):
    if not trades:
        return 0.0
    rate, denominator = wins / trades, 1 + z * z / trades
    centre = rate + z * z / (2 * trades)
    spread = z * ((rate * (1 - rate) / trades + z * z / (4 * trades * trades)) ** 0.5)
    return (centre - spread) / denominator


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="hourly_shortlist_results.xlsx")
    parser.add_argument("--minimum-average-pnl", type=float, default=3.5)
    parser.add_argument("--minimum-win-rate", type=float, default=70.0,
                        help="Percentage, for example 70 means 70%%")
    parser.add_argument("--output", default="final_goog_settings_forCL.xlsx")
    arguments = parser.parse_args()

    frame = pd.read_excel(arguments.input, sheet_name="Setting_Summary")
    selected = frame[
        (frame["symbol"] == "CL")
        & (frame["average_pnl_ticks"] >= arguments.minimum_average_pnl)
        & (frame["win_rate"] >= arguments.minimum_win_rate / 100)
    ].sort_values(["average_pnl_ticks", "win_rate", "total_pnl_ticks"],
                  ascending=False).drop_duplicates(["symbol", "bucket", *PARAMETERS])

    if selected.empty:
        print("No settings meet both requirements.")
        return

    selected = selected.copy()
    selected["selected_win_rate"] = selected["win_rate"]
    selected["selected_robust_win_rate"] = [
        wilson_lower(wins, trades)
        for wins, trades in zip(selected["wins"], selected["resolved"])
    ]
    selected["selected_trades"] = selected["resolved"]
    selected["selected_total_pnl_ticks"] = selected["total_pnl_ticks"]
    selected["status"] = "READY"
    selected["shortlist_case"] = selected.groupby(["symbol", "bucket"]).cumcount() + 1
    with pd.ExcelWriter(arguments.output, engine="openpyxl") as writer:
        selected.to_excel(writer, sheet_name="All_Settings", index=False)
        for (symbol, bucket), rows in selected.groupby(["symbol", "bucket"]):
            rows.to_excel(writer, sheet_name=f"{symbol}_{bucket}", index=False)

    print(f"{len(selected)} settings: average PnL >= "
          f"{arguments.minimum_average_pnl:g} ticks and win rate >= "
          f"{arguments.minimum_win_rate:g}%\n")
    for _, row in selected.iterrows():
        settings = " | ".join(f"{name}={row[name]:g}" for name in PARAMETERS)
        print(
            f"{row['symbol']} {row['bucket']} S{int(row['shortlist_case'])} | "
            f"average PnL={row['average_pnl_ticks']:.2f} ticks | "
            f"win rate={row['win_rate']:.1%} | resolved={row['resolved']:,.0f} | "
            f"total PnL={row['total_pnl_ticks']:+,.0f} ticks\n  {settings}"
        )
    print(f"\nSaved to {arguments.output}")


if __name__ == "__main__":
    main()

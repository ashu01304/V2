from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd


def write_walk_forward_report(trades, symbol, configuration):
    output_dir = Path("backtest_reports")
    output_dir.mkdir(exist_ok=True)
    path = output_dir / f"walk_forward_{symbol}_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
    trades = trades.sort_values(["Expression", "Entry_Date"]).reset_index(drop=True)
    summary = pd.DataFrame([_overall(trades)])
    with pd.ExcelWriter(path) as writer:
        summary.to_excel(writer, sheet_name="Summary", index=False)
        trades.to_excel(writer, sheet_name="Trades", index=False)
        _grouped(trades, "Expression").to_excel(writer, sheet_name="By_Expression", index=False)
        _grouped(trades, "Strategy").to_excel(writer, sheet_name="By_Strategy", index=False)
        _grouped(trades, "Test_Year").to_excel(writer, sheet_name="By_Year", index=False)
        _grouped(trades, "Signal").to_excel(writer, sheet_name="By_Side", index=False)
        _grouped(trades, "Exit_Reason").to_excel(writer, sheet_name="By_Exit", index=False)
        pd.DataFrame({"Parameter": configuration.keys(),
                      "Value": configuration.values()}).to_excel(
            writer, sheet_name="Configuration", index=False
        )
    return path.resolve()


def _overall(trades):
    winners = trades["Price_Move"] > 0
    gross_profit = trades.loc[winners, "Price_Move"].sum()
    gross_loss = -trades.loc[~winners, "Price_Move"].sum()
    cumulative = trades.sort_values("Exit_Date")["Price_Move"].cumsum()
    drawdown = cumulative - cumulative.cummax()
    return {
        "Trades": len(trades), "Winners": int(winners.sum()),
        "Win_Rate": winners.mean() * 100, "Total_Move": trades["Price_Move"].sum(),
        "Average_Move": trades["Price_Move"].mean(),
        "Median_Move": trades["Price_Move"].median(),
        "Profit_Factor": np.inf if gross_loss == 0 else gross_profit / gross_loss,
        "Best_Trade": trades["Price_Move"].max(),
        "Worst_Trade": trades["Price_Move"].min(),
        "Max_Drawdown": drawdown.min(), "Average_Hold": trades["Days_Held"].mean(),
    }


def _grouped(trades, column):
    rows = []
    for value, group in trades.groupby(column, dropna=False):
        row = {column: value, **_overall(group)}
        row.pop("Max_Drawdown", None)
        rows.append(row)
    return pd.DataFrame(rows).sort_values(
        ["Win_Rate", "Total_Move"], ascending=False
    ) if rows else pd.DataFrame()

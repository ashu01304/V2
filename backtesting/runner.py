import pandas as pd

from analysis.seasonality import Seasonality
from backtesting.engine import SeasonalBacktester
from backtesting.risk_engine import RiskManagedBacktester
from database.manager import DatabaseManager


def run_backtest(symbol, data_start_year, test_start_year, data_end_year, window_days,
                 strategy_func, features, strategy_config, engine="risk",
                 engine_config=None, expression=None, minimum_latest_score=0,
                 minimum_trend_score=0, scores_file="temp.xlsx",
                 seasonality_function="working_day_expression_seasonality",
                 output_file="backtest_report.xlsx"):
    engine_config = engine_config or {}
    if expression:
        universe = pd.DataFrame({"Expression": [expression]})
    else:
        universe = pd.read_excel(scores_file)
        universe = universe[(universe["Latest_Score"] >= minimum_latest_score)
                            & (universe["Trend_Score"] >= minimum_trend_score)]

    all_trades, errors = [], []
    db = DatabaseManager()
    try:
        fetch = getattr(Seasonality(db), seasonality_function)
        for _, row in universe.iterrows():
            current_expression = row["Expression"]
            try:
                result = fetch(symbol, current_expression, start_year=data_start_year,
                               end_year=data_end_year, window_days=window_days)
                if result["combined"].empty:
                    raise ValueError("No seasonality data")

                if engine == "risk":
                    trades = RiskManagedBacktester(result).run_walk_forward(
                        strategy_func, data_start_year, test_start_year,
                        features=features, strategy_config=strategy_config, **engine_config
                    )
                elif engine == "fixed":
                    trades = SeasonalBacktester(result).run_walk_forward(
                        strategy_func, data_start_year, test_start_year,
                        features=features, strategy_config=strategy_config
                    )
                else:
                    raise ValueError("engine must be 'risk' or 'fixed'")

                if not trades.empty:
                    trades.insert(0, "Expression", current_expression)
                    for column in ("Latest_Score", "Trend_Score", "Neighboring_Buckets_Score"):
                        if column in row:
                            trades[column] = row[column]
                    all_trades.append(trades)
            except Exception as error:
                errors.append({"Expression": current_expression, "Error": str(error)})
    finally:
        db.close()

    trades = pd.concat(all_trades, ignore_index=True) if all_trades else pd.DataFrame()
    errors = pd.DataFrame(errors)
    expression_summary = _summary(trades, "Expression")
    yearly_summary = _summary(trades, "Test_Year")
    exit_summary = _summary(trades, "Exit_Reason") if "Exit_Reason" in trades else pd.DataFrame()
    config = pd.DataFrame({"Parameter": ["symbol", "engine", "data_start_year", "test_start_year",
                                         "data_end_year", "window_days", "strategy", "features",
                                         "strategy_config", "engine_config"],
                           "Value": [symbol, engine, data_start_year, test_start_year, data_end_year,
                                     window_days, strategy_func.__name__, str(features),
                                     str(strategy_config), str(engine_config)]})

    with pd.ExcelWriter(output_file) as writer:
        trades.to_excel(writer, sheet_name="Trades", index=False)
        expression_summary.to_excel(writer, sheet_name="By_Expression", index=False)
        yearly_summary.to_excel(writer, sheet_name="By_Year", index=False)
        exit_summary.to_excel(writer, sheet_name="By_Exit", index=False)
        errors.to_excel(writer, sheet_name="Errors", index=False)
        config.to_excel(writer, sheet_name="Configuration", index=False)

    return {"trades": trades, "expression_summary": expression_summary,
            "yearly_summary": yearly_summary, "exit_summary": exit_summary,
            "errors": errors, "configuration": config}


def _summary(trades, group):
    if trades.empty or group not in trades:
        return pd.DataFrame()
    hold = "Days_Held" if "Days_Held" in trades else "Duration"
    summary = trades.groupby(group, as_index=False).agg(
        Trades=("Price_Move", "size"), Winners=("Success", "sum"),
        Success_Rate=("Success", "mean"), Total_Move=("Price_Move", "sum"),
        Average_Move=("Price_Move", "mean"), Average_Hold=(hold, "mean"))
    summary["Success_Rate"] *= 100
    return summary

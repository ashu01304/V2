import os

os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

import pandas as pd

from analysis.seasonality import Seasonality
from backtesting.runner import run_backtest
from backtesting.sl_tp_strategies import SLTPStrategies
from backtesting.strategies import SeasonalStrategies
from backtesting.trade_rules import get_trade_rules
from market_data import MarketData


def run_expression_batch(symbol, items, analysis_years, test_years,
                         transaction_cost, stop_before_expiry, exit_method,
                         signal_model="seasonal_sd"):
    trades_out, skipped = [], 0
    bracket = "15Y" if analysis_years >= 11 else "10Y" if analysis_years >= 6 else "5Y"
    database = MarketData()
    seasonality = Seasonality(database)
    try:
        for item in items:
            anchor_year = 2000 + int(item["contract"][1:])
            test_start_year = anchor_year - test_years + 1
            data_start_year = test_start_year - analysis_years
            rules = get_trade_rules(symbol, item["strategy"], exit_method)
            momentum = signal_model == "momentum"
            mean_reversion = signal_model == "mean_reversion"
            try:
                seasonal_result = seasonality.working_day_expression_seasonality(
                    symbol, item["expression"],
                    start_year=data_start_year,
                    end_year=anchor_year, window_days=400
                )
                result = run_backtest(
                    symbol=symbol, data_start_year=data_start_year,
                    test_start_year=test_start_year,
                    data_end_year=anchor_year, window_days=400,
                    strategy_func=(SeasonalStrategies.current_year_momentum if momentum else
                                   SeasonalStrategies.current_year_mean_reversion
                                   if mean_reversion else SeasonalStrategies.strict_research),
                    features=({"LIVE_STD": {"window": 20},
                               "LIVE_MEAN_DISTANCE": {"window": 20}}
                              if mean_reversion else
                              {"LIVE_STD": {"window": 20}} if momentum else
                              {f"UpRate_{bracket}_10D": {},
                               f"Expected_{bracket}_10D": {},
                               "TECH_ZScore": {"window": 20},
                               "LIVE_STD": {"window": 20}}),
                    strategy_config=({"holding_days": 10,
                                      "stop_before_expiry": stop_before_expiry,
                                      "minimum_expected_move": rules["minimum_expected_move"]}
                                     if momentum or mean_reversion else
                                     {"holding_days": 10,
                                      "stop_before_expiry": stop_before_expiry,
                                      "seasonality_bracket": bracket,
                                      "minimum_expected_move": rules["minimum_expected_move"],
                                      "entry_zscore": rules["entry_zscore"]}),
                    expression=item["expression"],
                    engine_config={
                        "max_hold_days": rules["max_hold_days"],
                        "history_years": analysis_years,
                        "transaction_cost": transaction_cost,
                        "min_reward_risk": rules["minimum_reward_risk"],
                        "breakeven_after_r": rules["breakeven_after_r"],
                        "not_working_days": rules["not_working_days"],
                        "stop_slippage": rules["stop_slippage"],
                        "zscore_exit": 3.0 if mean_reversion else None,
                        "sl_tp_strategy": (SLTPStrategies.time_only if rules["time_only"] else
                                           SLTPStrategies.feature_based),
                        "sl_tp_config": ({} if rules["time_only"] else {
                                             "stop_multiplier": rules["stop_multiplier"],
                                             "max_stop": rules["max_stop"],
                                             "max_target": rules["max_target"],
                                             "target_feature": ("LIVE_MEAN_DISTANCE"
                                                                if mean_reversion else None),
                                             "take_profit_multiplier": (1.0 if mean_reversion
                                                                        else rules["target_multiplier"]),
                                         }),
                        "exit_price_method": "exact" if rules["exact_stop_fill"] else "market",
                    },
                    output_file=None, database=database,
                    seasonality_result=seasonal_result,
                )
                trades = result["trades"]
                if not trades.empty:
                    trades.insert(0, "Contract", item["contract"])
                    trades.insert(1, "Strategy", item["strategy"])
                    trades_out.append(trades)
            except Exception:
                skipped += 1
    finally:
        database.close()
    return (pd.concat(trades_out, ignore_index=True)
            if trades_out else pd.DataFrame()), skipped

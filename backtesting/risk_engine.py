import pandas as pd

from analysis.feature_creation import FeatureCreator
from backtesting.sl_tp_strategies import SLTPStrategies


class RiskManagedBacktester:
    def __init__(self, result_dict):
        self.result, self.matrix, self.fc = result_dict, result_dict["combined"], FeatureCreator()

    def run_walk_forward(self, strategy_func, data_start_year, test_start_year, max_hold_days,
                         stop_loss=None, take_profit=None, features=None, strategy_config=None, sl_tp_strategy=SLTPStrategies.fixed,
                         sl_tp_config=None, exit_price_method="market", transaction_cost=0.0,
                         min_reward_risk=0.0, breakeven_after_r=None,
                         not_working_days=None, history_years=None,
                         stop_slippage=0.0, zscore_exit=None):
        features = features or {"TECH_ZScore": {"window": 20}, "TECH_RSI": {"window": 14}}
        strategy_config = strategy_config or {}
        sl_tp_config = sl_tp_config or {"stop_loss": stop_loss or 0.03,
                                        "take_profit": take_profit or 0.03}
        trades = []
        years = sorted(y for y in self.matrix.columns if y >= data_start_year)

        for test_year in (y for y in years if y >= test_start_year):
            history = [y for y in years if y < test_year]
            if history_years:
                history = history[-history_years:]
            if not history:
                continue
            historical_names = {name: config for name, config in features.items()
                                if not name.startswith(("TECH_", "LIVE_"))}
            rulebook = self.fc.create_historical_features(
                self.matrix[history + [test_year]], historical_names
            )
            prices = self.matrix[test_year]
            dates = self.result["series"][test_year]["date"].reindex(self.matrix.index).interpolate()
            position = None

            for i, day in enumerate(self.matrix.index):
                exited_today = False
                price = prices.loc[day]
                if pd.isna(price):
                    continue

                if position:
                    pnl = price - position["Entry_Price"] if position["Signal"] == "LONG" else position["Entry_Price"] - price
                    position["MFE"] = max(position["MFE"], pnl)
                    position["MAE"] = min(position["MAE"], pnl)
                    held = day - position["Day_to_Expiry"]
                    if (breakeven_after_r is not None and
                            position["MFE"] >= breakeven_after_r * position["Initial_Stop"]):
                        position["Breakeven_Active"] = True
                    stop_hit = (pnl <= 0 if position["Breakeven_Active"]
                                else pnl <= -position["Stop_Loss"])
                    adverse_zscore = False
                    if zscore_exit is not None:
                        live = prices.iloc[:i + 1].dropna().tail(20)
                        live_std = live.std()
                        live_zscore = ((live.iloc[-1] - live.mean()) / live_std
                                       if pd.notna(live_std) and live_std else 0)
                        adverse_zscore = ((position["Signal"] == "LONG" and
                                           live_zscore <= -zscore_exit) or
                                          (position["Signal"] == "SHORT" and
                                           live_zscore >= zscore_exit))
                    reason = ("TAKE_PROFIT" if pnl >= position["Take_Profit"] else
                              "BREAKEVEN" if stop_hit and position["Breakeven_Active"] else
                              "STOP_LOSS" if stop_hit else
                              "SIGNAL_INVALIDATION" if adverse_zscore else
                              "NOT_WORKING" if not_working_days is not None and
                              held >= not_working_days and pnl <= 0 else
                              "MAX_HOLD" if held >= position["Max_Hold"] else None)
                    if reason:
                        exit_price = price
                        if exit_price_method == "exact" and reason in ("TAKE_PROFIT", "STOP_LOSS", "BREAKEVEN"):
                            pnl = (position["Take_Profit"] if reason == "TAKE_PROFIT" else
                                   0.0 if reason == "BREAKEVEN" else
                                   -(position["Stop_Loss"] + stop_slippage))
                            exit_price = position["Entry_Price"] + pnl if position["Signal"] == "LONG" else position["Entry_Price"] - pnl
                        position.update(Exit_Date=dates.loc[day].strftime("%Y-%m-%d"), Exit_Day=day,
                                        Exit_Price=exit_price, Days_Held=held,
                                        Gross_Move=pnl, Cost=transaction_cost,
                                        Price_Move=pnl - transaction_cost,
                                        Exit_Reason=reason, Success=int(pnl - transaction_cost > 0))
                        trades.append(position)
                        position = None
                        exited_today = True

                if position is None and not exited_today:
                    trailing = prices.iloc[:i + 1].dropna()
                    context = pd.concat([rulebook.loc[day], self.fc.create_live_features(trailing, features)])
                    context["DAYS_TO_EXPIRY"] = abs(day)
                    signal, signal_hold = strategy_func(context, trailing, **strategy_config)
                    hold = int(signal_hold or max_hold_days)
                    if signal in ("LONG", "SHORT") and day + hold <= self.matrix.index.max():
                        stop_loss, take_profit = sl_tp_strategy(context, **sl_tp_config)
                        realised_risk = stop_loss + stop_slippage
                        if stop_loss <= 0 or take_profit / realised_risk < min_reward_risk:
                            continue
                        position = dict(
                            Signal_Date=dates.loc[day].strftime("%Y-%m-%d"),
                            Entry_Date=dates.loc[day].strftime("%Y-%m-%d"),
                            Day_to_Expiry=day, Entry_Price=price,
                            Test_Year=test_year, Signal=signal,
                            Max_Hold=hold, Stop_Loss=stop_loss,
                            Initial_Stop=stop_loss, Take_Profit=take_profit,
                            Breakeven_Active=False, MFE=0.0, MAE=0.0,
                        )
                        position.update(context.to_dict())
            if position:
                available = prices.dropna()
                final_day, final_price = available.index[-1], available.iloc[-1]
                pnl = (final_price - position["Entry_Price"] if position["Signal"] == "LONG"
                       else position["Entry_Price"] - final_price)
                position.update(Exit_Date=dates.loc[final_day].strftime("%Y-%m-%d"),
                                Exit_Day=final_day, Exit_Price=final_price,
                                Days_Held=final_day - position["Day_to_Expiry"],
                                Gross_Move=pnl, Cost=transaction_cost,
                                Price_Move=pnl - transaction_cost, Exit_Reason="END_OF_DATA",
                                Success=int(pnl - transaction_cost > 0))
                trades.append(position)
        return pd.DataFrame(trades)

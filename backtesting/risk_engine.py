import pandas as pd

from analysis.feature_creation import FeatureCreator
from backtesting.sl_tp_strategies import SLTPStrategies


class RiskManagedBacktester:
    def __init__(self, result_dict):
        self.result, self.matrix, self.fc = result_dict, result_dict["combined"], FeatureCreator()

    def run_walk_forward(self, strategy_func, data_start_year, test_start_year, max_hold_days,
                         stop_loss=None, take_profit=None, features=None, strategy_config=None, sl_tp_strategy=SLTPStrategies.fixed,
                         sl_tp_config=None, exit_price_method="exact"):
        features = features or {"TECH_ZScore": {"window": 20}, "TECH_RSI": {"window": 14}}
        strategy_config = strategy_config or {}
        sl_tp_config = sl_tp_config or {"stop_loss": stop_loss or 0.03,
                                        "take_profit": take_profit or 0.03}
        trades = []
        years = sorted(y for y in self.matrix.columns if y >= data_start_year)

        for test_year in (y for y in years if y >= test_start_year):
            history = [y for y in years if y < test_year]
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
                price = prices.loc[day]
                if pd.isna(price):
                    continue

                if position:
                    pnl = price - position["Entry_Price"] if position["Signal"] == "LONG" else position["Entry_Price"] - price
                    held = day - position["Day_to_Expiry"]
                    reason = "TAKE_PROFIT" if pnl >= position["Take_Profit"] else "STOP_LOSS" if pnl <= -position["Stop_Loss"] else "MAX_HOLD" if held >= max_hold_days else None
                    if reason:
                        exit_price = price
                        if exit_price_method == "exact" and reason != "MAX_HOLD":
                            pnl = position["Take_Profit"] if reason == "TAKE_PROFIT" else -position["Stop_Loss"]
                            exit_price = position["Entry_Price"] + pnl if position["Signal"] == "LONG" else position["Entry_Price"] - pnl
                        position.update(Exit_Date=dates.loc[day].strftime("%Y-%m-%d"), Exit_Day=day,
                                        Exit_Price=exit_price, Days_Held=held, Price_Move=pnl,
                                        Exit_Reason=reason, Success=int(pnl > 0))
                        trades.append(position)
                        position = None

                if position is None:
                    trailing = prices.iloc[:i + 1].dropna()
                    context = pd.concat([rulebook.loc[day], self.fc.create_live_features(trailing, features)])
                    context["DAYS_TO_EXPIRY"] = abs(day)
                    signal, _ = strategy_func(context, trailing, **strategy_config)
                    if signal in ("LONG", "SHORT") and day + max_hold_days <= self.matrix.index.max():
                        stop_loss, take_profit = sl_tp_strategy(context, **sl_tp_config)
                        position = dict(Entry_Date=dates.loc[day].strftime("%Y-%m-%d"), Test_Year=test_year,
                                        Day_to_Expiry=day, Signal=signal, Entry_Price=price,
                                        Max_Hold=max_hold_days, Stop_Loss=stop_loss, Take_Profit=take_profit)
                        position.update(context.to_dict())
        return pd.DataFrame(trades)

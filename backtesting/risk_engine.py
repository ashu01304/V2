import pandas as pd

from analysis.feature_creation import FeatureCreator


class RiskManagedBacktester:
    def __init__(self, result_dict):
        self.result, self.matrix, self.fc = result_dict, result_dict["combined"], FeatureCreator()

    def run_walk_forward(self, strategy_func, data_start_year, test_start_year,
                         max_hold_days, stop_loss, take_profit):
        trades = []
        years = sorted(y for y in self.matrix.columns if y >= data_start_year)
        for test_year in (y for y in years if y >= test_start_year):
            history = [y for y in years if y < test_year]
            if not history:
                continue
            feature_matrix = self.matrix[history + [test_year]]
            cleaned = self.fc.get_cleaned_averages(
                feature_matrix, self.fc.get_anomaly_years(feature_matrix)
            )
            rulebook = cleaned.join(self.fc.calculate_stats(feature_matrix))
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
                    reason = "TAKE_PROFIT" if pnl >= take_profit else "STOP_LOSS" if pnl <= -stop_loss else "MAX_HOLD" if held >= max_hold_days else None
                    if reason:
                        position.update(Exit_Date=dates.loc[day].strftime("%Y-%m-%d"), Exit_Day=day,
                                        Exit_Price=price, Days_Held=held, Price_Move=pnl,
                                        Exit_Reason=reason, Success=int(pnl > 0))
                        trades.append(position)
                        position = None
                        continue

                if position is None:
                    trailing = prices.iloc[:i + 1].dropna()
                    context = pd.concat([rulebook.loc[day].copy(), self.fc.calculate_live_technical_features(trailing)])
                    context["DAYS_TO_EXPIRY"] = abs(day)
                    signal, _ = strategy_func(context, trailing)
                    if signal in ("LONG", "SHORT") and day + max_hold_days <= self.matrix.index.max():
                        position = dict(Entry_Date=dates.loc[day].strftime("%Y-%m-%d"), Test_Year=test_year,
                                        Day_to_Expiry=day, Signal=signal, Entry_Price=price,
                                        Max_Hold=max_hold_days, Stop_Loss=stop_loss, Take_Profit=take_profit)
                        position.update(context.to_dict())
        return pd.DataFrame(trades)

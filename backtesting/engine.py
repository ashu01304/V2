import pandas as pd
import numpy as np
from analysis.feature_creation import FeatureCreator

class SeasonalBacktester:
    def __init__(self, result_dict):
        self.result = result_dict
        self.full_matrix = result_dict['combined']
        self.fc = FeatureCreator()

    def run_walk_forward(self, strategy_func, data_start_year, test_start_year):
        all_trades = []
        available_years = sorted([y for y in self.full_matrix.columns if y >= data_start_year])
        end_year = available_years[-1]

        for test_year in range(test_start_year, end_year + 1):
            if test_year not in available_years: continue
            
            # A. Build History Features (Rulebook)
            hist_pool = [y for y in available_years if y < test_year]
            if not hist_pool: continue
            hist_matrix = self.full_matrix[hist_pool]
            rulebook = self.fc.get_cleaned_averages(hist_matrix, self.fc.get_anomaly_years(hist_matrix)).join(self.fc.calculate_stats(hist_matrix))

            # B. Get Test Year Data
            live_price = self.full_matrix[test_year]
            date_map = self.result['series'][test_year]['date']

            # C. Vectorized Strategy Call
            # Returns Series of signals (1, -1, 0) and durations for the WHOLE YEAR
            signals, durations = strategy_func(rulebook, live_price)

            # D. Vectorized Outcome Logic
            # Identify indices where a trade was triggered
            trigger_days = signals[signals != 0].index
            
            for day in trigger_days:
                sig = signals.loc[day]
                dur = durations.loc[day]
                exit_day = day + dur
                
                if exit_day in live_price.index and not pd.isna(live_price.loc[exit_day]):
                    entry_p = live_price.loc[day]
                    exit_p = live_price.loc[exit_day]
                    pnl = (exit_p - entry_p) if sig == 1 else (entry_p - exit_p)
                    
                    trade_log = {
                        'Entry_Date': date_map.loc[day].strftime('%Y-%m-%d'),
                        'Test_Year': test_year,
                        'Day_to_Expiry': day,
                        'Signal': "LONG" if sig == 1 else "SHORT",
                        'Duration': dur,
                        'Entry_Price': entry_p,
                        'Exit_Price': exit_p,
                        'Price_Move': pnl,
                        'Success': 1 if pnl > 0 else 0
                    }
                    trade_log.update(rulebook.loc[day].to_dict())
                    all_trades.append(trade_log)

        return pd.DataFrame(all_trades)
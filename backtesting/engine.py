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
        
        # 1. Filter years based on Data Start Year
        available_years = sorted([y for y in self.full_matrix.columns if y >= data_start_year])
        if not available_years:
            print("Error: No data available for the specified start year.")
            return pd.DataFrame()

        end_year = available_years[-1]

        # 2. Year-by-Year Walk-Forward Loop
        for test_year in range(test_start_year, end_year + 1):
            if test_year not in available_years: continue
            
            # Setup History (Past data only)
            hist_pool = [y for y in available_years if y < test_year]
            if not hist_pool: continue
            
            # 3. Build Seasonal Rulebook (Historical Features)
            feature_matrix = self.full_matrix[hist_pool + [test_year]]
            anomaly_map = self.fc.get_anomaly_years(feature_matrix)
            df_hist_features = self.fc.get_cleaned_averages(feature_matrix, anomaly_map)
            stats_df = self.fc.calculate_stats(feature_matrix)
            rulebook = df_hist_features.join(stats_df)

            # 4. Setup Live Data for Test Year
            live_price = self.full_matrix[test_year]
            date_map = self.result['series'][test_year]['date'].reindex(self.full_matrix.index).interpolate(method='linear')

            # 5. Daily Loop
            # We use enumerate to easily slice the "trailing" data up to today
            for i, day in enumerate(self.full_matrix.index):
                if pd.isna(live_price.loc[day]): continue
                
                # Context from the past
                hist_today = rulebook.loc[day]
                
                # Context from the current year: 
                # Slice the series from the beginning of the year up to the current day
                live_trailing_data = live_price.iloc[:i+1].dropna()

                # Strategy decides: Signal (LONG/SHORT/NONE) and Duration
                signal, duration = strategy_func(hist_today, live_trailing_data)

                if signal != "NONE" and duration > 0:
                    exit_day = day + duration
                    
                    # Verify entry vs exit (Look-ahead check)
                    if exit_day in live_price.index and not pd.isna(live_price.loc[exit_day]):
                        entry_p = live_price.loc[day]
                        exit_p = live_price.loc[exit_day]
                        
                        pnl = exit_p - entry_p if signal == "LONG" else entry_p - exit_p
                        
                        trade_log = {
                            'Entry_Date': date_map.loc[day].strftime('%Y-%m-%d'),
                            'Test_Year': test_year,
                            'Day_to_Expiry': day,
                            'Signal': signal,
                            'Duration': duration,
                            'Entry_Price': entry_p,
                            'Exit_Price': exit_p,
                            'Price_Move': pnl,
                            'Success': 1 if pnl > 0 else 0
                        }
                        # Add historical features for manual auditing
                        trade_log.update(hist_today.to_dict())
                        all_trades.append(trade_log)

        return pd.DataFrame(all_trades)

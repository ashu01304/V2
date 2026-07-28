import pandas as pd
import numpy as np

class FeatureCreator:
    def get_anomaly_years(self, combined_df):
        """
        Identifies anomaly years based on correlation across three brackets:
        - 11-15 years (Full) -> 3 anomalies (Requires min 11 years)
        - 6-10 years (Medium) -> 2 anomalies (Requires min 6 years)
        - 4-5 years (Short) -> 1 anomaly (Requires min 4 years)
        """
        all_years = sorted(combined_df.columns)
        if not all_years:
            return None

        # 1. Exclude the current year (the latest one) from anomaly calculation
        historical_pool = all_years[:-1]
        
        results = {
            "bracket_15y": self._calculate_subset(combined_df, historical_pool, 15, min_req=11, budget=3),
            "bracket_10y": self._calculate_subset(combined_df, historical_pool, 10, min_req=6, budget=2),
            "bracket_5y":  self._calculate_subset(combined_df, historical_pool, 5, min_req=4, budget=1)
        }
        
        return results

    def _calculate_subset(self, df, historical_pool, window_size, min_req, budget):
        # Take the most recent N years from the historical pool
        subset_years = historical_pool[-window_size:]
        n = len(subset_years)

        # Requirement check
        if n < min_req:
            return "NA"

        # 1. Calculate the Rolling 30-day Standard Deviation for each year
        # This measures the volatility of each year independently
        rolling_vol = df[subset_years].rolling(window=30, min_periods=1).std()

        # 2. Calculate the average volatility for each year over the whole window
        # We take the mean of the daily SD values
        avg_volatility = rolling_vol.mean()
        
        # 3. Identify 'budget' years with the HIGHEST average volatility
        # These are the "wildest" years in terms of price swings
        anomalies = avg_volatility.sort_values(ascending=False).head(budget).index.tolist()
        
        return sorted(anomalies)

    def get_cleaned_averages(self, combined_df, anomaly_map):
        """
        Creates a copy of the seasonality data and adds cleaned average columns:
        - Avg_5y_Clean: Last 5 historical years minus 1 anomaly
        - Avg_10y_Clean: Last 10 historical years minus 2 anomalies
        - Avg_15y_Clean: Last 15 historical years minus 3 anomalies
        """
        # 1. Create a copy of the price matrix
        df_cleaned = combined_df.copy()
        all_years = sorted(df_cleaned.columns)
        
        # Exclude current year from all historical average calculations
        historical_pool = all_years[:-1]

        # 2. Define calculations for each bracket
        brackets = [
            # (Column Name, Window Size, Anomaly Key)
            ('Avg_5y_Clean', 5, 'bracket_5y'),
            ('Avg_10y_Clean', 10, 'bracket_10y'),
            ('Avg_15y_Clean', 15, 'bracket_15y')
        ]

        for col_name, window, key in brackets:
            # Take the subset of years for this window
            subset = historical_pool[-window:]
            anomalies = anomaly_map.get(key, [])
            
            # Filter out anomalies and 'NA' markers
            if anomalies == "NA":
                # If bracket is NA, average whatever is in the subset without exclusions
                valid_years = subset
            else:
                valid_years = [y for y in subset if y not in anomalies]

            # 3. Calculate mean across the row for these specific years
            if valid_years:
                df_cleaned[col_name] = df_cleaned[valid_years].mean(axis=1)
            else:
                df_cleaned[col_name] = np.nan

        return df_cleaned

    def calculate_current_rank(self, combined_df):
        """
        Calculates the rank of the current year compared to all available years.
        1 = Highest value among years, N = Lowest value.
        """
        # 1. Identify only the year columns (integers)
        year_cols = sorted([c for c in combined_df.columns if isinstance(c, (int, float)) and str(c).isdigit() or isinstance(c, int)])
        current_year = year_cols[-1]

        # 2. Rank across the row (axis=1)
        # ascending=False makes the Maximum value = 1
        # method='min' ensures if it is the highest, it gets 1
        all_ranks = combined_df[year_cols].rank(axis=1, ascending=False, method='min')

        # 3. Return only the rank for the current year
        return all_ranks[current_year]

    def calculate_current_rank(self, combined_df):
        # Identify numeric year columns
        year_cols = sorted([c for c in combined_df.columns if isinstance(c, (int, float))])
        current_year = year_cols[-1]
        # Rank: Highest Price = 1
        all_ranks = combined_df[year_cols].rank(axis=1, ascending=False, method='min')
        return all_ranks[current_year]

    def calculate_stats(self, combined_df):
        # Ensure index is sorted -400 to 0 (moving forward in time)
        df = combined_df.copy().sort_index()
        all_years = sorted([c for c in df.columns if isinstance(c, (int, float))])
        hist_years = all_years[:-1] # Exclude latest year
        
        intervals = [3, 6, 10, 15, 21]
        brackets = [
            ('5Y', 5, 0),    # Label, Window, Min_Years_Required
            ('10Y', 10, 6),
            ('15Y', 15, 11)
        ]

        stat_results = pd.DataFrame(index=df.index)

        for days in intervals:
            # shift(-3) moves the value from 3 days in the future to 'today'
            future_price = df.shift(-days)

            for label, window, min_req in brackets:
                subset = hist_years[-window:]
                
                if len(subset) < min_req:
                    stat_results[f"WinRate_{label}_{days}D"] = np.nan
                    stat_results[f"Expected_{label}_{days}D"] = np.nan
                    continue

                # Difference = Price[Future] - Price[Today]
                diffs = future_price[subset] - df[subset]
                
                # 1. Win Rate Calculation
                wins = (diffs > 0).sum(axis=1)
                total_valid = diffs.notna().sum(axis=1)
                # Ensure we don't divide by zero if near expiry
                win_rate = wins / total_valid
                
                # 2. Expected Value Calculation
                # Matches your formula: ( (P_fut1 - P_now1) + (P_fut2 - P_now2) ... ) / n
                expectancy = diffs.mean(axis=1)

                stat_results[f"WinRate_{label}_{days}D"] = win_rate
                stat_results[f"Expected_{label}_{days}D"] = expectancy

        return stat_results
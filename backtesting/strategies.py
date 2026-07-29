# These are the parameters available to you within the strategy_func(hist_stats, live_data) in strategies.py.

# 1. Seasonal Directional Stats (UpRate)
# UpRate_5Y_[Interval]D: Percentage of the last 5 years where price was higher after [Interval] days.
# UpRate_10Y_[Interval]D: Percentage of the last 10 years (min 6) where price was higher after [Interval] days.
# UpRate_15Y_[Interval]D: Percentage of all available years (min 11) where price was higher after [Interval] days.
# Note: [Interval] can be 3, 6, 10, 15, or 21.

# 2. Seasonal Expectancy Stats (Expected Move)
# Expected_5Y_[Interval]D: Average price change over [Interval] days across the last 5 historical years.
# Expected_10Y_[Interval]D: Average price change over [Interval] days across the last 10 historical years.
# Expected_15Y_[Interval]D: Average price change over [Interval] days across all historical years.

# 3. Seasonal Risk Stats (Max Drawdown)
# MaxDrawdown_5Y_[Interval]D: Average of the worst price drops seen within [Interval] days across the last 5 years.
# MaxDrawdown_10Y_[Interval]D: Average of the worst price drops seen within [Interval] days across the last 10 years.
# MaxDrawdown_15Y_[Interval]D: Average of the worst price drops seen within [Interval] days across all years.

# 4. Clean Seasonal Paths (Filtered Averages)
# Avg_5y_Clean: The mean price of the last 5 years after removing the most anomalous year (by volatility).
# Avg_10y_Clean: The mean price of the last 10 years after removing the 2 most anomalous years.
# Avg_15y_Clean: The mean price of all available years after removing the 3 most anomalous years.

# 5. Global Seasonal Benchmarks
# STAT_Average: The global average price for this "Day to Expiry" across all historical years.
# STAT_Std_Dev: The standard deviation (spread) of prices across all historical years for this day.
# STAT_Rolling_2Sigma_Path: The 2nd standard deviation of the average path's 30-day volatility.
# Current_Rank: The historical rank of the price at this "Day to Expiry" (1 = highest price in history).

# 6. Live Test-Year Data (live_data Series)
# live_data.iloc[-1]: The actual current market price on the day being evaluated.
# live_data.tail(N).mean(): The rolling average price of the current contract over the last N days.
# live_data.tail(N).std(): The rolling volatility (Standard Deviation) of the current contract over the last N days.
# len(live_data): The number of days the current contract has been trading so far.
# live_data.max() / live_data.min(): The life-to-date high or low of the current contract.

import pandas as pd
import numpy as np

class SeasonalStrategies:
    @staticmethod
    def ashu01_vectorized(rulebook, live_price):
        """
        Vectorized version of ashu01. 
        Returns two series: 'signals' (1 for LONG, -1 for SHORT) and 'durations'.
        """
        # 1. Pre-calculate live indicators for the whole year
        year_SD = live_price.rolling(10).std()
        year_MEAN = live_price.rolling(21).mean()

        # 2. Define Helper to get columns safely with a default
        def get_col(name, default=0.5):
            return rulebook[name] if name in rulebook.columns else pd.Series(default, index=rulebook.index)

        # 3. Create Boolean Masks for all conditions
        # LONG Conditions
        long_mask = (
            (get_col('UpRate_5Y_3D') >= 0.8) & 
            (get_col('UpRate_10Y_3D') >= 0.7) & 
            (get_col('UpRate_5Y_6D') >= 0.6) & 
            (get_col('UpRate_10Y_6D') > 0.7) & 
            (live_price <= get_col('Avg_5Y_Clean', live_price)) & 
            (live_price <= year_MEAN - 0.5 * year_SD)
        )

        # SHORT Conditions
        short_mask = (
            (get_col('UpRate_5Y_3D') <= 0.2) & 
            (get_col('UpRate_10Y_3D') <= 0.3) & 
            (get_col('UpRate_5Y_6D') <= 0.4) & 
            (get_col('UpRate_10Y_6D') < 0.3) & 
            (live_price >= get_col('Avg_5Y_Clean', live_price)) & 
            (live_price >= year_MEAN + 0.5 * year_SD)
        )

        # 4. Generate Signal Output
        signals = pd.Series(0, index=rulebook.index)
        signals[long_mask] = 1   # 1 for LONG
        signals[short_mask] = -1 # -1 for SHORT
        
        durations = pd.Series(0, index=rulebook.index)
        durations[long_mask | short_mask] = 6 # Set fixed duration 6
        
        return signals, durations
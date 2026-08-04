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
    def technical_zscore_rsi(hist_stats, live_trailing, holding_days=5,
                             z_score_threshold=1.85, upper_rsi=65,
                             lower_rsi=35, stop_before_expiry=63):
        """Trade live Z-score extremes confirmed by the existing RSI definition."""
        z_score = hist_stats.get('TECH_ZScore', np.nan)
        rsi = hist_stats.get('TECH_RSI', np.nan)
        days_to_expiry = hist_stats.get('DAYS_TO_EXPIRY', np.nan)
        if pd.isna(z_score) or pd.isna(rsi) or pd.isna(days_to_expiry):
            return "NONE", 0

        if days_to_expiry < stop_before_expiry:
            return "NONE", 0

        if z_score <= -z_score_threshold and rsi <= lower_rsi:
            return "LONG", holding_days
        elif z_score >= z_score_threshold and rsi >= upper_rsi:
            return "SHORT", holding_days
        return "NONE", 0

    @staticmethod
    def ashu01(hist_stats, live_trailing):
        year_SD = live_trailing.tail(10).std()
        year_MEAN = live_trailing.tail(21).mean()
        curr_price = live_trailing.iloc[-1]

        if hist_stats.get('UpRate_5Y_3D',0.5) >= 0.8 and hist_stats.get('UpRate_10Y_3D',0.5) >= 0.7 and hist_stats.get('UpRate_5Y_6D',0.5) >= 0.6 and hist_stats.get('UpRate_10Y_6D',0.5) > 0.7 and curr_price <= hist_stats.get('Avg_5Y_Clean', curr_price) and curr_price <= year_MEAN - 0.5*year_SD:
            return "LONG", 6
        
        elif hist_stats.get('UpRate_5Y_3D',0.5) <= 0.2 and hist_stats.get('UpRate_10Y_3D',0.5) <= 0.3 and hist_stats.get('UpRate_5Y_6D',0.5) <= 0.4 and hist_stats.get('UpRate_10Y_6D',0.5) < 0.3 and curr_price >= hist_stats.get('Avg_5Y_Clean', curr_price) and curr_price >= year_MEAN + 0.5*year_SD  :
            return "SHORT", 6
        else:
            return "NONE", 0

    @staticmethod
    def CO_Defly01(hist_stats, live_trailing): # its working fine on far months defly 
        days = 6
        if len(live_trailing) < 2:
            return "NONE", 0

        curr_price = live_trailing.iloc[-1]
        last_day_price = live_trailing.iloc[-2]
        year_SD = live_trailing.tail(21).std()
        year_MEAN = live_trailing.tail(42).mean()
        seac_avg_5c = hist_stats.get('Avg_5Y_Clean', curr_price)
        seac_avg_10c = hist_stats.get('Avg_10Y_Clean', curr_price)
        seac_sd_5 = hist_stats.get('STAT_Std_Dev', 0)
        up_5_6D  = hist_stats.get('UpRate_5Y_6D',0.5)
        up_5_10D = hist_stats.get('UpRate_5Y_10D',0.5)
        up_5_15D = hist_stats.get('UpRate_5Y_15D',0.5)
        up_5_20D = hist_stats.get('UpRate_5Y_20D',0.5)
        up_10_6D  = hist_stats.get('UpRate_10Y_6D',0.5)
        up_10_10D = hist_stats.get('UpRate_10Y_10D',0.5)
        up_10_15D = hist_stats.get('UpRate_10Y_15D',0.5)
        up_10_20D = hist_stats.get('UpRate_10Y_20D',0.5)

        if curr_price <= year_MEAN - 1*year_SD and up_5_6D >= 0.6 and up_10_6D >= 0.6 and curr_price >= year_MEAN - 3*year_SD and curr_price <= seac_avg_5c : 
            return "LONG", days

        elif curr_price >= year_MEAN + 1*year_SD and up_5_6D <= 0.4 and up_10_6D <= 0.4 and curr_price <= year_MEAN + 3*year_SD and curr_price >= seac_avg_5c: 
            return "SHORT", days

        else:
            return "NONE", 0

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
    def ashu01(hist_stats, live_trailing):
        year_SD = live_trailing.tail(10).std()
        year_MEAN = live_trailing.tail(21).mean()
        curr_price = live_trailing.iloc[-1]

        if hist_stats.get('UpRate_5Y_3D',0.5) >= 0.8 and hist_stats.get('UpRate_10Y_3D',0.5) >= 0.7 and hist_stats.get('UpRate_5Y_6D',0.5) >= 0.6 and hist_stats.get('UpRate_10Y_6D',0.5) > 0.7 and curr_price <= hist_stats.get('Avg_5Y_Clean', curr_price) and curr_price <= year_MEAN - 0.5*year_SD:
            return "LONG", 6
        
        elif hist_stats.get('UpRate_5Y_3D',0.5) <= 0.2 and hist_stats.get('UpRate_10Y_3D',0.5) <= 0.3 and hist_stats.get('UpRate_5Y_6D',0.5) <= 0.4 and hist_stats.get('UpRate_10Y_6D',0.5) < 0.3 and curr_price >= hist_stats.get('Avg_5Y_Clean', curr_price) and curr_price >= year_MEAN + 0.5*year_SD:
            return "SHORT", 6
        else:
            return "NONE", 0

    @staticmethod
    def aman01(hist_stats, live_trailing):
        """
        Implementation of the Seasonal Bias + Z-Score Framework.
        - L/S Classification Threshold: 60% UpRate for L, 40% for S.
        - Extreme Z-Score Threshold: +/- 2.0.
        """
        if len(live_trailing) < 1: return "NONE", 0

        # 1. Statistical Component (Z-Score)
        curr_price = live_trailing.iloc[-1]
        mean = hist_stats.get('STAT_Average', np.nan)
        std = hist_stats.get('STAT_Std_Dev', np.nan)
        
        if pd.isna(mean) or pd.isna(std) or std == 0:
            return "NONE", 0
        
        z_score = (curr_price - mean) / std

        # 2. Seasonal Component (Classifying 10D, 15D, 21D Windows)
        # We use 10Y bracket as the standard historical tendency
        windows = [10, 15, 21]
        signals = []
        for d in windows:
            uprate = hist_stats.get(f'UpRate_10Y_{d}D', 0.5)
            if uprate >= 0.60: signals.append('L')
            elif uprate <= 0.40: signals.append('S')
            else: signals.append('N')

        # 3. Final Bias Logic
        seasonal_pattern = "".join(signals)
        
        # Determine Final Signal and Duration
        # LONG: Strong alignment (LLL) or developing strength (e.g., NLL, LNL)
        if seasonal_pattern.count('L') >= 2 and 'S' not in seasonal_pattern:
            # Exceptional Long if Z-score is negative (undervalued)
            # Caution if Z-score is > 2 (exhaustion risk)
            return "LONG", 21 

        # SHORT: Strong alignment (SSS) or developing weakness
        if seasonal_pattern.count('S') >= 2 and 'L' not in seasonal_pattern:
            # Exceptional Short if Z-score is positive (overextended)
            return "SHORT", 21

        return "NONE", 0
    
    @staticmethod
    def standard_logic(hist_stats, live_trailing):
        signal = "NONE"
        duration = 0
        
        # Accessing your created columns
        uprate = hist_stats.get('UpRate_10Y_6D', 0)
        
        if uprate > 0.80:
            signal = "LONG"
            duration = 6
        elif uprate < 0.20:
            signal = "SHORT"
            duration = 6
            
        return signal, duration

    @staticmethod
    def complex_seasonal_strategy(hist_stats, live_trailing):
        # 1. Basic Thresholds
        uprate_5y = hist_stats.get('UpRate_5Y_6D', 0)
        uprate_15y = hist_stats.get('UpRate_15Y_6D', 0)
        avg_10y = hist_stats.get('Avg_10y_Clean', 0)
        max_dd = hist_stats.get('MaxDrawdown_10Y_6D', -999)
        
        # 2. Conditions
        high_prob = (uprate_5y > 0.8) and (uprate_15y > 0.75)
        good_value = live_trailing['Price'] < avg_10y  # Price is "cheap" vs history
        safe_risk = max_dd > -0.8  # Historically, doesn't drop more than 0.8 points
        
        # 3. Decision
        if high_prob and good_value and safe_risk:
            return "LONG", 6
        
        # Reverse for SHORT
        if (uprate_5y < 0.15) and (live_trailing['Price'] > avg_10y):
            return "SHORT", 6
            
        return "NONE", 0

    @staticmethod
    def aggressive_multi_window(hist_stats, live_data):
        """
        hist_stats: Series of historical features
        live_data: Series of test year prices from start of year to today
        """
        if len(live_data) < 1: return "NONE", 0

        # 1. Calculate Any Live Indicators here
        current_price = live_data.iloc[-1]
        m15 = live_data.tail(15).mean()
        s15 = live_data.tail(15).std()
        
        intervals = [3, 6, 10]
        horizons = ['5Y', '10Y']

        if s15 > 0.2:
            return "NONE", 0  # Too volatile, skip trading
        
        # 2. Check LONG conditions (Seasonality + Mean Reversion)
        long_ok = True
        for d in intervals:
            for h in horizons:
                up = hist_stats.get(f'UpRate_{h}_{d}D', np.nan)
                dd = hist_stats.get(f'MaxDrawdown_{h}_{d}D', -999)
                if not pd.isna(up) and (up <= 0.65 or dd <= -0.2):
                    long_ok = False; break
            if not long_ok: break
            
        if long_ok and current_price < (m15 - s15):
            # Pick duration with best expected value
            best_d = max(intervals, key=lambda d: hist_stats.get(f'Expected_5Y_{d}D', 0))
            return "LONG", best_d

        # 3. Check SHORT conditions
        short_ok = True
        for d in intervals:
            for h in horizons:
                up = hist_stats.get(f'UpRate_{h}_{d}D', np.nan)
                if not pd.isna(up) and up >= 0.20:
                    short_ok = False; break
            if not short_ok: break
            
        if short_ok and current_price > (m15 + s15):
            best_d = min(intervals, key=lambda d: hist_stats.get(f'Expected_5Y_{d}D', 0))
            return "SHORT", best_d
            
        return "NONE", 0
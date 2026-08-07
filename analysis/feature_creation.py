import pandas as pd
import numpy as np
from analysis.utils import BRACKETS, year_columns

class FeatureCreator:
    def create_historical_features(self, combined_df, features):
        """Create only requested historical feature families."""
        names = set(features)
        output = pd.DataFrame(index=combined_df.index)

        clean_names = names & {"Avg_5Y_Clean", "Avg_10Y_Clean", "Avg_15Y_Clean"}
        if clean_names:
            cleaned = self.get_cleaned_averages(combined_df, self.get_anomaly_years(combined_df))
            output[list(clean_names)] = cleaned[list(clean_names)]

        stat_names = [name for name in names if name.startswith(("STAT_", "UpRate_", "Expected_", "MaxDrawdown_"))]
        if stat_names:
            stats = self.calculate_stats(combined_df)
            output[stat_names] = stats[stat_names]

        if "Current_Rank" in names:
            output["Current_Rank"] = self.calculate_current_rank(combined_df)
        return output

    def create_live_features(self, live_trailing, features):
        """Create only the requested live features."""
        live = live_trailing.dropna().sort_index()
        output = {}

        if "TECH_ZScore" in features:
            window = features["TECH_ZScore"].get("window", 20)
            mean = live.rolling(window).mean()
            std = live.rolling(window).std()
            output["TECH_ZScore"] = ((live - mean) / std).iloc[-1]

        if "TECH_RSI" in features:
            window = features["TECH_RSI"].get("window", 14)
            delta = live.diff()
            gain = delta.clip(lower=0).rolling(window).mean()
            loss = (-delta.clip(upper=0)).rolling(window).mean()
            rs = gain / loss.replace(0, np.nan)
            rsi = 100 - (100 / (1 + rs))
            rsi.loc[(loss == 0) & (gain > 0)] = 100
            output["TECH_RSI"] = rsi.iloc[-1]

        if "LIVE_STD" in features:
            output["LIVE_STD"] = live.tail(features["LIVE_STD"].get("window", 20)).std()

        return pd.Series(output, dtype=float)

    def calculate_live_technical_features(self, live_trailing, window_bb=20, window_rsi=14):
        return self.create_live_features(live_trailing, {
            "TECH_ZScore": {"window": window_bb},
            "TECH_RSI": {"window": window_rsi},
        })

    def calculate_seasonality_features(self, combined_df):
        average = combined_df.mean(axis=1)
        std_years = combined_df.std(axis=1)
        rolling_std_path = average.rolling(window=30, min_periods=30).std() * 2 + 0.5 * std_years
        return average, std_years, rolling_std_path

    def get_anomaly_years(self, combined_df):
        """
        Identifies anomaly years based on rolling volatility across three brackets:
        - 11-15 years (Full) -> 3 anomalies (Requires min 11 years)
        - 6-10 years (Medium) -> 2 anomalies (Requires min 6 years)
        - 4-5 years (Short) -> 1 anomaly (Requires min 4 years)
        """
        all_years = year_columns(combined_df)
        if not all_years:
            return None

        # 1. Exclude the current year (the latest one) from anomaly calculation
        historical_pool = all_years[:-1]
        
        results = {
            f"bracket_{name.lower()}": self._calculate_subset(
                combined_df, historical_pool, config["window"],
                config["anomaly_min"], config["anomaly_budget"]
            )
            for name, config in BRACKETS.items()
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
        - Avg_5Y_Clean: Last 5 historical years minus 1 anomaly
        - Avg_10Y_Clean: Last 10 historical years minus 2 anomalies
        - Avg_15Y_Clean: Last 15 historical years minus 3 anomalies
        """
        # 1. Create a copy of the price matrix
        df_cleaned = combined_df.copy()
        all_years = year_columns(df_cleaned)
        
        # Exclude current year from all historical average calculations
        historical_pool = all_years[:-1]

        # 2. Define calculations for each bracket
        brackets = [
            (f"Avg_{name}_Clean", config["window"], f"bracket_{name.lower()}")
            for name, config in BRACKETS.items()
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
        year_cols = year_columns(combined_df)
        current_year = year_cols[-1]
        all_ranks = combined_df[year_cols].rank(axis=1, ascending=False, method='min')
        return all_ranks[current_year]

    def calculate_current_rank_ratio(self, combined_df, years=None):
        year_cols = year_columns(combined_df)
        if years is not None:
            year_cols = year_cols[-years:]
        current_year = year_cols[-1]
        values = combined_df[year_cols]
        return pd.DataFrame({
            "Rank": values.rank(axis=1, ascending=False, method="min")[current_year],
            "Count": values.notna().sum(axis=1),
        }, index=combined_df.index)

    def calculate_average_forward_slope(self, combined_df, days=10, years=None,
                                        drop_least_correlated=0.2):
        df = combined_df.sort_index()
        year_cols = year_columns(df)
        current = year_cols[-1]
        historical = year_cols[:-1]
        if years is not None:
            historical = historical[-years:] if years > 0 else []
        if not historical:
            return pd.Series(np.nan, index=df.index)
        drop_count = int(len(historical) * drop_least_correlated)
        if drop_count:
            correlations = df[historical].corrwith(df[current]).fillna(-np.inf)
            excluded = set(correlations.nsmallest(drop_count).index)
            historical = [year for year in historical if year not in excluded]
        return ((df[historical].shift(-days) - df[historical]) / days).mean(axis=1)

    def calculate_bollinger_signal(self, live_series, window=45):
        trailing = live_series.dropna().sort_index().tail(window)
        if len(trailing) < window:
            return {"percent_b": np.nan, "signal": None}
        average = trailing.mean()
        std = trailing.std(ddof=1)
        if pd.isna(std) or std == 0:
            return {"percent_b": np.nan, "signal": None}
        percent_b = (trailing.iloc[-1] - (average - 2 * std)) / (4 * std)
        signal = "LONG" if percent_b <= 0.20 else "SHORT" if percent_b >= 0.80 else "NEUTRAL"
        return {"percent_b": float(percent_b), "signal": signal}

    def calculate_stats(self, combined_df):
        # Ensure moving forward in time (-400 to 0)
        df = combined_df.copy().sort_index()
        year_cols = year_columns(df)
        year_df = df[year_cols]
        hist_years = year_cols[:-1]
        
        intervals = [3, 6, 10, 15, 21]
        brackets = [
            (name, config['window'], config['stats_min'])
            for name, config in BRACKETS.items()
        ]
        stat_results = pd.DataFrame(index=df.index)
        # Calculate Average, SD and Rolling 2S
        # Historical benchmarks must exclude the current/test year.
        hist_year_df = year_df[hist_years]
        average, std_years, rolling_std_path = self.calculate_seasonality_features(hist_year_df)
        stat_results['STAT_Average'] = average
        stat_results['STAT_Std_Dev'] = std_years
        stat_results['STAT_Rolling_2S'] = rolling_std_path

        for days in intervals:
            # Price at exact end of window
            future_price = year_df.shift(-days)
            # Forward-looking rolling minimum for Max Drawdown
            # We reverse the DF, take rolling min, and reverse back to look 'ahead'
            rolling_min = year_df.iloc[::-1].rolling(window=days+1, min_periods=1).min().iloc[::-1]

            for label, window, min_req in brackets:
                subset = hist_years[-window:]
                if len(subset) < min_req:
                    for metric in ['UpRate', 'Expected', 'MaxDrawdown']:
                        stat_results[f"{metric}_{label}_{days}D"] = np.nan
                    continue

                # 1. Directional Stats (UpRate & Expected)
                diffs_at_end = future_price[subset] - year_df[subset]
                valid_count = diffs_at_end.notna().sum(axis=1)
                stat_results[f"UpRate_{label}_{days}D"] = (
                    (diffs_at_end > 0).sum(axis=1)
                    / valid_count.replace(0, np.nan)
                )
                stat_results[f"Expected_{label}_{days}D"] = diffs_at_end.mean(axis=1)

                # 2. Risk Stats (Max Drawdown / MAE)
                # Drawdown = Lowest point in window - Price at start
                drawdowns = rolling_min[subset] - year_df[subset]
                # We cap drawdown at 0
                drawdowns = drawdowns.clip(upper=0)
                stat_results[f"MaxDrawdown_{label}_{days}D"] = drawdowns.mean(axis=1)

        return stat_results
    

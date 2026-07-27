import pandas as pd
from datetime import datetime
import re

class Seasonality:
    def prepare_calendar(self, data_dict):
        """Fills gaps and clips at Today's date."""
        processed = {}
        now = datetime.now()
        for code, df in data_dict.items():
            if df.empty: continue
            
            # Work on a copy and ensure index is datetime
            df = df.copy()
            df.index = pd.to_datetime(df.index)
            df = df.sort_index()
            
            # Clip future data
            df = df[df.index <= now]
            if df.empty: continue
            
            # Remove duplicate index entries if they exist
            df = df[~df.index.duplicated(keep='last')]
            
            # Fill gaps (Weekends/Holidays)
            full_range = pd.date_range(df.index.min(), df.index.max(), freq='D')
            df = df.reindex(full_range).interpolate(method='linear')
            processed[code] = df
        return processed

    def calculate_expression(self, data_dict, expression, instrument=None):
        """Evaluates math like Z26 - 2*F27 + G27."""
        # Find all month-year pairs (e.g., Z26, F27)
        matches = re.findall(r"([FGHJKMNQUVXZ])(\d{2})", expression)
        if not matches: return {}
        
        # The first contract in the expression is the "Anchor" leg
        ref_month, ref_yy = matches[0]
        ref_year_in_expr = int(ref_yy)
        
        # Determine available seasons based on the presence of the anchor leg
        available_seasons = []
        for k in data_dict.keys():
            # Matches codes like 'Z24' or 'CLZ24' depending on naming
            if ref_month in k:
                year_match = re.search(r"\d{2}", k)
                if year_match:
                    available_seasons.append(int("20" + year_match.group()))
        
        seasons_results = {}
        
        for s in sorted(available_seasons):
            # 1. Build a seasonal DataFrame using an INNER JOIN
            # This ensures we only have dates where ALL legs exist
            season_df = pd.DataFrame()
            mapping = {}
            valid = True
            
            for month, yy in matches:
                offset = int(yy) - ref_year_in_expr
                contract_code = f"{month}{(s + offset) % 100:02d}"
                
                # Check if the code (or symbol+code) exists in our data
                actual_key = next((k for k in data_dict.keys() if contract_code in k), None)
                
                if not actual_key:
                    valid = False
                    break
                
                token = f"{month}{yy}"
                series = data_dict[actual_key]['Close']
                
                if season_df.empty:
                    season_df = series.to_frame(name=token)
                else:
                    # Inner join handles the 1500 vs 500 row mismatch automatically
                    season_df = season_df.join(series.rename(token), how='inner')
            
            if not valid or season_df.empty:
                continue

            # 2. Perform the Math Parser logic
            # Normalizes the expression for robust calculation
            clean_expr = expression.replace(" ", "").replace("-", "+-")
            terms = [p for p in clean_expr.split("+") if p]
            
            result_series = pd.Series(0.0, index=season_df.index)
            
            for term in terms:
                try:
                    if "*" in term:
                        coeff, token = term.split("*")
                        result_series += float(coeff) * season_df[token]
                    elif term.startswith("-"):
                        token = term[1:]
                        result_series -= season_df[token]
                    else:
                        token = term.lstrip("+")
                        result_series += season_df[token]
                except Exception as e:
                    # print(f"Skipping term {term} for season {s}: leg data missing on specific days")
                    continue

            # Store results as a dataframe
            final_df = result_series.to_frame(name="Close").dropna()
            if not final_df.empty:
                seasons_results[s] = final_df

        return seasons_results

    def align_to_expiry(self, seasons_data, window_days=365):
        """Aligns data to the shared Year 2000 timeline."""
        BASE_YEAR = 2000
        aligned = {}
        for s_year, df in seasons_data.items():
            if df.empty: continue
            shifted = df.copy()
            
            # Map the Jan 1st of the season to Jan 1st 2000 for overlap
            ref_date = datetime(s_year, 1, 1)
            new_index = [datetime(BASE_YEAR, 1, 1) + (d - ref_date) for d in shifted.index]
            shifted.index = pd.DatetimeIndex(new_index)
            
            # Ensure index uniqueness after transformation
            shifted = shifted[~shifted.index.duplicated(keep='last')]
            
            # Cut to user-defined window (e.g., 250 or 365 days)
            start = datetime(BASE_YEAR, 1, 1) - pd.Timedelta(days=window_days)
            end = datetime(BASE_YEAR, 1, 1) + pd.Timedelta(days=31)
            
            trimmed = shifted[(shifted.index >= start) & (shifted.index <= end)]
            if not trimmed.empty:
                aligned[s_year] = trimmed.sort_index()
        return aligned

    def get_average(self, aligned_data):
        """Calculates the historical average across all seasonal years."""
        series_to_concat = []
        for year, df in aligned_data.items():
            if isinstance(year, int) and not df.empty:
                s = df['Close'].rename(year)
                series_to_concat.append(s)
        
        if not series_to_concat:
            return pd.DataFrame()
            
        combined = pd.concat(series_to_concat, axis=1)
        return combined.mean(axis=1).to_frame(name="Close").dropna()
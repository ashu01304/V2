import re
import numpy as np
import pandas as pd
from analysis.expiry import OfficialExpiryLookup
from analysis.feature_creation import FeatureCreator

class Seasonality:
    def __init__(self, db):
        self.db = db
        self.official_expiry = OfficialExpiryLookup()
        self.fc = FeatureCreator()

    # shared helpers
    def _fill_weekends(self, df, window_start, window_end):
        full_calendar = pd.date_range(window_start, window_end, freq='D')
        return df.reindex(full_calendar).interpolate(method='linear', limit_direction='both')

    def _month_ticks(self, offsets, dates):
        tickvals, ticktext, seen = [], [], set()
        for off, date in zip(offsets, dates):
            key = (date.year, date.month)
            if key not in seen:
                seen.add(key)
                tickvals.append(off)
                ticktext.append(date.strftime('%b'))
        return tickvals, ticktext

    def _leg_window(self, symbol, code, window_days):
        expiry = self.official_expiry.get(symbol, code)
        if expiry is None:
            expiry = self._last_contract_date(symbol, code)
        if expiry is None:
            return None, None, None
        today = pd.Timestamp(pd.Timestamp.now().date())
        expiry = pd.Timestamp(expiry).normalize()
        window_start = (expiry - pd.Timedelta(days=window_days)).normalize()
        window_end = min(expiry, today).normalize()
        return expiry, window_start, window_end

    def _last_contract_date(self, symbol, contract_code):
        self.db.cursor.execute(
            "SELECT MAX(trading_date) FROM seac_settlements WHERE symbol = ? AND contract_code = ?",
            (symbol, contract_code)
        )
        last_date = self.db.cursor.fetchone()[0]
        return pd.Timestamp(last_date) if last_date else None

    def _contract_years(self, symbol, letter, start_year, end_year=None):
        self.db.cursor.execute(
            "SELECT DISTINCT contract_code FROM seac_settlements WHERE symbol = ? AND contract_code LIKE ?",
            (symbol, f"{letter}%")
        )
        years = sorted({2000 + int(code[1:]) for code, in self.db.cursor.fetchall()})
        years = [year for year in years if year >= start_year]
        if end_year is not None:
            years = [year for year in years if year <= end_year]
        return years

    # outright seasonality
    def outright_seasonality(self, symbol, letter, start_year, end_year=None, window_days=400, interpolate=True):
        self.db.cursor.execute(
            "SELECT DISTINCT contract_code FROM seac_settlements WHERE symbol = ? AND contract_code LIKE ?",
            (symbol, f"{letter}%")
        )
        all_codes = [r[0] for r in self.db.cursor.fetchall()]
        codes = sorted(
            [c for c in all_codes if 2000 + int(c[1:]) >= start_year and (end_year is None or 2000 + int(c[1:]) <= end_year)],
            key=lambda c: int(c[1:])
        )
        if not codes:
            return self._empty_result([f"no contracts found for {letter} from {start_year}"])

        hist = self.db.get_contract_history(symbol, codes)
        today = pd.Timestamp(pd.Timestamp.now().date())
        series_out, warnings, ref_ticks = {}, [], None
        latest_year = max(2000 + int(code[1:]) for code in codes)

        for code, df in sorted(hist.items()):
            expiry, window_start, window_end = self._leg_window(symbol, code, window_days)
            if expiry is None or df.empty:
                warnings.append(f"{code}: skipped (no expiry or no data)")
                continue

            df = df.copy()
            df.index = pd.to_datetime(df.index).normalize()
            if 2000 + int(code[1:]) == latest_year:
                window_end = min(window_end, df.index.max())
            windowed = df[(df.index >= window_start) & (df.index <= window_end)].sort_index()
            windowed = windowed[~windowed.index.duplicated(keep='last')]
            if windowed.empty:
                warnings.append(f"{code}: no data in window")
                continue

            processed = self._fill_weekends(windowed, window_start, window_end) if interpolate else windowed
            if processed.empty:
                warnings.append(f"{code}: empty after processing")
                continue

            days_to_expiry = (processed.index - expiry).days
            year = 2000 + int(code[1:])
            series_out[year] = pd.DataFrame({
                'value': processed['Close'].values,
                'date': processed.index
            }, index=days_to_expiry)

            if expiry < today and (ref_ticks is None or len(processed) > len(ref_ticks[0])):
                ref_ticks = (days_to_expiry, processed.index)

        return self._package(series_out, ref_ticks, warnings)

    # expression seasonality
    def expression_seasonality(self, symbol, expression, start_year, end_year=None, window_days=400, interpolate=True):
        matches = re.findall(r"([FGHJKMNQUVXZ])(\d{2})", expression)
        if not matches:
            return self._empty_result(["could not parse expression"])

        ref_month, ref_yy = matches[0]
        ref_year_in_expr = int(ref_yy)
        today = pd.Timestamp(pd.Timestamp.now().date())

        series_out, warnings, ref_ticks = {}, [], None
        years = self._contract_years(symbol, ref_month, start_year, end_year)

        for s in years:
            anchor_code = f"{ref_month}{s % 100:02d}"
            expiry, window_start, window_end = self._leg_window(symbol, anchor_code, window_days)
            if expiry is None:
                warnings.append(f"{s}: no expiry or data for anchor {anchor_code}")
                continue

            leg_data, valid = {}, True
            for month, yy in matches:
                offset = int(yy) - ref_year_in_expr
                leg_code = f"{month}{(s + offset) % 100:02d}"
                hist = self.db.get_contract_history(symbol, [leg_code])
                if leg_code not in hist or hist[leg_code].empty:
                    valid = False
                    warnings.append(f"{s}: missing data for leg {leg_code}")
                    break

                df = hist[leg_code].copy()
                df.index = pd.to_datetime(df.index).normalize()
                if s == years[-1]:
                    window_end = min(window_end, df.index.max())
                windowed = df[(df.index >= window_start) & (df.index <= window_end)].sort_index()
                windowed = windowed[~windowed.index.duplicated(keep='last')]
                if windowed.empty:
                    valid = False
                    warnings.append(f"{s}: no data in window for leg {leg_code}")
                    break

                processed = self._fill_weekends(windowed, window_start, window_end) if interpolate else windowed
                if processed.empty:
                    valid = False
                    warnings.append(f"{s}: leg {leg_code} empty after processing")
                    break

                leg_data[f"{month}{yy}"] = processed['Close']

            if not valid:
                continue

            leg_df = pd.DataFrame(leg_data).dropna()
            if leg_df.empty:
                warnings.append(f"{s}: no overlapping dates across legs")
                continue

            result = self._evaluate_expression(expression, leg_df)
            if result.empty:
                warnings.append(f"{s}: expression evaluated to empty series")
                continue

            days_to_expiry = (result.index - expiry).days
            series_out[s] = pd.DataFrame({
                'value': result.values,
                'date': result.index
            }, index=days_to_expiry)

            if expiry < today and (ref_ticks is None or len(result) > len(ref_ticks[0])):
                ref_ticks = (days_to_expiry, result.index)

        return self._package(series_out, ref_ticks, warnings)

    def working_day_expression_seasonality(self, symbol, expression, start_year, end_year=None, window_days=400):
        matches = re.findall(r"([FGHJKMNQUVXZ])(\d{2})", expression)
        if not matches:
            return self._empty_result(["could not parse expression"])

        ref_month, ref_yy = matches[0]
        ref_year_in_expr = int(ref_yy)
        series_out, warnings = {}, []
        years = self._contract_years(symbol, ref_month, start_year, end_year)

        for s in years:
            anchor_code = f"{ref_month}{s % 100:02d}"
            anchor_hist = self.db.get_contract_history(symbol, [anchor_code]).get(anchor_code)
            if anchor_hist is None or anchor_hist.empty:
                warnings.append(f"{s}: missing data for anchor {anchor_code}")
                continue

            expiry = self._official_or_last_date(symbol, anchor_code, anchor_hist)
            leg_data, valid = {}, True

            for month, yy in matches:
                offset = int(yy) - ref_year_in_expr
                leg_code = f"{month}{(s + offset) % 100:02d}"
                hist = self.db.get_contract_history(symbol, [leg_code])
                if leg_code not in hist or hist[leg_code].empty:
                    valid = False
                    warnings.append(f"{s}: missing data for leg {leg_code}")
                    break

                df = hist[leg_code].copy()
                df.index = pd.to_datetime(df.index).normalize()
                leg_data[f"{month}{yy}"] = df["Close"]

            if not valid:
                continue

            leg_df = pd.DataFrame(leg_data).dropna()
            if leg_df.empty:
                warnings.append(f"{s}: no overlapping dates across legs")
                continue

            result = self._evaluate_expression(expression, leg_df)
            result = result[result.index <= expiry]
            if result.empty:
                warnings.append(f"{s}: expression evaluated to empty series")
                continue

            days_to_expiry = -np.busday_count(
                result.index.values.astype("datetime64[D]"),
                np.datetime64(expiry.date()),
            )
            keep = (days_to_expiry >= -window_days) & (days_to_expiry <= 0)
            result = result[keep]
            days_to_expiry = days_to_expiry[keep]
            if result.empty:
                warnings.append(f"{s}: no data in working-day window")
                continue

            series_out[s] = pd.DataFrame({
                "value": result.values,
                "date": result.index
            }, index=days_to_expiry)

        return self._package_working_days(series_out, warnings, window_days)

    def _evaluate_expression(self, expression, leg_df):
        clean_expr = expression.replace(" ", "").replace("-", "+-")
        terms = [p for p in clean_expr.split("+") if p]
        result = pd.Series(0.0, index=leg_df.index)
        for term in terms:
            try:
                if "*" in term:
                    coeff, token = term.split("*")
                    result += float(coeff) * leg_df[token]
                elif term.startswith("-"):
                    result -= leg_df[term[1:]]
                else:
                    result += leg_df[term.lstrip("+")]
            except Exception:
                continue
        return result.dropna()

    def _official_or_last_date(self, symbol, contract_code, history):
        official = self.official_expiry.get(symbol, contract_code)
        return pd.Timestamp(official if official is not None else history.index.max()).normalize()

    # packaging
    def _empty_result(self, warnings):
        return {'series': {}, 'average': pd.Series(dtype=float), 'ticks': ([], []), 'warnings': warnings}

    def _package(self, series_out, ref_ticks, warnings):
        if not series_out:
            return self._empty_result(warnings)
        
        # Create matrix: Index = Days to Expiry, Columns = Years
        combined = pd.concat(
            [s['value'].rename(y) for y, s in series_out.items()], axis=1
        ).sort_index()

        average, std_years, rolling_std_path = self.fc.calculate_seasonality_features(combined)
        tickvals, ticktext = self._month_ticks(*ref_ticks) if ref_ticks else ([], [])
        
        return {
            'series': series_out, 
            'average': average, 
            'std': std_years,
            'rolling_std_path': rolling_std_path,
            'combined': combined,
            'ticks': (tickvals, ticktext), 
            'warnings': warnings
        }

    def _package_working_days(self, series_out, warnings, window_days):
        if not series_out:
            return self._empty_result(warnings)

        raw_combined = pd.concat(
            [s["value"].rename(y) for y, s in series_out.items()], axis=1
        ).sort_index()
        combined = raw_combined.interpolate(method="linear", limit_area="inside")
        average, std_years, rolling_std_path = self.fc.calculate_seasonality_features(combined)

        tickvals = [-window_days, -300, -200, -100, 0]
        tickvals = [t for t in tickvals if -window_days <= t <= 0]
        ticktext = ["Expiry" if tick == 0 else str(tick) for tick in tickvals]

        return {
            "series": series_out,
            "average": average,
            "std": std_years,
            "rolling_std_path": rolling_std_path,
            "raw_combined": raw_combined,
            "combined": combined,
            "ticks": (tickvals, ticktext),
            "warnings": warnings,
            "xaxis_title": "Working days to expiry",
        }

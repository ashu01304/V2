import re
import pandas as pd
from analysis.expiry import ExpiryEstimator


class Seasonality:
    def __init__(self, db, symbol):
        self.db = db
        self.symbol = symbol
        self.estimator = ExpiryEstimator(db)

    # ---------------- shared helpers ----------------

    def _fill_weekends(self, df, window_start, window_end):
        full_calendar = pd.date_range(window_start, window_end, freq='D')
        return df.reindex(full_calendar).interpolate(method='linear').dropna()

    def _month_ticks(self, offsets, dates):
        tickvals, ticktext, seen = [], [], set()
        for off, date in zip(offsets, dates):
            key = (date.year, date.month)
            if key not in seen:
                seen.add(key)
                tickvals.append(off)
                ticktext.append(date.strftime('%b'))
        return tickvals, ticktext

    def _leg_window(self, code, window_days):
        """Estimate expiry for `code` and return (expiry, window_start, window_end)."""
        expiry, _ = self.estimator.estimate_expiry(self.symbol, code)
        if expiry is None:
            return None, None, None
        today = pd.Timestamp(pd.Timestamp.now().date())
        expiry = pd.Timestamp(expiry).normalize()
        window_start = (expiry - pd.Timedelta(days=window_days)).normalize()
        window_end = min(expiry, today).normalize()
        return expiry, window_start, window_end

    # ---------------- outright seasonality ----------------

    def outright_seasonality(self, letter, start_year, end_year, window_days=400, interpolate=True):
        self.db.cursor.execute(
            "SELECT DISTINCT contract_code FROM seac_settlements WHERE symbol = ? AND contract_code LIKE ?",
            (self.symbol, f"{letter}%")
        )
        all_codes = [r[0] for r in self.db.cursor.fetchall()]
        codes = sorted(
            [c for c in all_codes if start_year <= 2000 + int(c[1:]) <= end_year],
            key=lambda c: int(c[1:])
        )
        if not codes:
            return self._empty_result([f"no contracts found for {letter} in [{start_year},{end_year}]"])

        hist = self.db.get_contract_history(self.symbol, codes)
        today = pd.Timestamp(pd.Timestamp.now().date())

        series_out, warnings, ref_ticks = {}, [], None

        for code, df in sorted(hist.items()):
            expiry, window_start, window_end = self._leg_window(code, window_days)
            if expiry is None or df.empty:
                warnings.append(f"{code}: skipped (no expiry estimate or no data)")
                continue

            df = df.copy()
            df.index = pd.to_datetime(df.index).normalize()
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

    # ---------------- expression seasonality ----------------

    def expression_seasonality(self, expression, start_year, end_year, window_days=400, interpolate=True):
        matches = re.findall(r"([FGHJKMNQUVXZ])(\d{2})", expression)
        if not matches:
            return self._empty_result(["could not parse expression"])

        ref_month, ref_yy = matches[0]
        ref_year_in_expr = int(ref_yy)
        today = pd.Timestamp(pd.Timestamp.now().date())

        series_out, warnings, ref_ticks = {}, [], None

        for s in range(start_year, end_year + 1):
            anchor_code = f"{ref_month}{s % 100:02d}"
            expiry, window_start, window_end = self._leg_window(anchor_code, window_days)
            if expiry is None:
                warnings.append(f"{s}: no expiry estimate for anchor {anchor_code}")
                continue

            leg_data, valid = {}, True
            for month, yy in matches:
                offset = int(yy) - ref_year_in_expr
                leg_code = f"{month}{(s + offset) % 100:02d}"
                hist = self.db.get_contract_history(self.symbol, [leg_code])
                if leg_code not in hist or hist[leg_code].empty:
                    valid = False
                    warnings.append(f"{s}: missing data for leg {leg_code}")
                    break

                df = hist[leg_code].copy()
                df.index = pd.to_datetime(df.index).normalize()
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

    # ---------------- packaging ----------------

    def _empty_result(self, warnings):
        return {'series': {}, 'average': pd.Series(dtype=float), 'ticks': ([], []), 'warnings': warnings}

    def _package(self, series_out, ref_ticks, warnings):
        if not series_out:
            return self._empty_result(warnings)
        combined = pd.concat(
            [s['value'].rename(y) for y, s in series_out.items()], axis=1
        ).sort_index()
        average = combined.mean(axis=1)
        tickvals, ticktext = self._month_ticks(*ref_ticks) if ref_ticks else ([], [])
        return {'series': series_out, 'average': average, 'ticks': (tickvals, ticktext), 'warnings': warnings}
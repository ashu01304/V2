import numpy as np
import pandas as pd
from analysis.expiry import OfficialExpiryLookup
from analysis.expression import evaluate_expression, parse_expression, shift_contract_year

class Seasonality:
    def __init__(self, db):
        self.db = db
        self.official_expiry = OfficialExpiryLookup()

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

    def working_day_expression_seasonality(self, symbol, expression, start_year, end_year=None, window_days=400):
        legs = parse_expression(expression)
        if not legs:
            return self._empty_result(["could not parse expression"])

        ref_code = legs[0][1]
        ref_month = ref_code[0]
        ref_year_in_expr = int(ref_code[1:])
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

            for _, contract in legs:
                leg_code = shift_contract_year(contract, s % 100 - ref_year_in_expr)
                hist = self.db.get_contract_history(symbol, [leg_code])
                if leg_code not in hist or hist[leg_code].empty:
                    valid = False
                    warnings.append(f"{s}: missing data for leg {leg_code}")
                    break

                df = hist[leg_code].copy()
                df.index = pd.to_datetime(df.index).normalize()
                leg_data[contract] = df["Close"]

            if not valid:
                continue

            leg_df = pd.DataFrame(leg_data).dropna()
            if leg_df.empty:
                warnings.append(f"{s}: no overlapping dates across legs")
                continue

            result = evaluate_expression(expression, leg_df)
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

    def _official_or_last_date(self, symbol, contract_code, history):
        official = self.official_expiry.get(symbol, contract_code)
        return pd.Timestamp(official if official is not None else history.index.max()).normalize()

    def _empty_result(self, warnings):
        return {'series': {},'combined': pd.DataFrame(), 'ticks': ([], []), 'warnings': warnings}

    def _package_working_days(self, series_out, warnings, window_days):
        if not series_out:
            return self._empty_result(warnings)

        raw_combined = pd.concat(
            [s["value"].rename(y) for y, s in series_out.items()], axis=1
        ).sort_index()
        combined = raw_combined.interpolate(method="linear", limit_area="inside")

        tickvals = [-window_days, -300, -200, -100, 0]
        tickvals = [t for t in tickvals if -window_days <= t <= 0]
        ticktext = ["Expiry" if tick == 0 else str(tick) for tick in tickvals]

        return {
            "series": series_out,
            "raw_combined": raw_combined,
            "combined": combined,
            "ticks": (tickvals, ticktext),
            "warnings": warnings,
            "xaxis_title": "Working days to expiry",
        }

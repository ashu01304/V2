import numpy as np
import pandas as pd
from analysis.expiry import OfficialExpiryLookup
from analysis.expression import parse_expression, shift_contract_year
from market_data.data_client import DataClient
from market_data.workflow_client import WorkflowLiveClient, full_structure

class Seasonality:
    def __init__(self, db, live=None, include_live=True):
        self.db = db
        self.live = live or WorkflowLiveClient('seasonality')
        self.data = DataClient(database=db)
        self.include_live = include_live
        self.official_expiry = OfficialExpiryLookup()

    def close(self):
        """Release this calculation's client; the caller owns the database reader."""
        self.data.close()

    def _contract_years(self, symbol, letter, start_year, end_year=None):
        years = sorted({2000 + int(code[1:])
                        for code in self.db.contract_codes(symbol, f"{letter}%")})
        years = [year for year in years if year >= start_year]
        if end_year is not None:
            years = [year for year in years if year <= end_year]
        return years

    def working_day_expression_seasonality(self,symbol,expression,start_year,end_year=None,window_days=400):
        legs=parse_expression(expression)
        if not legs:
            return self._empty_result(['could not parse expression'])
        anchor=legs[0][1]
        years=self._contract_years(symbol,anchor[0],start_year,end_year)
        series,warnings={},[]
        for year in years:
            shifted=[(weight,shift_contract_year(code,year%100-int(anchor[1:]))) for weight,code in legs]
            anchor_code=shifted[0][1]
            history=self.db.get_contract_history(symbol,[anchor_code]).get(anchor_code)
            if history is None or history.empty:
                warnings.append(f'{year}: missing anchor metadata for {anchor_code}')
                continue
            expiry=self.official_or_last_date(symbol,anchor_code,history)
            text=full_structure(symbol,self._format_expression(shifted))
            result=self.data.seac(text,start=(expiry-pd.offsets.BDay(window_days+5)).date(),end=expiry.date())['data']
            if result.empty:
                warnings.append(f'{year}: no overlapping settlement observations')
                continue
            values=result.set_index('timestamp')['price']
            live_date=None
            if year==max(years):
                live,warning=self._live_daily_overlay(symbol,expression,year%100-int(anchor[1:]),values.index.max(),expiry)
                if warning:
                    warnings.append(warning)
                if not live.empty:
                    live_date=live.index[-1]
                    values=pd.concat([values,live]).sort_index()
                    values=values[~values.index.duplicated(keep='last')]
            days=-np.busday_count(values.index.values.astype('datetime64[D]'),np.datetime64(expiry.date()))
            keep=(days>=-window_days)&(days<=0)
            values=values[keep]
            if values.empty:
                continue
            series[year]=pd.DataFrame(dict(value=values.values,date=values.index,
                is_live=values.index==live_date if live_date is not None else False),index=days[keep])
        return self._package_working_days(series,warnings,window_days)

    @staticmethod
    def refresh_live_result(result, year, quote, last_settlement, expiry, window_days):
        """Replace only the current live point using cached history and a quote."""
        if not quote or quote.get('value') is None:
            current = result.get('series', {}).get(year)
            if current is None or 'is_live' not in current or not current['is_live'].any():
                return result
            current = current.loc[~current['is_live']]
            raw = result.get('raw_combined', result['combined']).drop(columns=[year], errors='ignore')
            raw = raw.join(current['value'].rename(year), how='outer').sort_index().dropna(how='all')
            raw = raw.reindex(columns=result['combined'].columns)
            return {**result, 'series': {**result['series'], year: current}, 'raw_combined': raw,
                    'combined': raw.interpolate(method='linear', limit_area='inside')}
        if year not in result.get("series", {}):
            return result
        try:
            value = float(quote["value"])
            day = pd.Timestamp(quote["timestamp"])
            if pd.isna(day) or not np.isfinite(value) or value == -100:
                return result
            day = day.tz_localize(None).normalize()
            if not pd.Timestamp(last_settlement) < day <= pd.Timestamp(expiry):
                return result
            offset = -int(np.busday_count(day.date(), pd.Timestamp(expiry).date()))
            if not -window_days <= offset <= 0:
                return result
        except (KeyError, ValueError, TypeError, OverflowError):
            return result
        current = result["series"][year]
        previous = current[current["is_live"]] if "is_live" in current else current.iloc[:0]
        if not previous.empty:
            latest = previous.iloc[-1]
            if day < pd.Timestamp(latest["date"]):
                return result
            if day == pd.Timestamp(latest["date"]) and value == float(latest["value"]):
                return result
        current = current.loc[~current["is_live"]] if "is_live" in current else current
        point = pd.DataFrame({"value": [value], "date": [day], "is_live": [True]}, index=[offset])
        current = pd.concat([current, point]).sort_index()
        series = {**result["series"], year: current}
        # Reuse every historical-year series; only the current column changes.
        raw = result.get("raw_combined", result["combined"]).drop(columns=[year])
        raw = raw.join(current["value"].rename(year), how="outer").sort_index().dropna(how="all")
        raw = raw.reindex(columns=result["combined"].columns)
        return {**result, "series": series, "raw_combined": raw,
                "combined": raw.interpolate(method="linear", limit_area="inside")}

    def _live_daily_overlay(self,symbol,expression,year_shift,last_settlement_date,expiry):
        if not self.include_live or last_settlement_date is None:
            return pd.Series(dtype=float),None
        legs=[(w,shift_contract_year(code,year_shift)) for w,code in parse_expression(expression)]
        try:
            quote=self.live.expression(symbol,self._format_expression(legs))
            if quote.get('value') is None or not quote.get('timestamp'):
                return pd.Series(dtype=float),quote.get('message')
            day=pd.Timestamp(quote['timestamp']).tz_localize(None).normalize()
            if pd.Timestamp(last_settlement_date).normalize()<day<=expiry:
                return pd.Series([quote['value']],index=[day],name='live_overlay'),None
        except Exception as error:
            return pd.Series(dtype=float),f'Live overlay unavailable: {error}'
        return pd.Series(dtype=float),None

    @staticmethod
    def _format_expression(legs):
        parts = []
        for index, (coefficient, contract) in enumerate(legs):
            sign = "-" if coefficient < 0 else "+" if index else ""
            size = abs(coefficient)
            multiplier = "" if size == 1 else f"{size:g}*"
            parts.append(f"{sign}{multiplier}{contract}")
        return "".join(parts)

    def official_or_last_date(self, symbol, contract_code, history):
        official = self.official_expiry.get(
            "CL" if symbol == "CL-CO" else symbol, contract_code
        )
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

        return {
            "series": series_out,
            "raw_combined": raw_combined,
            "combined": combined,
            "warnings": warnings,
            "xaxis_title": "Working days to expiry",
        }

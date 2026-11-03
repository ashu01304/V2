"""Shared candle aggregation using each candle's central 80% of minute prices."""

from market_data.minute_resolver import interval_minutes


def percentile_candles(minutes, interval):
    """Build OHLC from observed prices inside each candle's inclusive P10–P90."""
    duration = interval_minutes(interval)
    if duration < 5:
        raise ValueError("Choose a candle interval of at least 5 minutes.")
    frequency = f"{duration}min"
    groups = minutes.groupby(minutes.index.floor(frequency))
    low = groups.transform('quantile', q=.1)
    high = groups.transform('quantile', q=.9)
    retained = minutes.loc[minutes.between(low, high, inclusive='both')]
    candles = (retained.resample(frequency, origin='epoch').ohlc()
               .dropna().rename_axis('timestamp').reset_index())
    return candles, len(retained)

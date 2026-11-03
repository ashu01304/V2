"""Shared, read-time removal of isolated spikes in minute price series."""

import pandas as pd


def spike_replacements(frame, tick=.01):
    """Find isolated jumps that reverse within two minutes on either side.

    This is a heuristic, not price validation. Sustained moves and observations
    without enough nearby history remain unchanged. All tests use original data.
    """
    columns = ['timestamp', 'original', 'replacement']
    if len(frame) < 7:
        return pd.DataFrame(columns=columns)
    times = pd.to_datetime(frame.timestamp, utc=True)
    series = pd.Series(frame.price.to_numpy(), index=times)
    median = series.rolling('11min', center=True, min_periods=7).median()
    residual = (series - median).abs()
    mad = residual.rolling('11min', center=True, min_periods=7).median()
    threshold = (8 * 1.4826 * mad).clip(lower=5 * tick)
    previous, following = series.shift(1), series.shift(-1)
    mask = ((residual > threshold)
            & ((series - previous).abs() > threshold)
            & ((series - following).abs() > threshold)
            & ((previous - following).abs() <= threshold)
            & ((series - previous) * (series - following) > 0))
    gap_before = times.diff().dt.total_seconds().to_numpy()
    gap_after = (-times.diff(-1)).dt.total_seconds().to_numpy()
    mask &= (gap_before > 0) & (gap_before <= 120) & (gap_after > 0) & (gap_after <= 120)
    return pd.DataFrame({'timestamp': series.index[mask],
                         'original': series[mask].to_numpy(),
                         'replacement': following[mask].to_numpy()}, columns=columns)


def normalize_minutes(frame, tick=.01):
    """Drop detected spike timestamps; never substitute or fill their values."""
    spikes = spike_replacements(frame, tick)
    keep = ~pd.to_datetime(frame.timestamp, utc=True).isin(spikes.timestamp)
    return frame.loc[keep].copy().reset_index(drop=True), len(spikes)

"""Session windows and convenient, on-demand access through the master reader."""

import pandas as pd

from .queries import MarketData, TIMEFRAMES, MAX_CANDLES

SESSION_TIMEZONE = "America/Chicago"
SESSION_MINUTES = 23 * 60


def session_window(past_days, now=None):
    if past_days is None or not float(past_days).is_integer() or past_days < 1:
        raise ValueError("Past N days must be a positive whole number.")
    end = pd.Timestamp.now(tz="UTC") if now is None else pd.Timestamp(now)
    if end.tzinfo is None:
        raise ValueError("The reference time must include a timezone.")
    end = end.tz_convert("UTC")
    local = end.tz_convert(SESSION_TIMEZONE).tz_localize(None)
    opening = (local - pd.Timedelta(hours=17)).normalize() + pd.Timedelta(hours=17)
    start = opening - pd.Timedelta(days=int(past_days) - 1)
    return start.tz_localize(SESSION_TIMEZONE).tz_convert("UTC"), end

def load_session_minutes(product, expression, past_days=16, now=None, database=None):
    """Request only the selected session window via the read-only interface."""
    start, end = session_window(past_days, now)
    owned = database is None
    db = MarketData() if owned else database
    try:
        frame = db.expression_minutes(product, expression, start.to_pydatetime(), end.to_pydatetime())
    finally:
        if owned:
            db.close()
    if frame.empty:
        raise ValueError("No common minute prices are available in the selected date window.")
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    return frame.set_index("timestamp")


def load_candles(product, expression, timeframe=15, max_candles=MAX_CANDLES, database=None):
    """Request bounded candles via the read-only interface."""
    owned = database is None
    db = MarketData() if owned else database
    try:
        return db.expression_candles(product, expression, timeframe, max_candles)
    finally:
        if owned:
            db.close()

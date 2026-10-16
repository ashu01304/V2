"""Reusable candlestick figures, live markers, and product price precision."""

from math import isfinite

import numpy as np
import pandas as pd
import plotly.graph_objects as go


def price_precision(product):
    return 2 if product in {"CL", "CO", "CL-CO"} else 3


def round_price(value, product):
    return float(np.round(float(value), price_precision(product)))


def make_intraday_live_trace(product, live_quote=None):
    """Build the candlestick chart's live marker without requesting data."""
    decimals = price_precision(product)
    trace = go.Scatter(
        x=[], y=[], mode="markers", name="Live price", uid="intraday-live",
        showlegend=False, cliponaxis=False,
        marker={"size": 12, "color": "#22c55e",
                "line": {"color": "#dcfce7", "width": 2}},
        hovertemplate=f"%{{x|%Y-%m-%d %H:%M:%S}} UTC<br>Live: "
                      f"%{{y:.{decimals}f}}<extra></extra>",
    )
    if live_quote:
        try:
            timestamp = pd.to_datetime(live_quote["timestamp"], utc=True)
            price = round_price(live_quote["value"], product)
            if timestamp.dayofweek < 5 and isfinite(price):
                trace.x, trace.y = [timestamp], [price]
        except (KeyError, TypeError, ValueError, OverflowError):
            pass
    return trace


def make_intraday_figure(candles, product, expression, timeframe=15,
                         live_quote=None, title=None):
    decimals = price_precision(product)
    candles = candles.round(decimals)
    figure = go.Figure(go.Candlestick(
        uid="intraday-candles",
        x=candles.index, open=candles["open"], high=candles["high"],
        low=candles["low"], close=candles["close"], name="Minute-derived prices",
        increasing_line_color="#22c55e", decreasing_line_color="#ef4444",
        hovertemplate=(
            f"%{{x|%Y-%m-%d %H:%M}} UTC<br>Open: %{{open:.{decimals}f}}"
            f"<br>High: %{{high:.{decimals}f}}<br>Low: %{{low:.{decimals}f}}"
            f"<br>Close: %{{close:.{decimals}f}}<extra></extra>"
        ),
    ))
    figure.add_trace(make_intraday_live_trace(product, live_quote))
    figure.update_layout(
        title=title or f"{product} {expression} | {timeframe}-minute prices",
        template="plotly_dark", height=650, hovermode="x unified", dragmode="zoom",
        paper_bgcolor="#111827", plot_bgcolor="#111827",
        font={"color": "#f8fafc"}, margin={"l": 65, "r": 35, "t": 60, "b": 55},
        uirevision=f"{product}:{expression}:{timeframe}",
        xaxis={"title": "Time (UTC)", "rangeslider": {"visible": False},
               "rangebreaks": [{"bounds": ["sat", "mon"]}]},
        yaxis={"tickformat": f".{decimals}f", "title": "Price"},
        legend={"orientation": "h", "y": 1.03, "x": 1, "xanchor": "right"},
    )
    return figure

"""Minute-by-minute overlays aligned to the Chicago 17:00 trading-session open."""

from hashlib import sha256
from math import isfinite

import pandas as pd
import plotly.graph_objects as go
from plotly.colors import qualitative

from analysis.intraday_chart import price_precision, round_price
from market_data.intraday import SESSION_TIMEZONE, SESSION_MINUTES


def session_lines(minutes):
    local = pd.DatetimeIndex(minutes.index).tz_convert(SESSION_TIMEZONE).tz_localize(None)
    frame = pd.DataFrame({
        "price": minutes["price"].to_numpy(),
        "minute": (local.hour * 60 + local.minute - 17 * 60) % 1440,
        "day": (local - pd.Timedelta(hours=17)).normalize() + pd.Timedelta(days=1),
    })
    frame = frame[(frame["minute"] < SESSION_MINUTES) & (frame["day"].dt.dayofweek < 5)]
    lines = []
    for day, values in frame.groupby("day", sort=True):
        samples = values.groupby("minute", sort=True)["price"].last()
        # Missing observations stay NaN; the chart connects only the recorded points.
        samples = samples.reindex(range(SESSION_MINUTES))
        observed = samples.dropna()
        if not observed.empty:
            lines.append((day, samples, float(observed.iloc[0]), int(observed.index[0])))
    return lines


def make_session_live_trace(minutes, product, zero_base, live_quote):
    """Return a visible live marker aligned to its own session's baseline."""
    decimals = price_precision(product)
    trace = go.Scatter(
        x=[], y=[], mode="markers", name="Live price", showlegend=False,
        uid="session-live",
        cliponaxis=False,
        marker={"size": 12, "color": "#22c55e",
                "line": {"color": "#dcfce7", "width": 2}},
        hovertemplate=(f"%{{customdata[0]}} Chicago<br>Live: %{{customdata[1]:.{decimals}f}}"
                       f"<br>Value: %{{y:.{decimals}f}}<extra></extra>"),
    )
    if not live_quote:
        return trace
    try:
        timestamp = pd.to_datetime(live_quote["timestamp"], utc=True)
        price = round_price(live_quote["value"], product)
        local = timestamp.tz_convert(SESSION_TIMEZONE).tz_localize(None)
        day = (local - pd.Timedelta(hours=17)).normalize() + pd.Timedelta(days=1)
        minute = (local.hour * 60 + local.minute - 17 * 60) % 1440
        baseline = next((baseline for session, _, baseline, _ in session_lines(minutes)
                         if session == day), None)
        if (isfinite(price) and minute < SESSION_MINUTES and day.dayofweek < 5
                and (not zero_base or baseline is not None)):
            trace.x = [minute + local.second / 60]
            trace.y = [round_price(price - round_price(baseline, product), product)
                       if zero_base else price]
            trace.customdata = [[f"{local:%d-%m %H:%M:%S}", price]]
    except (KeyError, TypeError, ValueError, OverflowError):
        pass
    return trace


def make_session_overlay(minutes, product, expression, zero_base=True, label=None,
                         live_quote=None):
    lines = session_lines(minutes)
    if not lines:
        raise ValueError("No weekday trading-session minutes are available in this window.")
    decimals = price_precision(product)
    figure = go.Figure()
    clocks = [f"{((17 * 60 + minute) // 60) % 24:02d}:{minute % 60:02d}"
              for minute in range(SESSION_MINUTES)]
    colors = qualitative.Plotly + qualitative.Dark24
    for index, (day, samples, baseline, opening_minute) in enumerate(lines):
        color = "#f8fafc" if index == len(lines) - 1 else colors[index % len(colors)]
        samples = samples.round(decimals)
        baseline = round_price(baseline, product)
        values = (samples - baseline).round(decimals) if zero_base else samples
        figure.add_trace(go.Scattergl(
            x=list(range(SESSION_MINUTES)), y=values.to_numpy(),
            mode="lines+markers", connectgaps=True,
            name=f"{day:%d-%m}",
            uid="session-" + sha256(f"{product}:{expression}:{day:%Y-%m-%d}".encode()).hexdigest()[:24],
            line={"color": color, "width": 1.8 if index == len(lines) - 1 else 1.2},
            marker={"color": color, "size": 3},
            customdata=[[clock, float(price), baseline, clocks[opening_minute]]
                        for clock, price in zip(clocks, samples)],
            hovertemplate=(
                f"%{{customdata[0]}} Chicago<br>Value: %{{y:.{decimals}f}}"
                f"<br>Price: %{{customdata[1]:.{decimals}f}}"
                f"<br>First available (%{{customdata[3]}}): %{{customdata[2]:.{decimals}f}}"
                "<extra>%{fullData.name}</extra>"
            ),
        ))
    live_trace = make_session_live_trace(minutes, product, zero_base, live_quote)
    if live_trace.x:
        figure.add_trace(live_trace)
    ticks = list(range(0, SESSION_MINUTES, 120)) + [SESSION_MINUTES]
    figure.update_layout(
        template="plotly_dark", paper_bgcolor="#111827", plot_bgcolor="#111827",
        height=680, hovermode="x unified", dragmode="zoom",
        margin={"l": 65, "r": 15, "t": 65, "b": 60},
        font={"color": "#f8fafc"},
        uirevision=f"{product}:{expression}:{bool(zero_base)}",
        xaxis={"range": [0, SESSION_MINUTES],
               "tickmode": "array", "tickvals": ticks,
               "ticktext": [f"{(17 + minute // 60) % 24:02d}:00" for minute in ticks]},
        yaxis={"tickformat": f".{decimals}f"},
        legend={"orientation": "h", "x": 0, "y": 1.02,
                "xanchor": "left", "yanchor": "bottom", "font": {"size": 11}},
    )
    if zero_base:
        figure.add_hline(y=0, line_color="#94a3b8", line_width=1)
    return figure

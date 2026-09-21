"""Plotly presentation for the selected expression."""

from collections import Counter

import plotly.graph_objects as go
from plotly.subplots import make_subplots

def make_intraday_figure(result, ranked=False, candle_reduction=0, show_average=True):
    candles = result["candles"].copy()
    midpoint = ((candles["high"] + candles["low"]) / 2).round(2)
    reduction = float(1 if candle_reduction is None else candle_reduction)
    if not 0 <= reduction < float("inf"):
        raise ValueError("Candle reduction must be a finite non-negative number")
    candles["high"] = (candles["high"] - reduction * 0.01).clip(lower=midpoint).round(2)
    candles["low"] = (candles["low"] + reduction * 0.01).clip(upper=midpoint).round(2)
    candles[["open", "close"]] = candles[["open", "close"]].clip(
        lower=candles["low"], upper=candles["high"], axis=0)
    threshold = result["z_score_threshold"]
    lookback = result["z_score_lookback"]
    timeframe = result["timeframe_minutes"]
    timestamps = candles.index.strftime("%Y-%m-%d %H:%M")
    figure = make_subplots(
        rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.03,
        row_heights=[0.8, 0.2],
    )
    figure.add_trace(go.Candlestick(
        x=timestamps,
        open=candles["open"], high=candles["high"],
        low=candles["low"], close=candles["close"],
        name=f"{timeframe}-minute price",
        text=[f"Days before CO expiry: {days}"
              for days in (result["expiry"] - candles.index.normalize()).days],
        hoverinfo="x+y+text+name",
        increasing_line_color="#22c55e",
        decreasing_line_color="#ef4444",
    ))
    figure.add_trace(go.Scatter(
        x=timestamps, y=midpoint,
        mode="lines", name="Candle midpoint", visible=show_average,
        line={"color": "#f59e0b", "width": 2},
        hovertemplate="Candle midpoint: %{y:.2f}<extra></extra>",
    ))
    upper = result["rank_upper"] if ranked else result["z_score_upper"]
    lower = result["rank_lower"] if ranked else result["z_score_lower"]
    upper_name = (f"{result['band_rank']}th highest of last "
                  f"{result['rank_lookback']} candles" if ranked else
                  f"Upper Z band (+{threshold:g} std, {lookback} candles)")
    lower_name = (f"{result['band_rank']}th lowest of last "
                  f"{result['rank_lookback']} candles" if ranked else
                  f"Lower Z band (-{threshold:g} std, {lookback} candles)")
    figure.add_trace(go.Scatter(
        x=timestamps, y=upper, mode="lines", name=upper_name,
        line={"color": "#fb7185", "width": 2},
        hovertemplate="Upper band: %{y}<extra></extra>",
    ))
    figure.add_trace(go.Scatter(
        x=timestamps, y=lower, mode="lines", name=lower_name,
        line={"color": "#34d399", "width": 2},
        hovertemplate="Lower band: %{y}<extra></extra>",
    ))
    trades = result["trades"]
    daily_wins = Counter()
    daily_losses = Counter()
    for trade in trades:
        if trade["outcome"] == "TP":
            daily_wins[trade["entry_time"].date()] += 1
        elif trade["outcome"] == "SL":
            daily_losses[trade["entry_time"].date()] += 1
    outcome_days = sorted(set(daily_wins) | set(daily_losses))
    outcome_x = []
    for day in outcome_days:
        positions = [index for index, value in enumerate(candles.index.date)
                     if value == day]
        outcome_x.append(timestamps[positions[len(positions) // 2]])
    daily_outcomes = [daily_wins[day] - daily_losses[day] for day in outcome_days]
    figure.add_trace(go.Bar(
        x=outcome_x, y=[abs(value) for value in daily_outcomes],
        name="Daily outcome", width=3, opacity=0.8,
        marker_color=["#22c55e" if value >= 0 else "#ef4444"
                      for value in daily_outcomes],
    ), row=2, col=1)
    for direction, color, symbol in (
        ("LONG", "#22c55e", "triangle-up"),
        ("SHORT", "#ef4444", "triangle-down"),
    ):
        selected = [trade for trade in trades if trade["direction"] == direction]
        figure.add_trace(go.Scatter(
            x=[trade["entry_time"].strftime("%Y-%m-%d %H:%M") for trade in selected],
            y=[trade["entry_price"] for trade in selected], mode="markers",
            name=f"{direction} entry",
            marker={"color": color, "size": 11, "symbol": symbol,
                    "line": {"color": "white", "width": 1}},
            hovertemplate=(f"{direction} entry: %{{y}}<extra></extra>"),
        ))

    figure.update_layout(
        title=(f"{result['symbol']} | {result['expression']} | "
               f"{timeframe}-minute candles"),
        template="plotly_dark", paper_bgcolor="black", plot_bgcolor="black",
        hovermode="x unified", height=850, xaxis_rangeslider_visible=False,
        legend={
            "orientation": "h", "yanchor": "bottom", "y": 1.02,
            "xanchor": "left", "x": 0,
        },
        margin={"t": 145},
        barmode="overlay",
    )
    figure.update_xaxes(
        title_text=f"Available {timeframe}-minute candles", gridcolor="#333",
        type="category", nticks=14,
    )
    figure.update_yaxes(title_text="Value", gridcolor="#333", row=1, col=1)
    figure.update_yaxes(visible=False, row=2, col=1)
    return figure


def make_moving_average_figure(result):
    timeframe = result["timeframe_minutes"]
    lookback = result["moving_average_lookback"]
    moving_average = result["trailing_moving_average"].dropna()
    timestamps = moving_average.index.strftime("%Y-%m-%d %H:%M")
    figure = go.Figure()
    figure.add_trace(go.Scatter(
        x=timestamps, y=moving_average, mode="lines",
        name=f"Absolute trailing moving average ({lookback} previous candles)",
        line={"color": "#f59e0b", "width": 2},
        hovertemplate="Absolute moving average: %{y}<extra></extra>",
    ))
    figure.update_layout(
        title=(f"{result['symbol']} | {result['expression']} | "
               f"{timeframe}-minute absolute trailing moving average"),
        template="plotly_dark", paper_bgcolor="black", plot_bgcolor="black",
        hovermode="x unified", height=450,
    )
    figure.update_xaxes(
        title_text=f"Available {timeframe}-minute candles", gridcolor="#333",
        type="category", nticks=14,
    )
    figure.update_yaxes(title_text="Absolute moving average", gridcolor="#333")
    return figure

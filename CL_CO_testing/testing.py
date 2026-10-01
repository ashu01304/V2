"""Simple intraday price bands and rolling candle-range totals."""

from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from dash import Dash, Input, Output, State, dcc, html

from CL_CO_testing.config import (
    DEFAULT_SYMBOL, DEFAULT_TIMEFRAME_MINUTES, DEFAULT_RANK_LOOKBACK,
    DEFAULT_BAND_RANK, TIMEFRAME_OPTIONS,
)
from CL_CO_testing.engine import (
    DEFAULT_STRATEGY, EXPRESSION_ROWS, load_intraday_series, ranked_bands,
)
from CL_CO_testing.universe import UNIVERSE
from product_config import PRODUCT_CONFIG
from market_data.config import DATABASE_PATH
from market_data.live_client import LiveMarketDataClient
from market_data.chart_updates import preserve_live_charts

app = Dash(__name__, assets_folder=str(Path(__file__).with_name("assets")))
app.title = "Range Bands"
CONTRACTS = [row["Contract"] for row in EXPRESSION_ROWS[:7]]
_database_version = None


def number_control(label, control_id, value, minimum=1, step=1):
    return html.Label([
        html.Span(label, style={"display": "block", "marginBottom": "5px"}),
        dcc.Input(
            id=control_id, type="number", min=minimum, step=step, value=value,
            debounce=True, persistence=True, persistence_type="local",
            style={"width": "120px", "padding": "6px 8px",
                   "backgroundColor": "white", "color": "#111827"}),
    ])


def dropdown_control(label, control_id, options, value):
    return html.Label([
        html.Span(label, style={"display": "block", "marginBottom": "5px"}),
        dcc.Dropdown(
            id=control_id, options=options, value=value, clearable=False,
            persistence=True, persistence_type="local",
            style={"width": "160px", "color": "black"}),
    ])


def whole_number(value, label, minimum=1):
    if value is None or not float(value).is_integer() or value < minimum:
        raise ValueError(f"{label} must be a whole number of at least {minimum}")
    return int(value)


def quantile_bounds(candles, lookback, lower_percentile, upper_percentile):
    lookback = whole_number(lookback, "Quantile lookback", 2)
    lower_percentile, upper_percentile = float(lower_percentile), float(upper_percentile)
    if not 0 <= lower_percentile < upper_percentile <= 100:
        raise ValueError("Quantile percentiles must satisfy 0 <= lower < upper <= 100")
    return (candles.high.shift(1).rolling(lookback).quantile(upper_percentile / 100),
            candles.low.shift(1).rolling(lookback).quantile(lower_percentile / 100))


def band_figure(candles, upper, lower, title, revision, candle_reduction,
                range_window=None):
    displayed = candles.copy()
    reduction = float(candle_reduction or 0)
    if not 0 <= reduction < float("inf"):
        raise ValueError("Candle reduction must be a finite non-negative number")
    midpoint = (candles.high + candles.low) / 2
    displayed["high"] = (candles.high - reduction * 0.01).clip(lower=midpoint)
    displayed["low"] = (candles.low + reduction * 0.01).clip(upper=midpoint)
    displayed[["open", "close"]] = displayed[["open", "close"]].clip(
        lower=displayed.low, upper=displayed.high, axis=0)
    timestamps = candles.index.strftime("%Y-%m-%d %H:%M").tolist()
    figure = make_subplots(
        rows=2 if range_window is not None else 1, cols=1, shared_xaxes=True,
        vertical_spacing=0.07,
        row_heights=[0.75, 0.25] if range_window is not None else [1])
    figure.add_trace(go.Candlestick(
        x=timestamps, open=displayed.open.tolist(), high=displayed.high.tolist(),
        low=displayed.low.tolist(), close=displayed.close.tolist(), name="Price"),
        row=1, col=1)
    for values, name, color in (
            (upper, "Upper band", "#fb7185"), (lower, "Lower band", "#34d399")):
        figure.add_trace(go.Scatter(
            x=timestamps, y=values.tolist(), mode="lines", name=name,
            line={"color": color, "width": 2}), row=1, col=1)
    if range_window is not None:
        # Use original candle highs/lows, independent of visual candle reduction.
        config = PRODUCT_CONFIG["CL" if DEFAULT_SYMBOL == "CL-CO" else DEFAULT_SYMBOL]
        tick_size = config["tick_size"]
        # Synthetic subtraction can leave tiny positive floating-point residues.
        # Normalize to whole ticks before deciding whether a range is non-zero.
        range_ticks = ((candles.high - candles.low) / tick_size).round().astype("Int64")
        nonzero_ticks = range_ticks[range_ticks > 0]
        total_ticks = nonzero_ticks.rolling(
            range_window, min_periods=range_window).sum()
        # Zero-range candles retain the latest total without consuming the window.
        totals = (total_ticks.reindex(candles.index).ffill() * tick_size).round(
            config["price_decimals"])
        figure.add_trace(go.Scatter(
            x=timestamps, y=totals.tolist(), mode="lines",
            name=f"Range sum ({range_window} non-zero candles)",
            line={"color": "#38bdf8", "width": 2},
            hovertemplate="Sum of non-zero high - low ranges: %{y:.4f}<extra></extra>"),
            row=2, col=1)
        figure.update_yaxes(title_text="Range sum", row=2, col=1, rangemode="tozero")
    figure.update_layout(
        title=title, template="plotly_dark", paper_bgcolor="black",
        plot_bgcolor="black", height=800 if range_window is not None else 550,
        hovermode="x unified", uirevision=revision,
        legend={"orientation": "h", "y": 1.04}, margin={"t": 100})
    figure.update_xaxes(type="category", nticks=14, rangeslider_visible=False)
    figure.update_yaxes(title_text="Price", row=1, col=1)
    return figure


controls_style = {"display": "flex", "gap": "20px", "flexWrap": "wrap",
                  "alignItems": "end", "padding": "18px 24px",
                  "background": "#111827", "color": "white"}

app.layout = html.Div([
    html.Div([
        dropdown_control("Contract", "contract", CONTRACTS,
                         CONTRACTS[0] if CONTRACTS else None),
        dropdown_control("Timeframe", "timeframe",
                         [{"label": f"{value} min", "value": value}
                          for value in TIMEFRAME_OPTIONS], DEFAULT_TIMEFRAME_MINUTES),
        number_control("Ranked-band lookback", "rank-lookback", DEFAULT_RANK_LOOKBACK),
        number_control("Band rank", "band-rank", DEFAULT_BAND_RANK),
        number_control("Range sum (non-zero candles)", "range-window", 3),
        number_control("Candle reduction (ticks)", "candle-reduction", 0, minimum=0),
    ], style=controls_style),
    html.Div(id="selected-expression", style={"padding": "12px 24px"}),
    html.Div(id="chart-error", style={"color": "#f87171", "padding": "0 24px"}),
    dcc.Graph(id="ranked-chart"),
    html.H3("Rolling high/low quantiles", style={"padding": "0 24px"}),
    html.Div([
        number_control("Lookback (candles)", "quantile-lookback", 40, minimum=2),
        number_control("Lower percentile", "lower-percentile", 10, minimum=0),
        number_control("Upper percentile", "upper-percentile", 90, minimum=0),
    ], style=controls_style),
    dcc.Graph(id="quantile-chart"),
    dcc.Interval(id="live-refresh", interval=10_000, n_intervals=0),
], style={"background": "black", "color": "white", "minHeight": "100vh"})


@app.callback(
    Output("ranked-chart", "figure"), Output("quantile-chart", "figure"),
    Output("chart-error", "children"), Output("selected-expression", "children"),
    Input("contract", "value"), Input("timeframe", "value"),
    Input("rank-lookback", "value"), Input("band-rank", "value"),
    Input("range-window", "value"), Input("candle-reduction", "value"),
    Input("quantile-lookback", "value"), Input("lower-percentile", "value"),
    Input("upper-percentile", "value"), Input("live-refresh", "n_intervals"),
    State("ranked-chart", "figure"), State("quantile-chart", "figure"),
)
@preserve_live_charts([0, 1])
def update_expression(contract, timeframe, rank_lookback, band_rank,
                      range_window, candle_reduction, quantile_lookback,
                      lower_percentile, upper_percentile, _):
    global _database_version
    try:
        if contract not in CONTRACTS:
            raise ValueError("Select a contract")
        range_window = whole_number(range_window, "Range sum window")
        lookback = whole_number(rank_lookback, "Ranked-band lookback")
        rank = whole_number(band_rank, "Band rank")
        expression = UNIVERSE["contracts"][contract][DEFAULT_STRATEGY]
        version = DATABASE_PATH.stat().st_mtime_ns
        if version != _database_version:
            load_intraday_series.cache_clear()
            _database_version = version
        candles = load_intraday_series(DEFAULT_SYMBOL, expression, timeframe)
        upper, lower = ranked_bands(candles, lookback, rank)
        title = f"{DEFAULT_SYMBOL} | {expression} | {timeframe}-minute candles"
        revision = f"{DEFAULT_SYMBOL}:{expression}:{timeframe}"
        ranked = band_figure(
            candles, upper, lower, title, revision, candle_reduction, range_window)
        upper, lower = quantile_bounds(
            candles, quantile_lookback, lower_percentile, upper_percentile)
        quantiles = band_figure(
            candles, upper, lower, "Rolling high/low quantiles",
            revision + ":quantiles", candle_reduction)
        live_status = ""
        client = LiveMarketDataClient(timeout=2)
        try:
            quote = client.expression(DEFAULT_SYMBOL, expression)
            quote_time = pd.Timestamp(quote["timestamp"])
            candle_time = quote_time.floor(f"{int(timeframe)}min")
            for figure in (ranked, quantiles):
                figure.add_trace(go.Scatter(
                    x=[candle_time.strftime("%Y-%m-%d %H:%M")],
                    y=[float(quote["value"])], mode="markers", name="Live price",
                    marker={"color": "#22c55e", "size": 12},
                    customdata=[[quote_time.strftime("%Y-%m-%d %H:%M:%S UTC")]],
                    hovertemplate="Live: %{y:.4f}<br>%{customdata[0]}<extra></extra>"),
                    row=1, col=1)
            title += f" | Live: {float(quote['value']):.4f} | {quote_time:%H:%M:%S} UTC"
        except Exception as error:
            live_status = f"Live quote unavailable: {error}"
        finally:
            client.close()
        return ranked, quantiles, live_status, title
    except Exception as error:
        return go.Figure(), go.Figure(), str(error), ""


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8051, debug=False)

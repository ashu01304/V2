"""Dash interface for interactive range testing."""

from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from dash import Dash, Input, Output, dash_table, dcc, html

from CL_CO_testing.config import *
from CL_CO_testing.engine import (
    DEFAULT_STRATEGY, EXPRESSION_ROWS, calculate_chart, ranked_chart,
)
from CL_CO_testing.charts import make_intraday_figure

app = Dash(__name__, assets_folder=str(Path(__file__).with_name("assets")))
app.title = "Range Testing"
CONTRACTS = [row["Contract"] for row in EXPRESSION_ROWS[:7]]


def range_figure(result, upper, lower, title):
    candles = result["candles"]
    timestamps = candles.index.strftime("%Y-%m-%d %H:%M")
    figure = go.Figure([
        go.Candlestick(x=timestamps, open=candles.open, high=candles.high,
                       low=candles.low, close=candles.close, name="Price"),
        go.Scatter(x=timestamps, y=upper, mode="lines", name="Upper bound",
                   line={"color": "#fb7185", "width": 2}),
        go.Scatter(x=timestamps, y=lower, mode="lines", name="Lower bound",
                   line={"color": "#34d399", "width": 2}),
    ])
    figure.update_layout(
        title=title, template="plotly_dark", paper_bgcolor="black",
        plot_bgcolor="black", height=550, hovermode="x unified",
        xaxis_rangeslider_visible=False, yaxis_tickformat=".2f",
        legend={"orientation": "h", "y": 1.02},
    )
    figure.update_xaxes(type="category", nticks=14)
    return figure


def median_mad_bounds(candles, lookback, multiplier):
    lookback, multiplier = int(lookback), float(multiplier)
    values = candles.close.shift(1)
    middle = values.rolling(lookback).median()
    mad = values.rolling(lookback).apply(
        lambda window: np.median(np.abs(window - np.median(window))), raw=True)
    width = multiplier * 1.4826 * mad
    return middle + width, middle - width


def quantile_bounds(candles, lookback, lower_percentile, upper_percentile):
    lookback = int(lookback)
    lower_percentile, upper_percentile = float(lower_percentile), float(upper_percentile)
    if not 0 <= lower_percentile < upper_percentile <= 100:
        raise ValueError("Quantile percentiles must satisfy 0 <= lower < upper <= 100")
    return (candles.high.shift(1).rolling(lookback).quantile(upper_percentile / 100),
            candles.low.shift(1).rolling(lookback).quantile(lower_percentile / 100))


def ewma_bounds(candles, mean_span, volatility_span, multiplier):
    mean_span, volatility_span = int(mean_span), int(volatility_span)
    values = candles.close.shift(1)
    middle = values.ewm(span=mean_span, min_periods=mean_span, adjust=False).mean()
    volatility = ((values - middle) ** 2).ewm(
        span=volatility_span, min_periods=volatility_span, adjust=False).mean() ** 0.5
    width = float(multiplier) * volatility
    return middle + width, middle - width


def swing_bounds(candles, confirmation, retained):
    confirmation, retained = int(confirmation), int(retained)
    peaks, valleys, upper, lower = [], [], [], []
    highs, lows = candles.high.to_numpy(), candles.low.to_numpy()
    for index in range(len(candles)):
        pivot = index - 1 - confirmation
        if pivot >= confirmation:
            window = slice(pivot - confirmation, pivot + confirmation + 1)
            if highs[pivot] == np.max(highs[window]):
                peaks.append(highs[pivot])
            if lows[pivot] == np.min(lows[window]):
                valleys.append(lows[pivot])
        upper.append(np.median(peaks[-retained:]) if len(peaks) >= retained else np.nan)
        lower.append(np.median(valleys[-retained:]) if len(valleys) >= retained else np.nan)
    return pd.Series(upper, index=candles.index), pd.Series(lower, index=candles.index)


def session_bounds(candles, history_sessions, lower_percentile, upper_percentile):
    history_sessions = int(history_sessions)
    lower_percentile, upper_percentile = float(lower_percentile), float(upper_percentile)
    if not 0 <= lower_percentile < upper_percentile <= 100:
        raise ValueError("Session percentiles must satisfy 0 <= lower < upper <= 100")
    local = candles.index.tz_convert("America/Chicago")
    session = pd.Series((local - pd.Timedelta(hours=17)).date, index=candles.index)
    slot = pd.Series((local.hour * 60 + local.minute - 17 * 60) % 1440,
                     index=candles.index)
    session_open = candles.open.groupby(session).transform("first")
    change = candles.close - session_open
    minimum = min(3, history_sessions)
    lower_change = change.groupby(slot).transform(
        lambda values: values.shift(1).rolling(history_sessions, min_periods=minimum)
        .quantile(lower_percentile / 100))
    upper_change = change.groupby(slot).transform(
        lambda values: values.shift(1).rolling(history_sessions, min_periods=minimum)
        .quantile(upper_percentile / 100))
    return session_open + upper_change, session_open + lower_change


def number_control(label, control_id, value, minimum=0.1, step=0.1):
    return html.Label([
        html.Span(label, style={"display": "block", "marginBottom": "5px"}),
        dcc.Input(
            id=control_id, type="number", min=minimum, step=step, value=value,
            debounce=True, persistence=True, persistence_type="local",
            style={
                "width": "110px", "padding": "6px 8px", "fontWeight": "600",
                "backgroundColor": "white", "color": "#111827",
                "border": "1px solid #94a3b8", "borderRadius": "4px",
            },
        ),
    ])


def dropdown_control(label, control_id, options, value, width="145px"):
    return html.Label([
        html.Span(label, style={"display": "block", "marginBottom": "5px"}),
        dcc.Dropdown(id=control_id, options=options, value=value, clearable=False,
                     persistence=True, persistence_type="local",
                     style={"width": width, "color": "black"}),
    ])


def range_section(title, controls, graph_id):
    return html.Div([
        html.H3(title, style={"margin": "0", "color": "#f97316"}),
        html.Div(controls, style={"display": "flex", "gap": "20px",
                                 "alignItems": "end", "flexWrap": "wrap"}),
        dcc.Loading(dcc.Graph(id=graph_id, style={"width": "80%", "margin": "auto"}),
                    type="circle"),
    ], style={"padding": "16px 20px", "background": "#111827", "color": "white",
              "borderTop": "1px solid #334155"})


app.layout = html.Div([
    html.Div([
        html.Strong("Chart settings", style={"alignSelf": "center", "color": "#f97316"}),
        dropdown_control(
            "Timeframe", "timeframe",
            [
                {"label": "30 min", "value": 30},
                {"label": "60 min", "value": 60},
                {"label": "240 min", "value": 240},
            ],
            DEFAULT_TIMEFRAME_MINUTES, "125px",
        ),
        dcc.Checklist(id="show-average", options=[{"label": "Show average line", "value": "show"}],
                      value=["show"]),
        number_control("Candle reduction (ticks)", "candle-reduction", 1, minimum=0, step=1),
        number_control("TP (ticks)", "tp-ticks", DEFAULT_TP_TICKS),
        number_control("SL (ticks)", "sl-ticks", DEFAULT_SL_TICKS),
        number_control(
            "Minimum trade gap (candles)", "minimum-trade-gap",
            DEFAULT_MIN_TRADE_GAP_CANDLES,
        ),
        number_control(
            "Max parallel trades", "max-parallel-trades",
            DEFAULT_MAX_PARALLEL_POSITIONS,
        ),
        number_control(
            "Max consecutive same direction", "max-consecutive-direction",
            DEFAULT_MAX_CONSECUTIVE_DIRECTION,
        ),
        number_control("Days before CO expiry", "days-before-expiry",
                       DEFAULT_DAYS_BEFORE_EXPIRY, minimum=1, step=1),
        number_control("No-trade days before CO expiry", "no-trade-days",
                       DEFAULT_NO_TRADE_DAYS_BEFORE_EXPIRY, minimum=0, step=1),
    ], style={
        "display": "grid",
        "gridTemplateColumns": "repeat(auto-fit, minmax(145px, 1fr))",
        "alignItems": "end", "gap": "16px", "padding": "18px 24px",
        "background": "#111827", "color": "white",
        "borderBottom": "1px solid #334155",
    }),
    html.Div(id="selected-expression", style={
        "padding": "12px 20px", "background": "#0f172a", "color": "white",
        "fontSize": "18px", "fontWeight": "600",
    }),
    dash_table.DataTable(
        id="expression-table",
        columns=[{"name": "Strategy", "id": "Strategy"}]
                + [{"name": contract, "id": contract} for contract in CONTRACTS],
        data=[{"Strategy": "Chart 2"}],
        active_cell={"row": 0, "column": 1, "column_id": CONTRACTS[0],
                     "row_id": None},
        fixed_rows={"headers": True},
        style_table={"width": "fit-content", "maxWidth": "100%",
                     "margin": "0", "overflowX": "auto", "maxHeight": "420px",
                     "border": "1px solid #334155"},
        style_header={"backgroundColor": "#1e293b", "color": "white",
                      "fontWeight": "700"},
        style_cell={"backgroundColor": "#0f172a", "color": "white",
                    "border": "1px solid #334155", "padding": "7px",
                    "fontSize": "11px", "width": "250px",
                    "minWidth": "250px", "maxWidth": "250px",
                    "textAlign": "center",
                    "overflow": "hidden", "textOverflow": "ellipsis"},
        css=[{
            "selector": ".dash-spreadsheet-container .dash-spreadsheet-inner",
            "rule": "width: fit-content; min-width: 0;",
        }, {
            "selector": ".dash-spreadsheet-container table",
            "rule": "width: auto; table-layout: fixed;",
        }],
        style_data_conditional=[
            {"if": {"column_id": "Strategy"}, "width": "90px",
             "minWidth": "90px", "maxWidth": "90px"},
            {"if": {"state": "active"}, "backgroundColor": "#1d4ed8",
             "border": "2px solid #93c5fd"},
        ],
    ),
    html.Div(id="chart-error", style={"color": "#f87171", "padding": "0 20px"}),
    html.Div([
        number_control("Ranked-band lookback", "rank-lookback",
                       DEFAULT_RANK_LOOKBACK, minimum=1, step=1),
        number_control("Band rank", "band-rank", DEFAULT_BAND_RANK,
                       minimum=1, step=1),
    ], style={"display": "flex", "justifyContent": "center", "gap": "20px",
              "padding": "12px", "background": "#111827", "color": "white"}),
    html.Div(id="ranked-trade-summary", style={
        "display": "flex", "gap": "28px", "padding": "12px 20px",
        "background": "#0f172a", "color": "white", "fontSize": "18px",
    }),
    dcc.Loading(dcc.Graph(id="ranked-chart", style={"width": "80%", "margin": "auto"}), type="circle"),
    range_section("1. Rolling median + MAD", [
        number_control("Lookback (candles)", "mad-lookback", 40, minimum=2, step=1),
        number_control("MAD multiplier", "mad-multiplier", 2, minimum=0.1, step=0.1),
    ], "mad-chart"),
    range_section("2. Rolling high/low quantiles", [
        number_control("Lookback (candles)", "quantile-lookback", 40, minimum=2, step=1),
        number_control("Lower percentile", "lower-percentile", 10, minimum=0, step=1),
        number_control("Upper percentile", "upper-percentile", 90, minimum=0, step=1),
    ], "quantile-chart"),
    range_section("3. EWMA adaptive bands", [
        number_control("Mean span", "ewma-mean-span", 40, minimum=2, step=1),
        number_control("Volatility span", "ewma-volatility-span", 20, minimum=2, step=1),
        number_control("Width multiplier", "ewma-multiplier", 2, minimum=0.1, step=0.1),
    ], "ewma-chart"),
    range_section("4. Confirmed swing levels", [
        number_control("Confirmation candles", "swing-confirmation", 3, minimum=1, step=1),
        number_control("Retained swings", "retained-swings", 5, minimum=1, step=1),
    ], "swing-chart"),
    range_section("5. Historical time-of-session envelope", [
        number_control("Historical sessions", "history-sessions", 20, minimum=1, step=1),
        number_control("Lower percentile", "session-lower-percentile", 10,
                       minimum=0, step=1),
        number_control("Upper percentile", "session-upper-percentile", 90,
                       minimum=0, step=1),
    ], "session-chart"),
], style={"background": "black", "minHeight": "100vh", "margin": "0"})


@app.callback(
    Output("ranked-chart", "figure"),
    Output("mad-chart", "figure"),
    Output("quantile-chart", "figure"),
    Output("ewma-chart", "figure"),
    Output("swing-chart", "figure"),
    Output("session-chart", "figure"),
    Output("chart-error", "children"),
    Output("selected-expression", "children"),
    Output("ranked-trade-summary", "children"),
    Output("expression-table", "data"),
    Output("expression-table", "columns"),
    Input("expression-table", "active_cell"),
    Input("timeframe", "value"),
    Input("rank-lookback", "value"),
    Input("band-rank", "value"),
    Input("tp-ticks", "value"),
    Input("sl-ticks", "value"),
    Input("minimum-trade-gap", "value"),
    Input("max-parallel-trades", "value"),
    Input("max-consecutive-direction", "value"),
    Input("days-before-expiry", "value"),
    Input("no-trade-days", "value"),
    Input("candle-reduction", "value"),
    Input("show-average", "value"),
    Input("mad-lookback", "value"),
    Input("mad-multiplier", "value"),
    Input("quantile-lookback", "value"),
    Input("lower-percentile", "value"),
    Input("upper-percentile", "value"),
    Input("ewma-mean-span", "value"),
    Input("ewma-volatility-span", "value"),
    Input("ewma-multiplier", "value"),
    Input("swing-confirmation", "value"),
    Input("retained-swings", "value"),
    Input("history-sessions", "value"),
    Input("session-lower-percentile", "value"),
    Input("session-upper-percentile", "value"),
)
def update_expression(active_cell, timeframe, rank_lookback, band_rank,
                      tp_ticks, sl_ticks,
                      minimum_trade_gap, max_parallel_trades,
                      max_consecutive_direction,
                      days_before_expiry, no_trade_days, candle_reduction, show_average,
                      mad_lookback, mad_multiplier, quantile_lookback,
                      lower_percentile, upper_percentile, ewma_mean_span,
                      ewma_volatility_span, ewma_multiplier, swing_confirmation,
                      retained_swings, history_sessions, session_lower_percentile,
                      session_upper_percentile):
    symbol = DEFAULT_SYMBOL
    try:
        if not active_cell or active_cell.get("column_id") == "Strategy":
            raise ValueError("Click a contract cell in the table")
        contract = active_cell["column_id"]
        strategy = DEFAULT_STRATEGY
        timeframe = (DEFAULT_TIMEFRAME_MINUTES if timeframe is None else timeframe)
        tp_ticks = DEFAULT_TP_TICKS if tp_ticks is None else tp_ticks
        sl_ticks = DEFAULT_SL_TICKS if sl_ticks is None else sl_ticks
        minimum_trade_gap = (
            DEFAULT_MIN_TRADE_GAP_CANDLES
            if minimum_trade_gap is None else minimum_trade_gap
        )
        max_parallel_trades = (
            DEFAULT_MAX_PARALLEL_POSITIONS
            if max_parallel_trades is None else max_parallel_trades
        )
        max_consecutive_direction = (
            DEFAULT_MAX_CONSECUTIVE_DIRECTION
            if max_consecutive_direction is None else max_consecutive_direction
        )
        days_before_expiry = (DEFAULT_DAYS_BEFORE_EXPIRY
                              if days_before_expiry is None else days_before_expiry)
        no_trade_days = (DEFAULT_NO_TRADE_DAYS_BEFORE_EXPIRY
                         if no_trade_days is None else no_trade_days)
        result = calculate_chart(
            symbol, contract, strategy,
            DEFAULT_Z_SCORE_THRESHOLD, DEFAULT_Z_SCORE_LOOKBACK, timeframe,
            days_before_expiry, no_trade_days,
            tp_ticks, sl_ticks, minimum_trade_gap, max_parallel_trades,
            max_consecutive_direction, DEFAULT_MOVING_AVERAGE_LOOKBACK,
        )
        ranked = ranked_chart(
            result,
            DEFAULT_RANK_LOOKBACK if rank_lookback is None else rank_lookback,
            DEFAULT_BAND_RANK if band_rank is None else band_rank,
        )
        candles = result["candles"]
        mad_upper, mad_lower = median_mad_bounds(candles, mad_lookback, mad_multiplier)
        quantile_upper, quantile_lower = quantile_bounds(
            candles, quantile_lookback, lower_percentile, upper_percentile)
        ewma_upper, ewma_lower = ewma_bounds(
            candles, ewma_mean_span, ewma_volatility_span, ewma_multiplier)
        swing_upper, swing_lower = swing_bounds(
            candles, swing_confirmation, retained_swings)
        session_upper, session_lower = session_bounds(
            candles, history_sessions, session_lower_percentile,
            session_upper_percentile)
        experimental_figures = (
            range_figure(result, mad_upper, mad_lower, "Rolling median + MAD"),
            range_figure(result, quantile_upper, quantile_lower,
                         "Rolling high/low quantiles"),
            range_figure(result, ewma_upper, ewma_lower, "EWMA adaptive bands"),
            range_figure(result, swing_upper, swing_lower, "Confirmed swing levels"),
            range_figure(result, session_upper, session_lower,
                         "Historical time-of-session envelope"),
        )
        selected = f"Selected: {symbol} / {contract} / {strategy} / {result['expression']}"
        ranked_summary = [
            html.Span(f"Total trades: {ranked['total_trades']}"),
            html.Span(f"Resolved: {ranked['resolved_trades']}"),
            html.Span(f"Open: {ranked['open_trades']}"),
            html.Span(f"Win rate: {ranked['win_rate']:.1%}"
                      if ranked["win_rate"] is not None else "Win rate: N/A"),
            html.Span(f"Total PnL: {ranked['total_pnl_ticks']:+d} ticks"),
        ]
        chart_2_row = {"Strategy": "Chart 2"}
        valid_contracts = []
        for item_contract in CONTRACTS:
            try:
                item = result if item_contract == contract else calculate_chart(
                    symbol, item_contract, strategy,
                    DEFAULT_Z_SCORE_THRESHOLD, DEFAULT_Z_SCORE_LOOKBACK, timeframe,
                    days_before_expiry, no_trade_days,
                    tp_ticks, sl_ticks, minimum_trade_gap, max_parallel_trades,
                    max_consecutive_direction, DEFAULT_MOVING_AVERAGE_LOOKBACK,
                )
            except ValueError:
                continue
            valid_contracts.append(item_contract)
            item_ranked = ranked_chart(
                item,
                DEFAULT_RANK_LOOKBACK if rank_lookback is None else rank_lookback,
                DEFAULT_BAND_RANK if band_rank is None else band_rank,
            )
            ranked_win = (f"{item_ranked['win_rate']:.0%}"
                          if item_ranked["win_rate"] is not None else "N/A")
            chart_2_row[item_contract] = (
                f"Win : {ranked_win} | PnL : {item_ranked['total_pnl_ticks']:+d} | "
                f"{item_ranked['total_trades']} Trades"
            )
        return (
            make_intraday_figure(ranked, ranked=True, candle_reduction=candle_reduction,
                                 show_average=bool(show_average)),
            *experimental_figures,
            "", selected,
            ranked_summary, [chart_2_row],
            [{"name": "Strategy", "id": "Strategy"}] + [
                {"name": item, "id": item} for item in valid_contracts
            ],
        )
    except Exception as error:
        return (
            go.Figure(), go.Figure(), go.Figure(), go.Figure(), go.Figure(),
            go.Figure(), str(error),
            "No expression selected", [],
            [{"Strategy": "Chart 2"}],
            [{"name": "Strategy", "id": "Strategy"}],
        )


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8051, debug=False)

"""Interactive CL-CO and NG structure price dashboard."""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
from dash import Dash, Input, Output, Patch, State, ctx, dcc, html, no_update

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analysis.contract_universe import (
    DEFAULT_CONFIG_PATH, DEFAULT_EXPIRY_PATH, build_universe, latest_market_date,
    load_config, load_expiries, select_anchors,
)
from CL_CO_testing.engine import load_intraday_series
from market_data.live_client import LiveMarketDataClient


PRODUCTS = {
    "CL-CO": {"config_symbol": "CL", "decimals": 2},
    "NG": {"config_symbol": "NG", "decimals": 3},
}
STRUCTURES = {
    "spread": ("Spread", "MS"),
    "fly": ("Fly", "MF"),
    "defly": ("Defly", "MDF"),
}
MAX_CANDLES = 2_000
MAX_MONTHS = 4
CHART_TIMEZONE = "UTC"


def product_universe(product: str) -> dict:
    base_config = load_config(DEFAULT_CONFIG_PATH)
    config = replace(base_config, symbol=PRODUCTS[product]["config_symbol"])
    effective_date = latest_market_date() or pd.Timestamp.now().date()
    expiries = load_expiries(config.symbol, DEFAULT_EXPIRY_PATH)
    anchors = select_anchors(expiries, config, effective_date)
    return build_universe(expiries, anchors, config.custom_structures)


def contracts_for(product: str, structure: str) -> list[str]:
    universe = product_universe(product)
    suffix = STRUCTURES[structure][1]
    return [
        anchor for anchor, expressions in universe["contracts"].items()
        if f"1{suffix}" in expressions
    ]


def expression_for(product: str, structure: str, contract: str) -> str:
    universe = product_universe(product)
    return universe["contracts"][contract][f"1{STRUCTURES[structure][1]}"]



def live_coordinates(quote: dict | None) -> tuple[list, list]:
    if quote is None:
        return [], []
    timestamp = pd.Timestamp(quote["timestamp"])
    timestamp = (timestamp.tz_localize(CHART_TIMEZONE) if timestamp.tzinfo is None
                 else timestamp.tz_convert(CHART_TIMEZONE))
    # Use the same timezone for candles, axis labels, and weekend live quotes.
    if timestamp.dayofweek >= 5:
        return [], []
    return [timestamp.isoformat()], [float(quote["value"])]

def make_figure(candles: pd.DataFrame, product: str, structure: str,
                contract: str, timeframe: int, live_quote: dict | None = None,
                previous_view: dict | None = None) -> go.Figure:
    decimals = PRODUCTS[product]["decimals"]
    figure = go.Figure(go.Candlestick(
        x=candles.index, open=candles["open"], high=candles["high"],
        low=candles["low"], close=candles["close"], name=product,
        hovertemplate=(
            f"%{{x|%Y-%m-%d %H:%M}}<br>Open: %{{open:.{decimals}f}}"
            f"<br>High: %{{high:.{decimals}f}}<br>Low: %{{low:.{decimals}f}}"
            f"<br>Close: %{{close:.{decimals}f}}<extra></extra>"
        ),
    ))
    # Reserve a stable trace even when the live service has no quote yet.
    live_x, live_y = live_coordinates(live_quote)
    figure.add_trace(go.Scatter(
        x=live_x, y=live_y,
        mode="markers", name="Live price", uid="liveprice",
        marker={"size": 15, "color": "#22c55e",
                "line": {"color": "#dcfce7", "width": 2}},
        hovertemplate=f"%{{x|%Y-%m-%d %H:%M:%S}}<br>Live price: "
                      f"%{{y:.{decimals}f}}<extra></extra>",
    ))
    figure.update_layout(
        title=f"{product} {STRUCTURES[structure][0]} {contract} | "
              f"{timeframe}-minute prices",
        template="plotly_dark", height=720, hovermode="x unified",
        paper_bgcolor="#111827", plot_bgcolor="#111827",
        font={"color": "#f8fafc"}, margin={"l": 70, "r": 150, "t": 80, "b": 60},
        uirevision=f"{product}:{structure}:{contract}:{timeframe}",
        xaxis_rangeslider_visible=False,
        xaxis_title="Time (UTC)",
        xaxis_rangebreaks=[{"bounds": ["sat", "mon"]}],
    )
    if previous_view:
        x_range = previous_view.get("xaxis.range")
        if isinstance(x_range, (list, tuple)) and len(x_range) == 2:
            figure.update_xaxes(range=list(x_range), autorange=False)
    return figure


app = Dash(__name__, assets_folder=str(Path(__file__).resolve().parent / "assets"))
app.title = "Structure Prices"
CONTROL_STYLE = {
    "backgroundColor": "#1f2937", "color": "#f8fafc",
    "border": "1px solid #64748b", "borderRadius": "5px", "padding": "7px",
}
app.layout = html.Div([
    html.H1("Structure Prices", style={"color": "#f8fafc"}),
    html.P("Choose a product, structure, and contract. Weekend points are hidden.",
           style={"color": "#cbd5e1"}),
    html.Div([
        html.Label(["Product", dcc.Dropdown(
            id="product", options=[{"label": key, "value": key} for key in PRODUCTS],
            value="CL-CO", clearable=False, style={**CONTROL_STYLE, "width": "150px"},
        )], style={"color": "#e2e8f0"}),
        html.Label(["Structure", dcc.Dropdown(
            id="structure",
            options=[{"label": label, "value": key}
                     for key, (label, _) in STRUCTURES.items()],
            value="fly", clearable=False, style={**CONTROL_STYLE, "width": "150px"},
        )], style={"color": "#e2e8f0"}),
        html.Label(["Contract", dcc.Dropdown(
            id="contract", options=[], value=None, clearable=False,
            style={**CONTROL_STYLE, "width": "180px"},
        )], style={"color": "#e2e8f0"}),
        html.Label(["Timeframe", dcc.Dropdown(
            id="timeframe",
            options=[{"label": f"{minutes} minutes", "value": minutes}
                     for minutes in (15, 30, 60, 240)],
            value=60, clearable=False, style={**CONTROL_STYLE, "width": "150px"},
        )], style={"color": "#e2e8f0"}),
    ], style={"display": "flex", "gap": "16px", "alignItems": "end",
              "flexWrap": "wrap"}),
    html.Div(id="status", style={"color": "#fca5a5", "minHeight": "24px",
                                 "marginTop": "18px"}),
    dcc.Graph(id="bounds-chart"),
    dcc.Interval(id="live-refresh", interval=10_000, n_intervals=0),
], style={"minHeight": "100vh", "padding": "28px", "backgroundColor": "#0b1120",
          "color": "#f8fafc", "fontFamily": "Segoe UI, Arial, sans-serif"})


@app.callback(
    Output("contract", "options"),
    Output("contract", "value"),
    Input("product", "value"),
    Input("structure", "value"),
)
def update_contracts(product, structure):
    contracts = contracts_for(product, structure)
    return [{"label": contract, "value": contract} for contract in contracts], (
        contracts[0] if contracts else None
    )


@app.callback(
    Output("bounds-chart", "figure"),
    Output("status", "children"),
    Input("product", "value"),
    Input("structure", "value"),
    Input("contract", "value"),
    Input("timeframe", "value"),
    Input("live-refresh", "n_intervals"),
    State("bounds-chart", "figure"),
)
def update_chart(product, structure, contract, timeframe, _refresh, previous_figure):
    revision = f"{product}:{structure}:{contract}:{timeframe}"
    previous_figure = previous_figure or {}
    layout = previous_figure.get("layout", {})
    traces = previous_figure.get("data", [])
    live_index = next((index for index, trace in enumerate(traces)
                       if trace.get("uid") == "liveprice"), None)
    if (set(ctx.triggered_prop_ids) == {"live-refresh.n_intervals"}
            and layout.get("uirevision") == revision and live_index is not None):
        # Timer updates never resend candles, axes, or other layout settings.
        client = LiveMarketDataClient(timeout=2)
        try:
            quote = client.expression(product, layout["meta"]["expression"])
            live_x, live_y = live_coordinates(quote)
            patch = Patch()
            patch["data"][live_index]["x"] = live_x
            patch["data"][live_index]["y"] = live_y
            decimals = PRODUCTS[product]["decimals"]
            message = (f"Live price: {live_y[0]:.{decimals}f}" if live_y
                       else "Weekend live price hidden (UTC).")
            return patch, layout["meta"]["status"] + message
        except (KeyError, TypeError, ValueError, OSError, RuntimeError) as error:
            return no_update, layout.get("meta", {}).get("status", "") + (
                f"Live price unavailable: {error}"
            )
        finally:
            client.close()
    try:
        if not contract:
            return go.Figure(), "Select a contract."
        expression = expression_for(product, structure, contract)
        candles = load_intraday_series(product, expression, int(timeframe))
        candles = candles.dropna(subset=["open", "high", "low", "close"])
        candles = candles.copy()
        candles.index = candles.index.tz_convert(CHART_TIMEZONE)
        candles = candles[candles.index.dayofweek < 5]
        cutoff = candles.index.max() - pd.DateOffset(months=MAX_MONTHS)
        candles = candles.loc[candles.index >= cutoff].tail(MAX_CANDLES)
        if candles.empty:
            raise ValueError("No weekday data is available in the latest four months.")
        live_quote = None
        live_message = "Live price unavailable."
        client = LiveMarketDataClient(timeout=2)
        try:
            live_quote = client.expression(product, expression)
            decimals = PRODUCTS[product]["decimals"]
            _, live_y = live_coordinates(live_quote)
            live_message = (f"Live price: {live_y[0]:.{decimals}f}" if live_y
                            else "Weekend live price hidden (UTC).")
        except (KeyError, TypeError, ValueError, OSError, RuntimeError) as error:
            live_message = f"Live price unavailable: {error}"
        finally:
            client.close()
        figure = make_figure(
            candles, product, structure, contract, int(timeframe),
            live_quote,
        )
        decimals = PRODUCTS[product]["decimals"]
        status = (
            f"Loaded {len(candles):,} weekday candles, latest {MAX_MONTHS} months, "
            f"maximum {MAX_CANDLES:,}. Prices shown to {decimals} decimals. "
        )
        figure.update_layout(meta={"expression": expression, "status": status})
        return figure, status + live_message
    except (KeyError, TypeError, ValueError, OSError) as error:
        return go.Figure(), f"Could not load prices: {error}"


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8061)
    args = parser.parse_args()
    app.run(host="127.0.0.1", port=args.port, debug=False)

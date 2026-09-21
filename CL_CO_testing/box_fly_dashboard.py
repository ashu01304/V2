"""Minute structures for individual products and the CL-CO difference."""

import sys
import re
import json
import hashlib
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd
import plotly.graph_objects as go
from dash import Dash, Input, Output, State, ctx, dcc, html, no_update

from analysis.contract_universe import (build_universe, generate_universe, load_expiries)
from market_data.chart_updates import preserve_live_charts
from market_data import MarketData
from market_data.config import DATABASE_PATH
from market_data.live_client import LiveMarketDataClient


# Include every month within the configured horizon, including deferred months
# omitted by the shared testing universe. Data availability is checked per chart.
UNIVERSE, universe_config, as_of = generate_universe()
expiries = load_expiries(universe_config.symbol)
anchors = [code for code, expiry in expiries
           if (expiry >= as_of if universe_config.keep_expiry_day else expiry > as_of)
           and expiry.year <= as_of.year + universe_config.years_ahead]
UNIVERSE = build_universe(expiries, anchors, universe_config.custom_structures)
CONTRACTS = list(UNIVERSE["contracts"])
STRUCTURES = UNIVERSE["columns"]


def available_symbols():
    database = MarketData()
    try:
        products = set()
        for instrument in database.spreads():
            match = re.fullmatch(
                r"([A-Z]+)([FGHJKMNQUVXZ]\d{1,2})-([FGHJKMNQUVXZ]\d{1,2})",
                instrument)
            if match:
                products.add(match[1])
    finally:
        database.close()
    aliases = {"LCO": "CO", "LGO": "G", "DBIP": "DBI"}
    mapping = {aliases.get(product, product): (product,)
               for product in sorted(products)}
    mapping.update({"CL": ("CL",), "CO": ("LCO",), "CL-CO": ("CL", "LCO")})
    return mapping


SYMBOL_PRODUCTS = available_symbols()
SYMBOLS = ["CL-CO", "CL", "CO"] + sorted(set(SYMBOL_PRODUCTS) - {"CL-CO", "CL", "CO"})
app = Dash(__name__)
app.title = "Structures Minute Data"
app.layout = html.Div([
    html.H2("Structures | Change From Open"),
    html.Div([
        html.Label(["Symbol", dcc.Dropdown(
            id="symbol", options=SYMBOLS, value="CL-CO", clearable=False,
            style={"width": "180px", "color": "black"})]),
        html.Label(["Contracts", dcc.Dropdown(
            id="contract", options=CONTRACTS,
            value=CONTRACTS[:5], multi=True,
            style={"width": "600px", "maxWidth": "80vw", "color": "black"})]),
        html.Label(["Structures", dcc.Dropdown(
            id="structure", options=STRUCTURES, value=["1MF"], multi=True,
            style={"width": "300px", "color": "black"})]),
        dcc.Checklist(id="zero-base", options=[{"label": " Zero base", "value": "yes"}],
                      value=["yes"], persistence=True, persistence_type="local"),
        html.Label(["Past N days", dcc.Input(
            id="past-days", type="number", value=0, min=0, step=1,
            debounce=False, persistence=True, persistence_type="local",
            style={"display": "block", "width": "100px", "color": "black"})]),
        html.Button("Refresh", id="refresh", n_clicks=0),
    ], style={"display": "flex", "gap": "20px", "alignItems": "end", "flexWrap": "wrap"}),
    html.Div(id="data-updated", style={"paddingTop": "15px", "color": "#94a3b8"},
             title="Last modification time of the local master database file."),
    html.Div(id="live-data-updated", style={"paddingTop": "6px", "color": "#94a3b8"},
             title="Latest received quote for the selected symbol from the live service on port 8060; charts use stored minute data."),
    html.Div(id="status", style={"padding": "15px 0"}),
    dcc.Graph(id="chart"),
    dcc.Graph(id="continuous-chart"),
    dcc.Interval(id="live-refresh", interval=10_000, n_intervals=0),
], style={"background": "#0f172a", "color": "white", "minHeight": "100vh",
          "padding": "20px"})


@app.callback(Output("data-updated", "children"),
              Input("live-refresh", "n_intervals"),
              Input("refresh", "n_clicks"))
def show_database_updated(_, __):
    try:
        updated = pd.Timestamp(DATABASE_PATH.stat().st_mtime, unit="s", tz="UTC")
    except OSError:
        return "Database last updated: unavailable"
    minutes = max(0, int((pd.Timestamp.now(tz="UTC") - updated).total_seconds() // 60))
    age = "less than a minute ago" if minutes == 0 else f"{minutes:,} minutes ago"
    return f"Database last updated: {updated:%Y-%m-%d %H:%M:%S} UTC ({age})"


@app.callback(Output("live-data-updated", "children"),
              Input("live-refresh", "n_intervals"),
              Input("refresh", "n_clicks"),
              Input("symbol", "value"))
def show_live_data_updated(_, __, symbol):
    products = ["CL", "CO"] if symbol == "CL-CO" else [symbol]
    if symbol not in SYMBOL_PRODUCTS:
        return "Live feed last received: select a symbol"
    client = LiveMarketDataClient(timeout=2)
    try:
        health = client.health()
        rows = client.snapshot(products=products)
        timestamps = pd.to_datetime(
            [row.get("received_at") for row in rows], utc=True, errors="coerce")
        timestamps = timestamps.dropna()
        state = health.get("status", "UNKNOWN")
        if timestamps.empty:
            return f"Live feed last received ({symbol}): no quotes received | {state}"
        latest = timestamps.max()
        seconds = max(0, int((pd.Timestamp.now(tz="UTC") - latest).total_seconds()))
        return (f"Live feed last received ({symbol}): {latest:%Y-%m-%d %H:%M:%S} UTC "
                f"({seconds:,} seconds ago) | {state}")
    except Exception:
        return "Live feed last received: unavailable (cannot read live service on port 8060)"
    finally:
        client.close()


@app.callback(Output("chart", "figure"), Output("continuous-chart", "figure"),
              Output("status", "children"),
              Input("symbol", "value"),
              Input("contract", "value"),
              Input("structure", "value"),
              Input("zero-base", "value"),
              Input("past-days", "value"),
              Input("refresh", "n_clicks"),
              Input("live-refresh", "n_intervals"),
              State("chart", "figure"), State("continuous-chart", "figure"), State('chart', "figure"), State('continuous-chart', "figure"),
              running=[(Output("live-refresh", "disabled"), True, False)])
@preserve_live_charts([0, 1])
def update_chart(symbol, contracts, structures, zero_base, past_days, _, __,
                 chart_state=None, continuous_state=None):
    revision = json.dumps([symbol, contracts, structures, bool(zero_base), past_days])
    # Preserve the existing charts on transient refresh errors only when
    # they already represent the requested parameters.
    live_refresh = (
        set(ctx.triggered_prop_ids) == {"live-refresh.n_intervals"}
        and (chart_state or {}).get("layout", {}).get("uirevision") == "daily-overlay:" + revision
        and (continuous_state or {}).get("layout", {}).get("uirevision") == "continuous:" + revision
    )
    figure = go.Figure()
    continuous = go.Figure()
    figure.update_layout(template="plotly_dark", height=750,
                         paper_bgcolor="#0f172a", plot_bgcolor="#0f172a",
                         uirevision="daily-overlay:" + revision,
                         hovermode="x unified", xaxis_title="Session time (Chicago; opens 17:00)",
                         xaxis=dict(range=[0, 1380], tickmode="array",
                                    tickvals=list(range(0, 1380, 120)) + [1380],
                                    ticktext=[f"{(17 + hour) % 24:02d}:00"
                                              for hour in range(0, 23, 2)] + ["16:00"]),
                         yaxis_title="Change from open", yaxis_tickformat=".4f")
    continuous.update_layout(
        template="plotly_dark", height=750, paper_bgcolor="#0f172a",
        plot_bgcolor="#0f172a", hovermode="x unified", uirevision="continuous:" + revision,
        xaxis_title="Time (Chicago)", xaxis_tickformat="%d %b<br>%H:%M",
        yaxis_tickformat=".4f")
    try:
        if symbol not in SYMBOL_PRODUCTS:
            raise ValueError("Select a valid symbol")
        products = SYMBOL_PRODUCTS[symbol]
        if not contracts:
            raise ValueError("Select at least one contract")
        if not structures:
            raise ValueError("Select at least one structure")
        if any(contract not in UNIVERSE["contracts"] for contract in contracts):
            raise ValueError("Unknown contract selected; reload the page")
        if any(structure not in STRUCTURES for structure in structures):
            raise ValueError("Unknown structure selected; reload the page")
        selections = {
            f"{contract} | {structure}": UNIVERSE["contracts"][contract][structure]
            for contract in contracts
            for structure in structures
        }
        if past_days is None or past_days < 0 or not float(past_days).is_integer():
            raise ValueError("Past N days must be a non-negative whole number")
        past_days = int(past_days)
        end = pd.Timestamp.now(tz="UTC")
        local_now = end.tz_convert("America/Chicago").tz_localize(None)
        session_open = (local_now - pd.Timedelta(hours=17)).normalize() + pd.Timedelta(hours=17)
        period_start = session_open - pd.Timedelta(days=past_days)
        start = period_start.tz_localize("America/Chicago").tz_convert("UTC")
        session_close = (session_open + pd.Timedelta(hours=23)).tz_localize(
            "America/Chicago").tz_convert("UTC")
        session_date = (session_open + pd.Timedelta(days=1)).date()
        if session_open.dayofweek in (4, 5) and past_days == 0:
            raise ValueError("Market closed: no current trading session")
        database = MarketData()
        missing, points = [], 0
        try:
            for contract, expression in selections.items():
                try:
                    series = {}
                    for product in products:
                        frame = database.synthetic(product, expression, start=start,
                                                   end=min(end, session_close))
                        frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
                        series[product] = frame.set_index("timestamp")["price"]
                    aligned = pd.concat(series, axis=1, join="inner").dropna().sort_index()
                    aligned = aligned[aligned.index < session_close]
                    if aligned.empty:
                        raise ValueError(f"No aligned {symbol} minute data")
                except ValueError as error:
                    missing.append(f"{contract}: {error}")
                    continue
                prices = (aligned[products[0]] - aligned[products[1]]
                          if len(products) == 2 else aligned[products[0]])
                local = prices.index.tz_convert("America/Chicago").tz_localize(None)
                period_opening = prices.iloc[0]
                continuous.add_trace(go.Scatter(
                    x=[timestamp.isoformat() for timestamp in local],
                    y=((prices - period_opening) if zero_base else prices).round(4).tolist(),
                    mode="lines+markers", name=f"{contract} | {expression}",
                    customdata=list(zip(local.strftime("%Y-%m-%d %H:%M"),
                                        [period_opening] * len(prices))),
                    line={"width": 1.5}, marker={"size": 6},
                    hovertemplate="%{customdata[0]} CT<br>Value: %{y:+.4f}<br>Start: %{customdata[1]:.4f}<extra>%{fullData.name}</extra>"))
                sessions = pd.DataFrame({
                    "price": prices,
                    "time": local,
                    "minute": (local.hour * 60 + local.minute - 17 * 60) % 1440,
                    "day": (local - pd.Timedelta(hours=17) + pd.Timedelta(days=1)).date,
                })
                sessions = sessions[sessions["minute"] < 1380]
                for day, values in sessions.groupby("day"):
                    opening = values["price"].iloc[0]
                    figure.add_trace(go.Scatter(
                        x=values["minute"].tolist(),
                        y=((values["price"] - opening) if zero_base
                           else values["price"]).round(4).tolist(),
                        mode="lines+markers", name=f"{contract} | {day}",
                        customdata=list(zip(values["time"].dt.strftime("%Y-%m-%d %H:%M"),
                                            [opening] * len(values))),
                        line={"width": 1.5,
                              **({"color": "white"} if day == session_date else {})},
                        marker={"size": 6,
                                **({"color": "white"} if day == session_date else {})},
                        hovertemplate="%{customdata[0]} CT<br>Value: %{y:+.4f}<br>Open: %{customdata[1]:.4f}<extra>%{fullData.name}</extra>"))
                    if day == session_date:
                        latest = values.iloc[-1]
                        figure.add_trace(go.Scatter(
                            x=[latest["minute"]],
                            y=[round(latest["price"] - opening, 4)
                               if zero_base else round(latest["price"], 4)],
                            mode="markers", name=f"{contract} live", showlegend=False,
                            marker={"size": 14, "color": "#22c55e",
                                    "line": {"color": "white", "width": 2}},
                            customdata=[[latest["time"].strftime("%Y-%m-%d %H:%M"),
                                         latest["price"]]],
                            hovertemplate="%{customdata[0]} CT<br>Live price: %{customdata[1]:.4f}<extra>%{fullData.name}</extra>"))
                        continuous.add_trace(go.Scatter(
                            x=[latest["time"].isoformat()],
                            y=[round(latest["price"] - period_opening, 4)
                               if zero_base else round(latest["price"], 4)],
                            mode="markers", name=f"{contract} live", showlegend=False,
                            marker={"size": 14, "color": "#22c55e",
                                    "line": {"color": "white", "width": 2}},
                            customdata=[[latest["time"].strftime("%Y-%m-%d %H:%M"),
                                         latest["price"]]],
                            hovertemplate="%{customdata[0]} CT<br>Live price: %{customdata[1]:.4f}<extra>%{fullData.name}</extra>"))
                    points += len(values)
        finally:
            database.close()
        if zero_base:
            figure.add_hline(y=0, line_width=1, line_color="#94a3b8")
            continuous.add_hline(y=0, line_width=1, line_color="#94a3b8")
        figure.update_layout(
            title=(f"{'Change From Open' if zero_base else 'Structure Level'} | {symbol} | "
                   f"Daily Overlay"),
            yaxis_title="Change from open" if zero_base else f"{symbol} structure")
        continuous.update_layout(
            title=(f"{'Change From Period Start' if zero_base else 'Structure Level'} | "
                   f"{symbol} | Continuous"),
            yaxis_title="Change from period start" if zero_base else f"{symbol} structure")
        status = ""
        if not points:
            status = f"No {symbol} minute data for the selected period."
        if missing:
            status += (" | " if status else "") + "Unavailable: " + "; ".join(missing)
        for label, chart in (("daily", figure), ("continuous", continuous)):
            for trace in chart.data:
                # Plotly uses UIDs in CSS selectors; keep them alphanumeric.
                identity = json.dumps([symbol, label, trace.name])
                trace.uid = "trace" + hashlib.sha256(identity.encode("utf-8")).hexdigest()
        # Send a complete trace collection so removed contracts disappear.
        # Stable uirevision and trace UIDs preserve the view on timer updates.
        return figure, continuous, status
    except Exception as error:
        if live_refresh:
            return no_update, no_update, str(error)
        return figure, continuous, str(error)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8055, debug=False)

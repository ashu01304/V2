"""Dashboard for the unified market-data service."""

import pandas as pd
import plotly.graph_objects as go
from dash import Dash, Input, Output, Patch, State, ctx, dash_table, dcc, html, no_update

from market_data.live_client import LiveMarketDataClient


API_URL = "http://127.0.0.1:8060"
COLORS = {"background": "#020617", "panel": "#0f172a", "line": "#38bdf8"}
MONTH_ORDER = {month: index for index, month in enumerate("FGHJKMNQUVXZ")}


def contract_key(code):
    try:
        return int(code[1:]), MONTH_ORDER[code[0]]
    except (ValueError, KeyError, IndexError):
        return 999, 999


def figure(title, y_title, x_title="UTC time"):
    result = go.Figure()
    result.update_layout(
        title=title, template="plotly_dark", paper_bgcolor=COLORS["panel"],
        plot_bgcolor=COLORS["panel"], height=430, hovermode="x unified",
        uirevision=title, margin={"l": 55, "r": 30, "t": 55, "b": 45},
        xaxis_title=x_title, yaxis_title=y_title)
    return result


def with_gaps(frame, time_column, value_column, gap_minutes=15):
    """Insert empty points so Plotly does not bridge missing market data."""
    if frame.empty or time_column not in frame or value_column not in frame:
        return [], []
    x, y, previous = [], [], None
    for timestamp, value in frame[[time_column, value_column]].itertuples(index=False):
        if previous is not None and timestamp - previous > pd.Timedelta(minutes=gap_minutes):
            x.append(timestamp - pd.Timedelta(microseconds=1))
            y.append(None)
        x.append(timestamp)
        y.append(value)
        previous = timestamp
    return x, y


app = Dash(__name__)
app.title = "Market Data"
panel = {"background": COLORS["panel"], "border": "1px solid #334155",
         "borderRadius": "8px", "padding": "14px"}
control = {"color": "black", "width": "180px"}

app.layout = html.Div([
    html.H2("Market Data"),
    html.Div(id="health", style={**panel, "marginBottom": "12px"}),
    html.Div([
        html.Label(["Product", dcc.Dropdown(
            id="product", options=["CL", "CO", "NG", "DBI", "FCPO", "G",
                                   "GC", "HG", "HO", "RB", "SI"],
            value="CL", clearable=False, persistence=True,
            persistence_type="local", style=control)]),
        html.Label(["Outright contract", dcc.Dropdown(
            id="contract", clearable=False, persistence=True,
            persistence_type="local", style=control)]),
        html.Label(["Expression", dcc.Input(
            id="expression", value="X26-Z26", debounce=True,
            persistence=True, persistence_type="local",
            style={**control, "height": "36px"})]),
        html.Label(["History hours", dcc.Input(
            id="hours", type="number", value=24, min=1, step=1, debounce=True,
            persistence=True, persistence_type="local",
            style={**control, "height": "36px"})]),
        html.Button("Refresh", id="refresh", n_clicks=0,
                    style={"height": "38px"}),
    ], style={**panel, "display": "flex", "gap": "18px", "alignItems": "end",
              "flexWrap": "wrap", "marginBottom": "12px"}),
    html.Div([
        dcc.Graph(id="forward-curve", style={"flex": "1", "minWidth": "480px"}),
        html.Div([
            html.H4("Live outright snapshot"),
            dash_table.DataTable(
                id="snapshot", page_size=12, sort_action="native",
                style_table={"overflowX": "auto"},
                style_header={"backgroundColor": "#1e293b", "color": "white"},
                style_cell={"backgroundColor": COLORS["panel"], "color": "white",
                            "padding": "6px", "textAlign": "right",
                            "fontFamily": "monospace", "fontSize": "12px"}),
        ], style={**panel, "flex": "1", "minWidth": "560px"}),
    ], style={"display": "flex", "gap": "12px", "flexWrap": "wrap"}),
    html.Div(id="history-status", style={**panel, "marginTop": "12px"}),
    dcc.Graph(id="cache-history"),
    dcc.Graph(id="expression-history"),
    dcc.Graph(id="settlement-history"),
    dcc.Interval(id="timer", interval=10_000, n_intervals=0),
    dcc.Interval(id="history-timer", interval=60_000, n_intervals=0),
], style={"background": COLORS["background"], "color": "white",
          "minHeight": "100vh", "padding": "16px"})


@app.callback(
    Output("health", "children"), Output("contract", "options"),
    Output("contract", "value"), Output("snapshot", "data"),
    Output("snapshot", "columns"), Output("forward-curve", "figure"),
    Input("history-timer", "n_intervals"), Input("refresh", "n_clicks"),
    Input("product", "value"), State("contract", "value"))
def update_live(_, __, product, selected):
    client = LiveMarketDataClient(API_URL)
    try:
        health = client.health()
        rows = client.snapshot([product])
    except Exception as error:
        return (f"Market-data service unavailable: {error}", no_update, no_update,
                [], [], figure("Live forward curve", "Price", "Contract"))
    finally:
        client.close()

    rows.sort(key=lambda row: contract_key(row.get("contract", "")))
    contracts = [row["contract"] for row in rows]
    selected = selected if selected in contracts else (contracts[0] if contracts else None)
    display_columns = ["contract", "admin", "last", "mid", "bid", "ask", "received_at"]
    table_rows = [{column: (round(row.get(column), 4)
                            if column in ("admin", "last", "mid", "bid", "ask")
                            and row.get(column) is not None else row.get(column))
                   for column in display_columns} for row in rows]
    columns = [{"name": column.replace("_", " ").title(), "id": column}
               for column in display_columns]

    curve = figure(f"{product} live outright curve", "Price", "Contract")
    curve.add_trace(go.Scatter(
        x=contracts, y=[row.get("value") for row in rows], mode="lines+markers",
        marker={"size": 9, "color": COLORS["line"]}, line={"color": COLORS["line"]},
        customdata=[[row.get("admin"), row.get("last"), row.get("bid"), row.get("ask")]
                    for row in rows],
        hovertemplate=("%{x}<br>Value: %{y:.4f}<br>Admin: %{customdata[0]}"
                       "<br>Last: %{customdata[1]}<br>Bid: %{customdata[2]}"
                       "<br>Ask: %{customdata[3]}<extra></extra>")))
    status = [
        html.B(str(health.get("status"))),
        html.Span(f" | instruments: {health.get('received_instruments', 0)}/"
                  f"{health.get('subscribed_instruments', 0)}"),
        html.Span(f" | latest live age: "
                  f"{float(health.get('latest_update_age_seconds') or 0):.1f} seconds"),
        html.Span(f" | last spread update: {health.get('last_spread_update') or 'running/waiting'}"),
        html.Span(f" | last SEAC update: {health.get('last_seac_update') or 'running/waiting'}"),
    ]
    if health.get("sync_error"):
        status.append(html.Span(" | sync error: " + health["sync_error"],
                                style={"color": "#f87171"}))
    return status, contracts, selected, table_rows, columns, curve


@app.callback(
    Output("cache-history", "figure"), Output("expression-history", "figure"),
    Output("settlement-history", "figure"),
    Output("history-status", "children"),
    Input("timer", "n_intervals"), Input("refresh", "n_clicks"),
    Input("product", "value"), Input("contract", "value"),
    Input("expression", "value"), Input("hours", "value"))
def update_history(_, __, product, contract, expression, hours):
    cache_plot = figure("Three-hour live outright cache", "Price")
    merged_plot = figure("Merged expression history", "Expression value")
    settlement_plot = figure("SEAC daily settlement history", "Settlement")
    if not contract or not expression:
        return (cache_plot, merged_plot, settlement_plot,
                "Select a contract and enter an expression.")
    try:
        hours = max(1, int(hours or 24))
    except (TypeError, ValueError):
        return (cache_plot, merged_plot, settlement_plot,
                "History hours must be a whole number.")
    now = pd.Timestamp.now(tz="UTC")
    rolling_start = now - pd.Timedelta(hours=3)
    start = (now - pd.Timedelta(hours=hours)).isoformat()
    client, messages = LiveMarketDataClient(API_URL), []

    if ctx.triggered_id == "history-timer":
        cache_patch, merged_patch = Patch(), Patch()
        try:
            cached = pd.DataFrame(client.history(
                product, contract, start=rolling_start.isoformat()))
            if not cached.empty:
                cached["timestamp"] = pd.to_datetime(cached["timestamp"], utc=True)
            x, y = with_gaps(cached, "timestamp", "price", gap_minutes=2)
            cache_patch["data"][0]["x"], cache_patch["data"][0]["y"] = x, y
            messages.append(f"cache points: {len(cached):,}")

            recent = pd.DataFrame(client.expression_history(
                product, expression, start=rolling_start.isoformat()))
            if not recent.empty:
                recent["timestamp"] = pd.to_datetime(recent["timestamp"], utc=True)
            x, y = with_gaps(recent, "timestamp", "price")
            merged_patch["data"][1]["x"], merged_patch["data"][1]["y"] = x, y
            messages.append(f"latest three-hour points: {len(recent):,}")
            latest = client.expression(product, expression)
            merged_patch["data"][2]["x"] = [pd.to_datetime(
                latest["timestamp"], utc=True)]
            merged_patch["data"][2]["y"] = [latest["value"]]
            messages.append(f"live expression: {float(latest['value']):.4f}")
            return (cache_patch, merged_patch, no_update,
                    "Automatic update: latest three hours only | " + " | ".join(messages))
        except Exception as error:
            return no_update, no_update, no_update, f"Recent update error: {error}"
        finally:
            client.close()

    try:
        try:
            cached = pd.DataFrame(client.history(product, contract, start=start))
            if not cached.empty:
                cached["timestamp"] = pd.to_datetime(cached["timestamp"], utc=True)
            x, y = with_gaps(cached, "timestamp", "price", gap_minutes=2)
            cache_plot.add_trace(go.Scatter(
                x=x, y=y, mode="lines+markers", connectgaps=False,
                name=f"{product} {contract}", marker={"size": 6},
                line={"color": "#a78bfa"}))
            messages.append(f"cache points: {len(cached):,}")
        except Exception as error:
            cache_plot.add_trace(go.Scatter(
                x=[], y=[], mode="lines+markers", name=f"{product} {contract}",
                line={"color": "#a78bfa"}))
            messages.append(f"cache error: {error}")

        try:
            merged = pd.DataFrame(client.expression_history(
                product, expression, start=start))
            if not merged.empty:
                merged["timestamp"] = pd.to_datetime(merged["timestamp"], utc=True)
            older = merged[merged["timestamp"] < rolling_start] if not merged.empty else merged
            recent = merged[merged["timestamp"] >= rolling_start] if not merged.empty else merged
            for values, name, legend in (
                    (older, f"{product} {expression}", True),
                    (recent, "Latest three hours", False)):
                x, y = with_gaps(values, "timestamp", "price")
                merged_plot.add_trace(go.Scattergl(
                    x=x, y=y, mode="lines" if legend else "lines+markers",
                    connectgaps=False,
                    name=name, showlegend=legend, marker={"size": 5},
                    line={"color": "#38bdf8"}))
            messages.append(f"merged points: {len(merged):,}")
            try:
                latest = client.expression(product, expression)
                merged_plot.add_trace(go.Scatter(
                    x=[pd.to_datetime(latest["timestamp"], utc=True)],
                    y=[latest["value"]], mode="markers", name="Live latest",
                    marker={"size": 14, "color": "#22c55e",
                            "line": {"width": 2, "color": "white"}}))
                messages.append(f"live expression: {float(latest['value']):.4f}")
            except Exception as error:
                merged_plot.add_trace(go.Scatter(
                    x=[], y=[], mode="markers", name="Live latest",
                    marker={"size": 14, "color": "#22c55e",
                            "line": {"width": 2, "color": "white"}}))
                messages.append(f"latest error: {error}")
        except Exception as error:
            for name, legend in ((f"{product} {expression}", True),
                                 ("Latest three hours", False)):
                merged_plot.add_trace(go.Scattergl(
                    x=[], y=[], mode="lines+markers", name=name,
                    showlegend=legend, line={"color": "#38bdf8"}))
            merged_plot.add_trace(go.Scatter(x=[], y=[], mode="markers",
                                             name="Live latest"))
            messages.append(f"merged history error: {error}")
        try:
            settlements = pd.DataFrame(client.settlement_history(product, contract))
            if not settlements.empty:
                settlements["trading_date"] = pd.to_datetime(
                    settlements["trading_date"])
                settlement_plot.add_trace(go.Scatter(
                    x=settlements["trading_date"], y=settlements["price"],
                    mode="lines+markers", name=f"{product} {contract}",
                    marker={"size": 6}, line={"color": "#f59e0b"}))
            messages.append(f"SEAC points: {len(settlements):,}")
        except Exception as error:
            messages.append(f"SEAC error: {error}")
    finally:
        client.close()
    return cache_plot, merged_plot, settlement_plot, " | ".join(messages)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8062, debug=False)

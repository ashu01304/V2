"""Independent live-price curves for outrights, calendar spreads and butterflies."""

import argparse
from datetime import date
from functools import lru_cache
from math import isfinite

import duckdb
import numpy as np
import pandas as pd
import plotly.graph_objects as go
from scipy.interpolate import make_smoothing_spline
from dash import Dash, Input, Output, dash_table, dcc, html, set_props
from requests import RequestException

from analysis.contract_universe import load_expiries
from analysis.expression import format_contract_label
from market_data import LiveMarketDataClient, MarketData
from market_data.queries import database_signature
from market_data.config import LIVE_PRODUCTS

MONTH_CODES = "FGHJKMNQUVXZ"
LIVE_COLOR = "#dbeafe"
PRICE_DECIMALS = {"HO": 4, "NG": 3, "RB": 4}
TIMEFRAMES = {15: "15 min", 30: "30 min", 60: "1 hour", 120: "2 hours"}
FIT_OPTIONS = {"linear": "Linear", "quadratic": "Quadratic", "cubic": "Cubic", "spline": "Smooth spline"}
PRODUCTS = ("CL-CO", *LIVE_PRODUCTS)
STRUCTURES = {"outright": "Outright / single month", "spread": "Calendar spread", "fly": "Butterfly (1:-2:1)"}


def consecutive_contracts(months, today=None, skip_months=0, *, product="CL-CO"):
    """Skip N calendar months from the first unexpired pair; keep month gaps."""
    today = today or date.today()
    calendars = [dict(load_expiries(symbol)) for symbol in (("CL", "CO") if product == "CL-CO" else (product,))]
    common = set.intersection(*(set(calendar) for calendar in calendars))
    active = [code for code in common if min(calendar[code] for calendar in calendars) >= today]
    if not active:
        return []
    first = min(active, key=lambda code: (int(code[1:]), MONTH_CODES.index(code[0])))
    start = pd.Timestamp(2000 + int(first[1:]), MONTH_CODES.index(first[0]) + 1, 1)
    start += pd.DateOffset(months=skip_months)
    return [f"{MONTH_CODES[month.month - 1]}{month.year % 100:02d}"
            for month in pd.date_range(start, periods=months + 1, freq="MS")]


def live_prices(now=None, *, product="CL-CO"):
    """Use the live server's reference value (admin, then last, then midpoint)."""
    def number(value):
        try:
            value = float(value)
            return value if isfinite(value) and value != -100 else None
        except (TypeError, ValueError):
            return None

    now = pd.Timestamp.now(tz="UTC") if now is None else pd.Timestamp(now)
    client = LiveMarketDataClient(timeout=2)
    try:
        health = client.health()
        if not health.get("status", "").startswith("CONNECTED:"):
            return {}, "Live feed disconnected"
        rows = client.snapshot(["CL", "CO"] if product == "CL-CO" else [product])
        legs = {}
        for row in rows:
            try:
                received = pd.to_datetime(row["received_at"], utc=True)
                age = (now - received).total_seconds()
                if pd.isna(received) or not 0 <= age <= 120:
                    continue
                key = (row["product"], row["contract"])
                value = number(row.get("value"))
                if value is not None:
                    legs[key] = value
            except (KeyError, ValueError, TypeError):
                continue
        if product != "CL-CO":
            prices = {code: value for (symbol, code), value in legs.items() if symbol == product}
            return prices, "Live feed connected" if prices else "Waiting for matching live prices"
        prices = {code: value - legs[("CO", code)]
                  for (product, code), value in legs.items()
                  if product == "CL" and ("CO", code) in legs}
        return prices, "Live feed connected" if prices else "Waiting for matching live prices"
    except (RequestException, ValueError, TypeError, KeyError):
        return {}, "Live service unavailable"
    finally:
        client.close()


def structure_legs(contracts, structure="spread", gap=1):
    """Roll each structure forward one month; gap is the spacing between its legs."""
    weights = {"outright": (1,), "spread": (1, -1), "fly": (1, -2, 1)}[structure]
    span = (len(weights) - 1) * gap
    return [[(contracts[start + index * gap], weight) for index, weight in enumerate(weights)]
            for start in range(max(0, len(contracts) - span))]


def structure_expressions(contracts, structure="spread", gap=1):
    return ["".join(("+" if index and weight > 0 else "-" if weight < 0 else "")
                   + (f"{abs(weight)}*" if abs(weight) != 1 else "") + code
                   for index, (code, weight) in enumerate(legs))
            for legs in structure_legs(contracts, structure, gap)]


def spread_values(prices, contracts, structure="spread", gap=1, *, product="CL-CO"):
    values = []
    for legs in structure_legs(contracts, structure, gap):
        try:
            value = sum(weight * float(prices[code]) for code, weight in legs)
            values.append(round(value, PRICE_DECIMALS.get(product, 2)) if isfinite(value) else None)
        except (KeyError, ValueError, TypeError):
            values.append(None)
    return values


@lru_cache(maxsize=16)
def _historical_changes(contracts, minute, signature, product="CL-CO", structure="spread", gap=1):
    expressions = structure_expressions(contracts, structure, gap)
    if structure == "outright":
        # The minute master stores spreads, not outright minute price histories.
        return {age: [None] * len(expressions) for age in TIMEFRAMES}
    targets = [pd.Timestamp(minute) - pd.Timedelta(minutes=age) for age in TIMEFRAMES]
    db = MarketData()
    try:
        snapshots = db.expression_snapshots([(expression, product, expression) for expression in expressions], targets)
    finally:
        db.close()
    return {age: [snapshots.get(target, {}).get(expression) for expression in expressions]
            for age, target in zip(TIMEFRAMES, targets)}


def change_rows(contracts, live, product="CL-CO", structure="spread", gap=1):
    history = {}
    signature = database_signature()
    if structure != "outright" and signature is not None and len(contracts) > 1:
        try:
            history = _historical_changes(tuple(contracts), pd.Timestamp.now(tz="UTC").floor("min").isoformat(),
                                          tuple(signature), product, structure, gap)
        except (duckdb.Error, OSError, RuntimeError, ValueError):
            pass  # Live quotes remain usable while history is unavailable.
    rows = []
    for index, (expression, price) in enumerate(zip(structure_expressions(contracts, structure, gap),
                                                  spread_values(live, contracts, structure, gap, product=product))):
        row = {"Spread": format_contract_label(expression), "Live": price}
        for age in TIMEFRAMES:
            values = history.get(age, [])
            earlier = values[index] if index < len(values) else None
            decimals = PRICE_DECIMALS.get(product, 2)
            row[f"change-{age}"] = (round(price - round(float(earlier), decimals), decimals)
                if price is not None and earlier is not None and isfinite(float(earlier)) else None)
        rows.append(row)
    return rows


def trend_boxes(rows):
    boxes = []
    for age, label in TIMEFRAMES.items():
        values = [row[f"change-{age}"] for row in rows if row[f"change-{age}"] is not None]
        up, down = sum(value > 0 for value in values), sum(value < 0 for value in values)
        unchanged = len(values) - up - down
        trend, color = "Mixed", "#94a3b8"
        if not values:
            trend = "No data"
        elif up > len(values)/2:
            trend, color = "Up", "#4ade80"
        elif down > len(values)/2:
            trend, color = "Down", "#f87171"
        elif unchanged == len(values):
            trend = "Flat"
        count = (f"{up if trend == 'Up' else down} contracts in the same direction" if trend in {"Up", "Down"}
                 else f"{up} up / {down} down / {unchanged} unchanged")
        boxes.append(html.Div([
            html.Div(f"Last {label}"), html.H3(trend, style={"color": color}), html.Div(count),
            html.Small(f"{len(values)}/{len(rows)} structures with data", style={"color": "#94a3b8"}),
        ], style={"background": "#1e293b", "border": f"1px solid {color}", "borderRadius": "8px", "padding": "16px"}))
    return boxes


def make_figure(contracts, live, curves=None, *, product="CL-CO", structure="spread", gap=1):
    labels = [format_contract_label(expression) for expression in structure_expressions(contracts, structure, gap)]
    decimals = PRICE_DECIMALS.get(product, 2)
    values = spread_values(live, contracts, structure, gap, product=product)
    figure = go.Figure(go.Scatter(
        x=list(range(len(labels))), y=values, customdata=labels,
        name="Live", uid="live", mode="lines+markers", connectgaps=False,
        line={"color": LIVE_COLOR, "width": 3}, marker={"size": 7},
        hovertemplate="%{customdata}<br>%{y:." + str(decimals) + "f}<extra>Live</extra>",
    ))
    available = [(index, value) for index, value in enumerate(values) if value is not None]
    for curve in dict.fromkeys(curves or []):
        if curve not in FIT_OPTIONS:
            continue
        required = 5 if curve == "spline" else {"linear": 2, "quadratic": 3, "cubic": 4}[curve]
        if len(available) < required:
            figure.add_annotation(text=f"{FIT_OPTIONS[curve]} needs at least {required} live prices", x=.5,
                                  y=1 - .07*len(figure.layout.annotations), xref="paper", yref="paper", showarrow=False)
            continue
        x, y = np.array(available).T
        grid = np.unique(np.r_[np.linspace(x.min(), x.max(), 300), x])
        if curve == "spline":
            fitted = make_smoothing_spline(x, y, lam=1.)(grid)
        else:
            degree = {"linear": 1, "quadratic": 2, "cubic": 3}[curve]
            fitted = np.polynomial.Polynomial.fit(x, y, degree)(grid)
        figure.add_trace(go.Scatter(x=grid.tolist(), y=np.round(fitted, decimals).tolist(), mode="lines",
            name=FIT_OPTIONS[curve], uid=f"fit-{curve}",
            line={"color": {"linear": "#fbbf24", "quadratic": "#fb923c", "cubic": "#f472b6", "spline": "#4ade80"}[curve]},
            hovertemplate="%{y:." + str(decimals) + "f}<extra>%{fullData.name}</extra>"))
    figure.update_layout(
        template="plotly_dark", paper_bgcolor="#111827", plot_bgcolor="#111827",
        margin={"l": 65, "r": 25, "t": 35, "b": 90}, dragmode="zoom",
        uirevision=":".join([product, structure, str(gap), *contracts]), showlegend=bool(curves), hovermode="closest",
        legend={"orientation": "h", "y": 1.08},
        xaxis={"title": "Consecutive contract months", "type": "linear",
               "tickmode": "array", "tickvals": list(range(len(labels))), "ticktext": labels, "tickangle": -45},
        yaxis={"title": f"{product} {STRUCTURES[structure]} price",
               "tickformat": f".{decimals}f", "zerolinecolor": "#64748b"},
    )
    if not any(value is not None for value in values):
        figure.add_annotation(text="No matching live prices available", x=.5, y=.5,
                              xref="paper", yref="paper", showarrow=False)
    return figure


# Isolate this canvas from the main dashboard's CSS and browser event handlers.
app = Dash(__name__, assets_folder="idea_dashboard_assets", title="Live Price Ideas")
app.layout = html.Div([
    html.Div([
        html.H3("Live price curves", id="idea-heading", style={"margin": 0}),
        html.Label(["Product ", dcc.Dropdown(id="idea-product", options=list(PRODUCTS), value="CL-CO",
            clearable=False, persistence=True, persistence_type="local", style={"width": "140px", "color": "#111827"})]),
        html.Label(["Structure ", dcc.Dropdown(id="idea-structure",
            options=[{"label": label, "value": key} for key, label in STRUCTURES.items()], value="spread",
            clearable=False, persistence=True, persistence_type="local", style={"width": "200px", "color": "#111827"})]),
        html.Label(["Leg spacing (months) ", dcc.Input(id="idea-gap", type="number", value=1,
            min=1, max=24, step=1, debounce=True, persistence=True, persistence_type="local", style={"width": "65px"})]),
        html.Label(["Starting months ", dcc.Input(id="idea-months", type="number", value=24,
            min=1, max=120, step=1, debounce=True, persistence=True,
            persistence_type="local", style={"width": "65px"})]),
        html.Label(["Skip first N months ", dcc.Input(id="idea-skip-months", type="number", value=0,
            min=0, max=120, step=1, debounce=True, persistence=True,
            persistence_type="local", style={"width": "65px"})]),
        html.Label(["Fitted curves ", dcc.Dropdown(id="idea-curves", multi=True,
            options=[{"label": label, "value": key} for key, label in FIT_OPTIONS.items()],
            value=[], placeholder="Select curves", persistence=True, persistence_type="local",
            style={"width": "300px", "color": "#111827"})]),
        html.Button("Refresh", id="idea-refresh", n_clicks=0),
    ], style={"display": "flex", "gap": "24px", "alignItems": "center", "flexWrap": "wrap"}),
    html.P("One live reference-price line. Outrights use the server's reference value; spreads and flies "
           "are calculated from those values (1:-1 and 1:-2:1). CL-CO is calculated from CL minus CO. "
           "Missing or stale prices leave gaps.",
           style={"fontSize": "12px", "color": "#94a3b8"}),
    dcc.Graph(id="idea-chart", config={"scrollZoom": False, "displaylogo": False}, style={"height": "85vh"}),
    html.Div(id="idea-feed-status", style={"fontSize": "12px", "color": "#94a3b8"}),
    html.H3("Live price change over time"),
    html.Div(id="idea-trend-boxes", style={"display": "grid", "gap": "14px", "margin": "16px 0",
        "gridTemplateColumns": "repeat(auto-fit, minmax(180px, 1fr))"}),
    html.P("Change = live minus earlier price. Up/Down requires more than 50% of structures with data. "
           "This section ignores Skip first N months. Outright minute history is not stored; its changes stay blank.",
           style={"fontSize": "12px", "color": "#94a3b8"}),
    html.Details([
        html.Summary("Price-change table (expand / minimise)", style={"cursor": "pointer", "marginBottom": "10px"}),
        dash_table.DataTable(id="idea-changes-table", data=[], columns=[
            {"name": "Structure", "id": "Spread"},
            {"name": "Live price", "id": "Live", "type": "numeric", "format": {"specifier": ".2f"}},
            *[{"name": label, "id": f"change-{age}", "type": "numeric", "format": {"specifier": "+.2f"}}
              for age, label in TIMEFRAMES.items()]],
            sort_action="native", page_size=24, style_table={"overflowX": "auto"},
            style_header={"backgroundColor": "#1e293b", "color": "white", "fontWeight": "bold"},
            style_cell={"backgroundColor": "#111827", "color": "#e2e8f0", "border": "1px solid #334155", "padding": "8px"},
            style_data_conditional=[{"if": {"filter_query": f'{{change-{age}}} {operator} 0', "column_id": f"change-{age}"}, "color": color}
                for age in TIMEFRAMES for operator, color in [(">", "#4ade80"), ("<", "#f87171")]]),
    ], open=True),
    dcc.Interval(id="idea-tick", interval=10_000),
], style={"background": "#111827", "color": "white", "minHeight": "100vh",
          "padding": "20px", "boxSizing": "border-box", "fontFamily": "Arial, sans-serif"})


@app.callback(Output("idea-chart", "figure"), Input("idea-months", "value"),
              Input("idea-skip-months", "value"), Input("idea-refresh", "n_clicks"),
              Input("idea-tick", "n_intervals"), Input("idea-curves", "value"),
              Input("idea-product", "value"), Input("idea-structure", "value"), Input("idea-gap", "value"))
def update_chart(months, skip_months, _refresh, _tick, curves, product="CL-CO", structure="spread", gap=1):
    months = max(1, min(120, int(24 if months is None else months)))
    skip_months = max(0, min(120, int(skip_months or 0)))
    product = product if product in PRODUCTS else "CL-CO"
    structure = structure if structure in STRUCTURES else "spread"
    gap = max(1, min(24, int(gap or 1)))
    if structure == "outright":
        gap = 1
    span = gap * {"outright": 0, "spread": 1, "fly": 2}[structure]
    set_props("idea-gap", {"disabled": structure == "outright"})
    try:
        all_contracts = consecutive_contracts(months + skip_months + span - 1, product=product)
    except (ValueError, OSError) as error:
        set_props("idea-feed-status", {"children": str(error)})
        set_props("idea-changes-table", {"data": []})
        set_props("idea-trend-boxes", {"children": trend_boxes([])})
        return make_figure([], {}, curves, product=product, structure=structure, gap=gap)
    contracts = all_contracts[skip_months:]
    live, status = live_prices(product=product)
    rows = change_rows(all_contracts[:months + span], live, product, structure, gap)
    title = f"{product} | {STRUCTURES[structure]}"
    if structure != "outright":
        title += f" | {gap}-month spacing"
    set_props("idea-heading", {"children": title})
    set_props("idea-feed-status", {"children": status})
    decimals = PRICE_DECIMALS.get(product, 2)
    columns = [{"name": "Structure", "id": "Spread"},
               {"name": "Live price", "id": "Live", "type": "numeric", "format": {"specifier": f".{decimals}f"}},
               *[{"name": label, "id": f"change-{age}", "type": "numeric", "format": {"specifier": f"+.{decimals}f"}}
                 for age, label in TIMEFRAMES.items()]]
    set_props("idea-changes-table", {"data": rows, "columns": columns})
    set_props("idea-trend-boxes", {"children": trend_boxes(rows)})
    return make_figure(contracts, live, curves, product=product, structure=structure, gap=gap)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8055)
    args = parser.parse_args()
    app.run(host="127.0.0.1", port=args.port, debug=False)

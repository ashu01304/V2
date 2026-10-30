"""SEAC expression builder; reuse analysis engines without modifying them."""

from functools import lru_cache
from math import isfinite
from uuid import uuid4

import duckdb
import numpy as np
import pandas as pd
import plotly.graph_objects as go
from dash import ALL, Input, Output, State, ctx, dcc, html, no_update
from dash.exceptions import PreventUpdate

from analysis.contract_universe import load_expiries
from analysis.expression import format_contract_label, parse_expression
from analysis.expiry import OfficialExpiryLookup
from analysis.plotting import SeasonalityPlotter
from analysis.seasonality import Seasonality
from market_data import MarketData
from market_data.config import LIVE_PRODUCTS
from market_data.queries import database_signature
from market_data.workflow_client import fetch_live_quote as shared_live_quote

def fetch_live_quote(product, expression):
    return shared_live_quote(product, expression, scope="seac-builder")

MONTHS = "FGHJKMNQUVXZ"
PRODUCTS = (*LIVE_PRODUCTS, "CL-CO")
PRESETS = {"spread": (1, -1), "fly": (1, -2, 1), "defly": (1, -3, 3, -1), "outright": (1,)}
STRUCTURE_NAMES = {"outright": "Outright", "spread": "Spread", "fly": "Fly", "defly": "Defly"}


def selection(value):
    return value if isinstance(value, dict) else {"count": value, "structure": "outright"}


def month_cells(row):
    return [html.Div([
                        html.Button(code, id={"type": "seac-month-clear", "code": code}, n_clicks=0,
                                    className="seac-month-label", title=f"Clear {code}"),
                        html.Button(html.Strong(
                        f"{selection(value)['count']:g} {STRUCTURE_NAMES[selection(value)['structure']]}"
                        if selection(value)["count"] else "0"),
                        id={"type": "seac-month", "code": code}, n_clicks=0,
                        draggable="true" if selection(value)["count"] else "false",
                        className="seac-month-value", title=f"Edit {code}: count and structure")],
                        className="seac-month-cell")
            for code, value in row.items()]


def apply_structure(row, code, count, structure):
    if count is None or not isfinite(float(count)) or float(count) != int(count):
        raise ValueError("Count must be a whole number")
    if structure not in PRESETS:
        raise ValueError("Choose a structure")
    if code not in row:
        raise ValueError("Select a month")
    updated = dict(row)
    updated[code] = {"count": int(count), "structure": structure}
    return updated


def today():
    return pd.Timestamp.now(tz="Europe/London").tz_localize(None).normalize()


def contract_chain(product, count=48, skip=0):
    calendars = [dict(load_expiries(symbol)) for symbol in (("CL", "CO") if product == "CL-CO" else (product,))]
    active = [code for code in set.intersection(*(set(items) for items in calendars))
              if min(items[code] for items in calendars) >= today().date()]
    if not active:
        raise ValueError("No unexpired contracts found in the expiry calendar")
    first = min(active, key=lambda code: (int(code[1:]), MONTHS.index(code[0])))
    start = pd.Timestamp(2000 + int(first[1:]), MONTHS.index(first[0]) + 1, 1) + pd.DateOffset(months=skip)
    return [f"{MONTHS[stamp.month-1]}{stamp.year % 100:02d}" for stamp in pd.date_range(start, periods=count, freq="MS")]


def format_legs(legs):
    # Earliest delivery anchors expiry alignment, regardless of the entered order.
    totals = {}
    for weight, code in legs:
        if not isfinite(float(weight)):
            raise ValueError("Contract weights must be finite numbers")
        totals[code] = totals.get(code, 0) + float(weight)
    if not all(isfinite(weight) for weight in totals.values()):
        raise ValueError("Combined contract weights must be finite")
    items = sorted(((code, weight) for code, weight in totals.items() if weight),
                   key=lambda item: (int(item[0][1:]), MONTHS.index(item[0][0])))
    if not items:
        raise ValueError("Enter at least one nonzero contract weight")
    return "".join(("-" if weight < 0 else "+" if index else "")
                   + (f"{abs(weight):g}*" if abs(weight) != 1 else "") + code
                   for index, (code, weight) in enumerate(items))


def expression_from_weights(rows):
    legs = []
    for code, value in (rows[0] if rows else {}).items():
        item = selection(value)
        start = (2000 + int(code[1:])) * 12 + MONTHS.index(code[0])
        for offset, weight in enumerate(PRESETS[item["structure"]]):
            year, month = divmod(start + offset, 12)
            legs.append((float(item["count"] or 0) * weight, f"{MONTHS[month]}{year % 100:02d}"))
    return format_legs(legs)


def latest_years(count, expression):
    # Match the seasonality engine: the first net leg anchors the contract year.
    end = 2000 + int(parse_expression(expression)[0][1][1:])
    maximum = end - 1999
    if count is None or not isfinite(float(count)) or float(count) != int(count) or not 1 <= int(count) <= maximum:
        raise ValueError(f"Years must be a whole number from 1 to {maximum}")
    return set(range(end - int(count) + 1, end + 1))


@lru_cache(maxsize=6)
def history(product, expression, start_year, end_year, window, signature):
    reader = MarketData()
    engine = None
    try:
        engine = Seasonality(reader, include_live=False)
        return engine.working_day_expression_seasonality(product, expression, start_year, end_year, window)
    finally:
        if engine is not None:
            engine.close()
        reader.close()


def prepare_result(result, requested, exclude, window, view, flags):
    selected = (set(result.get("series", {})) & requested) - set(exclude or [])
    series = {}
    for year in sorted(selected):
        frame = result["series"][year].copy()
        frame = frame.loc[(frame.index >= -window) & (frame.index <= 0)]
        frame = frame.loc[~frame.index.duplicated(keep="last")].sort_index()
        if view == "change":
            frame["value"] = frame.value.diff()
        frame = frame.dropna(subset=["value"])
        if frame.empty:
            continue
        if "zero" in flags:
            frame["value"] -= frame.value.iloc[0]
        series[year] = frame
    if not series:
        raise ValueError("No matching SEAC history for these legs, years and window")
    combined = pd.concat([frame.value.rename(year) for year, frame in series.items()], axis=1).sort_index()
    return {"series": series, "combined": combined, "warnings": [],
            "average": combined.mean(axis=1) if "average" in flags and len(series) > 1 else None,
            "xaxis_title": "Working days to expiry"}


def empty_figure(message="Set contract weights and press Plot"):
    figure = go.Figure()
    figure.update_layout(template="plotly_dark", paper_bgcolor="black", plot_bgcolor="black", height=650,
                         margin=dict(l=55, r=20, t=40, b=45), xaxis=dict(visible=False), yaxis=dict(visible=False))
    figure.add_annotation(text=message, x=.5, y=.5, xref="paper", yref="paper", showarrow=False)
    return figure


def live_coordinates(config, quote):
    """Map a fresh quote to the plotted expiry axis and price transformation."""
    if not config or not quote:
        return [], []
    try:
        timestamp = pd.Timestamp(quote["timestamp"])
        timestamp = timestamp.tz_localize("UTC") if timestamp.tzinfo is None else timestamp.tz_convert("UTC")
        age = (pd.Timestamp.now(tz="UTC") - timestamp).total_seconds()
        value = float(quote["value"])
        if not isfinite(value) or value == -100 or not -5 <= age <= 120:
            return [], []
        day = timestamp.tz_localize(None).normalize()
        expiry = pd.Timestamp(config["expiry"])
        offset = -int(np.busday_count(day.date(), expiry.date()))
        if day > expiry or not -config["window"] <= offset <= 0:
            return [], []
        points = config["points"]
        if config["view"] == "change":
            earlier = [point for point in points if pd.Timestamp(point[0]) < day]
            if not earlier:
                return [], []
            value -= earlier[-1][1]
        if "zero" in config["flags"]:
            baseline = points[0][1]
            if config["view"] == "change":
                if len(points) < 2:
                    return [], []
                baseline = points[1][1] - points[0][1]
            value -= baseline
        return [offset], [value]
    except (KeyError, ValueError, TypeError, OverflowError):
        return [], []


def live_config(base, result, product, expression, window, view, flags):
    anchor = parse_expression(expression)[0][1]
    year = 2000 + int(anchor[1:])
    expiry = OfficialExpiryLookup().get("CL" if product == "CL-CO" else product, anchor)
    if year not in result["series"] or expiry is None:
        return None
    frame = base["series"][year].sort_index()
    frame = frame.loc[(frame.index >= -window) & (frame.index <= 0)]
    frame = frame.loc[~frame.index.duplicated(keep="last")].dropna(subset=["value"])
    if frame.empty:
        return None
    return {"product": product, "expression": expression, "expiry": str(expiry),
            "window": window, "view": view, "flags": flags,
            "points": [[pd.Timestamp(row.date).isoformat(), float(row.value)] for row in frame.itertuples()]}


def builder_layout():
    codes = contract_chain("CL")
    weights = {code: {"count": 0, "structure": "outright"} for code in codes}
    return html.Div([
        html.Div([
            html.Label([html.Span("Years"), dcc.Input(id="seac-years", type="number",
                min=1, max=100, step=1, value=8, debounce=True)],
                title="Latest N contract years, ending with the selected expression's anchor year"),
            html.Details([html.Summary("Exclude years"),
                dcc.Checklist(options=[],
                    value=[], id="seac-exclude", className="seac-exclude-options")], className="seac-exclude-menu"),
            dcc.Checklist([{"label": "Average", "value": "average"}, {"label": "Zero base", "value": "zero"}], [], id="seac-flags", inline=True),
            html.Label([html.Span("Product"), dcc.Dropdown(list(PRODUCTS), "CL", id="seac-product", clearable=False)]),
            html.Label([html.Span("Contracts"), dcc.Input(id="seac-count", type="number", min=3, max=120, value=48, step=1, debounce=True)]),
            html.Label([html.Span("Skip months"), dcc.Input(id="seac-skip", type="number", min=0, max=120, value=0, step=1, debounce=True)]),
            html.Label([html.Span("Lookback"), dcc.Input(id="seac-window", type="number",
                min=1, max=2000, step=1, value=365, debounce=True)], title="Lookback in working days"),
            html.Button("Plot", id="seac-plot", n_clicks=0, className="seac-plot-button"),
        ], className="seac-toolbar seac-controls seac-control-bar"),
        html.Div([
            dcc.Store(id="seac-weights", data=[weights]),
            html.Div(month_cells(weights), id="seac-months", className="seac-months"),
            dcc.Store(id="seac-selected-month"),
            dcc.Store(id="seac-month-move"),
            dcc.Store(id="seac-structure-wheel"),
            html.Div([
                html.Strong(id="seac-editor-title"),
                html.Label(["Count", dcc.Input(id="seac-leg-count", type="number", value=1, step=1)]),
                html.Label(["St.", dcc.Dropdown([
                    {"label": "Outright", "value": "outright"},
                    {"label": "Spread", "value": "spread"},
                    {"label": "Fly", "value": "fly"},
                    {"label": "Defly", "value": "defly"}], "outright", id="seac-structure", clearable=False, searchable=False)]),
                html.Button("×", id="seac-close-month", n_clicks=0, title="Close", **{"aria-label": "Close month editor"}),
                html.Span(id="seac-editor-error", className="seac-error"),
            ], id="seac-month-editor", className="seac-month-editor", style={"display": "none"}),
            html.Div(id="seac-chain-note", className="seac-chain-note"),
        ], id="seac-chain"),
        html.Div(id="seac-expression", className="seac-expression"),
        dcc.Store(id="seac-live-config"),
        dcc.Store(id="seac-live-point"),
        dcc.Store(id="seac-live-rendered"),
        dcc.Interval(id="seac-live-tick", interval=10_000),
        dcc.Loading([html.Div(id="seac-status", className="seac-status"),
                     dcc.Graph(id="seac-chart", figure=empty_figure(), config={"displaylogo": False, "scrollZoom": False,
                         "responsive": True, "toImageButtonOptions": {"filename": "seac_seasonality", "scale": 2}}, className="seac-chart")]),
        html.Details([html.Summary("Chart notes"), html.Div(id="seac-notes", children="Edit weights, then press Plot. "
            "All years align to expiry. White marks the latest displayed year. "
            "Volume and open interest are not stored in the settlement database.")], className="seac-notes"),
    ], className="seac-builder")


def register_seac_callbacks(app):
    app.clientside_callback(
        """function(point, config) {
            const skip = window.dash_clientside.no_update;
            if (!point || !config || !point.bid || !point.ask || point.revision !== config.revision) return skip;
            const graph = document.querySelector('#seac-chart .js-plotly-plot');
            const updates = [
                {trace: point.trace, name: 'Live price', x: point.x, y: point.y},
                {trace: config.bid_trace, name: 'Best bid', x: point.bid.x, y: point.bid.y},
                {trace: config.ask_trace, name: 'Best ask', x: point.ask.x, y: point.ask.y}
            ];
            if (!graph || !graph.data || updates.some(p => !graph.data[p.trace] ||
                graph.data[p.trace].name !== p.name)) return skip;
            return Plotly.restyle(graph, {x: updates.map(p => p.x), y: updates.map(p => p.y)}, updates.map(p => p.trace))
                .then(function() { return point.revision; });
        }""",
        Output("seac-live-rendered", "data"), Input("seac-live-point", "data"),
        State("seac-live-config", "data"))

    app.clientside_callback(
        "function(value) { window.seacMonthStructure = value; return value; }",
        Output("seac-structure-wheel", "data"), Input("seac-structure", "value"))

    @app.callback(Output("seac-exclude", "options"), Output("seac-exclude", "value"),
                  Input("seac-years", "value"), Input("seac-weights", "data"), State("seac-exclude", "value"))
    def exclusion_years(count, rows, excluded):
        try:
            years = latest_years(count, expression_from_weights(rows))
        except (ValueError, TypeError):
            return [], []
        return ([{"label": str(year), "value": year} for year in sorted(years, reverse=True)],
                [year for year in (excluded or []) if year in years])

    @app.callback(Output("seac-months", "children"), Input("seac-weights", "data"))
    def render_months(rows):
        return month_cells((rows or [{}])[0])

    @app.callback(Output("seac-weights", "data"), Output("seac-chain-note", "children"),
                  Output("seac-editor-error", "children"),
                  Output("seac-selected-month", "data"), Output("seac-month-editor", "style"),
                  Output("seac-editor-title", "children"), Output("seac-leg-count", "value"),
                  Output("seac-structure", "value"),
                  Input({"type": "seac-month", "code": ALL}, "n_clicks"),
                  Input({"type": "seac-month-clear", "code": ALL}, "n_clicks"),
                  Input("seac-close-month", "n_clicks"), Input("seac-product", "value"),
                  Input("seac-count", "value"), Input("seac-skip", "value"),
                  Input("seac-month-move", "data"),
                  State("seac-weights", "data"), State("seac-selected-month", "data"),
                  State("seac-leg-count", "value"), State("seac-structure", "value"))
    def edit_month(clicks, clear_clicks, _, product, count, skip, move, rows, selected, leg_count, structure):
        trigger = ctx.triggered_id
        if isinstance(trigger, dict) and not ctx.triggered[0].get("value"):
            return (no_update,) * 8
        row = (rows or [{}])[0]
        try:
            clearing = isinstance(trigger, dict) and trigger["type"] == "seac-month-clear"
            if selected and trigger != "seac-product" and not (clearing and trigger["code"] == selected):
                row = apply_structure(row, selected, leg_count, structure)
            if trigger == "seac-month-move":
                source, target = move["source"], move["target"]
                if source == target or source not in row or target not in row or not selection(row[source])["count"]:
                    return (no_update,) * 8
                row = {**row, target: dict(selection(row[source])), source: {"count": 0, "structure": "outright"}}
                return [row], f"Moved {source} to {target} · press Plot to update chart", "", None, {"display": "none"}, "", 0, "outright"
            if clearing:
                row = apply_structure(row, trigger["code"], 0, "outright")
                return [row], f"Cleared {trigger['code']} · press Plot to update chart", "", None, {"display": "none"}, "", 0, "outright"
            if isinstance(trigger, dict):
                code = trigger["code"]
                item = selection(row[code])
                # Do not rebuild buttons while opening: their reset click counts would close the popup.
                data = [row] if selected and selected != code else no_update
                return data, no_update, "", code, {"display": "flex"}, code, item["count"], item["structure"]
            if trigger == "seac-close-month":
                return [row], "Saved · press Plot to update chart", "", None, {"display": "none"}, "", 0, "outright"
            count, skip = max(3, min(120, int(count or 48))), max(0, min(120, int(skip or 0)))
            codes = contract_chain(product, count, skip)
            keep = trigger in ("seac-count", "seac-skip")
            row = {code: row.get(code, {"count": 0, "structure": "outright"}) if keep else
                   {"count": 0, "structure": "outright"} for code in codes}
            return [row], "Click a month · edits save when the popup closes · press Plot to update chart", "", None, {"display": "none"}, "", 0, "outright"
        except (ValueError, TypeError, KeyError, OSError) as error:
            return no_update, no_update, str(error), no_update, {"display": "flex"}, no_update, no_update, no_update

    @app.callback(Output("seac-expression", "children"),
                  Input("seac-weights", "data"), Input("seac-product", "value"))
    def expression_preview(rows, product):
        try:
            expression = f"{product} · {expression_from_weights(rows)}"
        except (ValueError, TypeError) as error:
            expression = str(error)
        return "Expression preview: " + expression

    @app.callback(Output("seac-chart", "figure"), Output("seac-status", "children"),
                  Output("seac-notes", "children"), Output("seac-live-config", "data"), Input("seac-plot", "n_clicks"),
                  *[State(name, "value") for name in ("seac-product", "seac-years", "seac-exclude",
                    "seac-window", "seac-flags")],
                  State("seac-weights", "data"), State("seac-selected-month", "data"),
                  State("seac-leg-count", "value"), State("seac-structure", "value"), prevent_initial_call=True)
    def plot(_, product, years, exclude, window, flags, rows, selected, leg_count, structure):
        view = "overlay"
        try:
            if selected:
                rows = [apply_structure((rows or [{}])[0], selected, leg_count, structure)]
            expression = expression_from_weights(rows)
            requested = latest_years(years, expression)
            if window is None or not 1 <= float(window) <= 2000 or float(window) != int(window):
                raise ValueError("Lookback must be a whole number from 1 to 2000 working days")
            window, flags = int(window), flags or []
            if product not in PRODUCTS:
                raise ValueError("Select a supported product")
            signature = database_signature()
            if signature is None:
                raise ValueError("Master database unavailable")
            base = history(product, expression, min(requested), max(requested), window, tuple(signature))
            result = prepare_result(base, requested, exclude, window, view, flags)
            title = f"{product} · {format_contract_label(expression)}"
            figure = SeasonalityPlotter().build_seasonality_figure(result, title, height=690)
            figure.update_layout(font=dict(family="Arial, sans-serif", size=12, color="#e5e7eb"),
                margin=dict(l=60, r=22, t=100, b=48), title=dict(x=.5, y=.98, xanchor="center", yanchor="top", font=dict(size=14)),
                yaxis_title="Daily price change" if view == "change" else "Price change from first observation" if "zero" in flags else "Settlement price",
                uirevision=f"seac:{product}:{expression}:{sorted(requested)}:{window}:{view}:{flags}",
                legend=dict(orientation="h", x=.5, xanchor="center", y=1.03), hovermode="x unified")
            figure.update_xaxes(zeroline=True, zerolinecolor="#737373", showspikes=True)
            figure.update_yaxes(zeroline=True, zerolinecolor="#d4d4d4", gridcolor="#232323")
            config = live_config(base, result, product, expression, window, view, flags)
            quote = fetch_live_quote(product, expression) if config else None
            x, y = live_coordinates(config, quote)
            figure.add_trace(go.Scatter(x=x, y=y, mode="markers", name="Live price",
                marker=dict(size=7, color="#22c55e", line=dict(color="white", width=1)),
                hovertemplate="Average (bid + ask) / 2<br>Value: %{y}<extra></extra>"))
            if config:
                config["trace"] = len(figure.data) - 1
                config["revision"] = uuid4().hex
            for side, color, symbol in (("bid", "#60a5fa", "triangle-up"), ("ask", "#fbbf24", "triangle-down")):
                x, y = live_coordinates(config, {**quote, "value": quote.get(side)} if quote else None)
                figure.add_trace(go.Scatter(x=x, y=y, mode="markers", name="Best " + side,
                    marker=dict(size=10, color=color, symbol=symbol),
                    hovertemplate="Best " + side + "<br>Value: %{y}<extra></extra>"))
                if config:
                    config[side + "_trace"] = len(figure.data) - 1
            observations = sum(len(frame) for frame in result["series"].values())
            status = f"{len(result['series'])} years · {observations:,} observations · {window} working days to expiry · Daily settlements · Plot to apply edits"
            notes = [html.P("White = latest displayed year. All years align to expiry. "
                            "Average uses settlement observations; gaps are not filled. Live markers show best bid (blue), "
                            "best ask (yellow), and their average (green), from the shared manager. Only the live markers "
                            "refresh every 10 seconds. Quotes older than 2 minutes are hidden. No volume/OI data is included.")]
            notes += [html.Div(warning) for warning in base.get("warnings", [])[:25]]
            return figure, status, notes, config
        except (ValueError, TypeError, KeyError, OSError, RuntimeError, duckdb.Error) as error:
            return empty_figure("Unable to plot these settings"), html.Span(str(error), className="seac-error"), str(error), None

    @app.callback(Output("seac-live-point", "data"),
                  Input("seac-live-tick", "n_intervals"), State("seac-live-config", "data"), prevent_initial_call=True)
    def refresh_live_dot(_, config):
        if not config:
            raise PreventUpdate
        quote = fetch_live_quote(config["product"], config["expression"])
        x, y = live_coordinates(config, quote)
        point = {"revision": config["revision"], "trace": config["trace"], "x": x, "y": y}
        for side in ("bid", "ask"):
            x, y = live_coordinates(config, {**quote, "value": quote.get(side)} if quote else None)
            point[side] = {"x": x, "y": y}
        return point


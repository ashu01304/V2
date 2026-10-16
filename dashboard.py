from threading import Lock, Thread
from datetime import datetime
import numpy as np
import pandas as pd
import plotly.graph_objects as go
from dash import (
    Dash, Input, Output, State, ALL, Patch, ctx, dcc, html,
    dash_table, no_update, set_props,
)

from market_data.chart_updates import preserve_live_charts
from market_data import LiveMarketDataClient, MarketData
from requests import RequestException
from analysis.expression import parse_expression
from analysis.forward_curve import ForwardCurve
from analysis.plotting import SeasonalityPlotter
from analysis.rollover import StrategyRollover
from analysis.seasonality import Seasonality
from analysis.contract_universe import load_universe
from dashboard_components.minute_charts import minute_charts_layout, register_minute_chart_callbacks
from dashboard_components.bookmark_ranges import register_bookmark_ranges

RANK_YEARS = 4


def log_dashboard(message):
    print(
        f"[dashboard {datetime.now().strftime('%H:%M:%S')}] {message}",
        flush=True,
    )
CONTRACT_MONTHS = {
    "F": "Jan", "G": "Feb", "H": "Mar", "J": "Apr",
    "K": "May", "M": "Jun", "N": "Jul", "Q": "Aug",
    "U": "Sep", "V": "Oct", "X": "Nov", "Z": "Dec",
}


def format_contract_label(contract):
    contract = str(contract)
    month = CONTRACT_MONTHS.get(contract[:1])
    return f"{contract[0]} ({month}) {contract[1:]}" if month else contract


def expression_metrics(sznlty, symbol, expression, rank_years):
    legs = parse_expression(expression)
    codes = sorted({code for _, code in legs})
    histories = sznlty.db.get_contract_history(symbol, codes)
    if not legs or any(code not in histories or histories[code].empty for code in codes):
        return None, None
    common = None
    for code in codes:
        dates = pd.DatetimeIndex(histories[code].index).normalize()
        common = dates if common is None else common.intersection(dates)
    if common is None or common.empty:
        return None, None
    anchor = legs[0][1]
    expiry = sznlty.official_or_last_date(symbol, anchor, histories[anchor])
    required = max(0, int(np.busday_count(
        common.max().to_datetime64().astype("datetime64[D]"),
        expiry.to_datetime64().astype("datetime64[D]"),
    )))
    current_year = 2000 + int(anchor[1:])
    result = sznlty.working_day_expression_seasonality(
        symbol, expression, current_year - rank_years + 1, current_year,
        max(400, required + 10),
    )
    combined = result.get("combined", pd.DataFrame())
    current = next((column for column in combined if int(column) == current_year), None)
    if current is None or combined[current].dropna().empty:
        return None, None
    live = combined[current].dropna()
    settlement = sum(
        coefficient * histories[code].loc[common.max(), "Close"]
        for coefficient, code in legs
    )
    value = float(live.iloc[-1])
    return (value, value - float(settlement)), result


def latest_settlement_date(database):
    date = database.latest_settlement_date()
    return pd.Timestamp(date).strftime("%Y-%m-%d") if date is not None else "unavailable"


def latest_live_timestamp():
    client = LiveMarketDataClient()
    try:
        # Expression prices also use the bridge's received_at timestamp.
        rows = client.snapshot()
        timestamps = pd.to_datetime(
            [row.get("received_at") for row in rows if row.get("value") is not None],
            utc=True, errors="coerce",
        ).dropna()
        if timestamps.empty:
            return "unavailable"
        return timestamps.max().tz_convert("Europe/London").strftime("%Y-%m-%d %H:%M:%S %Z")
    except RequestException:
        return "unavailable (live service unreachable)"
    finally:
        client.close()

def copy_frames(frames):
    return {name: frame.copy() for name, frame in frames.items()}


def product_frames(symbol):
    existing = dashboard_state["frames"].get(symbol)
    return copy_frames(existing if existing is not None else {
        "values": empty_frames["values"], "changes": empty_frames["values"],
    })


def calculate_dashboard(
    universe, selected_columns, symbols, rank_years, job_id, live_refresh=False,
):
    log_dashboard(f"job {job_id} started ({'live refresh' if live_refresh else 'analysis'})")
    db = MarketData()
    try:
        sznlty = Seasonality(db)
        with state_lock:
            frames = {symbol: product_frames(symbol) for symbol in symbols}
            results = {key: value for key, value in dashboard_state["results"].items()
                       if key[0] in symbols}
            dashboard_state["settlement_date"] = latest_settlement_date(db)
        for symbol in symbols:
            for row, strategies in enumerate(universe["contracts"].values()):
                for strategy in selected_columns:
                    expression = strategies[strategy]
                    cache_key = (symbol, expression, rank_years[symbol])
                    with state_lock:
                        if job_id != dashboard_state["job_id"]:
                            return
                        cached = dashboard_state["cache"].get(cache_key)
                    if cached is None:
                        cached = expression_metrics(
                            sznlty, symbol, expression, rank_years[symbol],
                        )
                        with state_lock:
                            dashboard_state["cache"][cache_key] = cached
                    metrics, result = cached
                    if metrics is None:
                        continue
                    value, change = metrics
                    frames[symbol]["values"].at[row, strategy] = value
                    frames[symbol]["changes"].at[row, strategy] = change
                    if result is not None:
                        results[(symbol, expression)] = result
            publish_dashboard_snapshot(frames, results, [symbol], job_id, live_refresh)
    except Exception:
        with state_lock:
            if job_id == dashboard_state["job_id"]:
                dashboard_state["processing"] = False
                dashboard_state["version"] += int(not live_refresh)
        log_dashboard(f"job {job_id} failed")
        raise
    finally:
        db.close()
    with state_lock:
        if job_id == dashboard_state["job_id"]:
            dashboard_state["processing"] = False
            dashboard_state["version"] += int(not live_refresh)
            log_dashboard(f"job {job_id} finished")


def publish_dashboard_snapshot(frames, results, symbols, job_id, live_refresh=False):
    with state_lock:
        if job_id != dashboard_state["job_id"]:
            return
        snapshot = {symbol: copy_frames(frames)
                    for symbol, frames in dashboard_state["frames"].items()}
        snapshot.update({symbol: copy_frames(frames[symbol]) for symbol in symbols})
        displays = {symbol: make_display(frames, symbol) for symbol, frames in snapshot.items()}
        pending_live_updates = dashboard_state["live_updates"]
        if live_refresh:
            updates = {
                symbol: {column: displays[symbol][column].tolist() for column in columns}
                for symbol in symbols
            }
            if updates:
                pending_live_updates = {**(pending_live_updates or {}), **updates}
        dashboard_state.update({
            "frames": snapshot,
            "displays": displays,
            "results": {**dashboard_state["results"], **results},
            "version": dashboard_state["version"] + int(not live_refresh),
            "updated_symbols": list(symbols),
            "live_updates": pending_live_updates,
        })

def format_value(value, precision=3):
    if value is None or pd.isna(value):
        return "-"

    return f"{float(value):.{precision}f}"

# Build the current expression matrix from expiry data.
universe = load_universe()

columns = universe["columns"]

def format_change(value, precision=3):
    if value is None or pd.isna(value):
        return "-"
    return f"{float(value):+.{precision}f}"


def change_color(value):
    if value is None or pd.isna(value):
        return "#3E362E"
    if float(value) > 0:
        return "#146C2E"
    if float(value) < 0:
        return "#A61B1B"
    return "#3E362E"


def format_change_display(value, precision=3):
    change = format_change(value, precision)
    if change == "-":
        return "—", change
    numeric_change = float(value)
    if numeric_change > 0:
        return "▲", change
    if numeric_change < 0:
        return "▼", change
    return "—", change


def make_display(frames, symbol=None):
    precision = 2 if symbol in {"CL", "CO", "CL-CO"} else 3
    values = frames["values"]
    changes = frames.get("changes")
    display = values.copy()
    for column in columns:
        if changes is None:
            display[column] = [format_value(value, precision) for value in values[column]]
            continue
        display[column] = [
            (
                f"<div class=\"market-cell\">"
                f"<div class=\"market-live\">{format_value(value, precision)}</div>"
                f"<div class=\"market-change\" style=\"color: {change_color(change)};\">"
                f"<span class=\"market-arrow\">{format_change_display(change, precision)[0]}</span> "
                f"{format_change_display(change, precision)[1]}</div>"
                f"</div>"
            )
            if pd.notna(change)
            else format_value(value, precision)
            for value, change in zip(values[column], changes[column])
        ]
    return display

product_db = MarketData()
try:
    products = product_db.symbols()
    settlement_date = latest_settlement_date(product_db)
    if "CL" in products and "CO" in products and "CL-CO" not in products:
        products.append("CL-CO")
finally:
    product_db.close()

empty_frame = pd.DataFrame({"Contract": list(universe["contracts"])})
empty_frames = {"values": empty_frame.reindex(columns=["Contract", *columns])}
dashboard_state = {"displays": {}, "frames": {}, "results": {}, "cache": {},
                   "version": 0, "job_id": 0,
                   "processing": False, "updated_symbols": [],
                   "live_updates": None,
                   "settlement_date": settlement_date, "live_timestamp": "unavailable"}
state_lock = Lock()

def control(label, component):
    return html.Label([html.Span(label), component], className="control-group")


app = Dash(__name__)
app.title = "Market Curves"
plotter = SeasonalityPlotter()
app.layout = html.Div([
    html.Div([
        control("Product", dcc.Dropdown(
            id="product-selector",
            options=[{"label": product, "value": product} for product in products],
            value=None, placeholder="Select products", multi=True,
            persistence=True, persistence_type="local",
            clearable=True, className="product-dropdown")),
        html.Div(
            id="data-status",
            children="Latest settlement date: — | Latest live data: —",
            className="data-status",
        ),
        html.Button("Apply", id="apply-parameters", n_clicks=0,
                    className="apply-button"),
    ], className="control-row"),
    html.Details([
        html.Summary("Columns"),
        dcc.Checklist(
            id="column-selector",
            options=[{"label": column, "value": column} for column in columns],
            value=[column for column in columns if not column.endswith("MS")],
            inline=True,
            persistence=True,
            persistence_type="local",
            inputStyle={"marginRight": "3px"},
            labelStyle={"marginRight": "10px"},
        ),
    ], className="columns-panel"),
    html.Div(id="product-tables", children=html.Div()),
    html.Div([
        html.Div(id="modal-backdrop", n_clicks=0,
                 style={
                     "position": "fixed",
                     "inset": "0",
                     "zIndex": "1000",
                     "backgroundColor": "rgba(0,0,0,0.75)",
                     "cursor": "pointer",
                 }),
        html.Div([
            html.Button("×", id="close-modal", style={
                "position": "absolute", "top": "10px", "right": "14px",
                "zIndex": "1002", "fontSize": "30px", "lineHeight": "30px",
                "width": "40px", "height": "40px", "color": "white",
                "backgroundColor": "#865D36", "border": "none",
                "borderRadius": "6px", "cursor": "pointer",
            }),
            dcc.Graph(id="seasonality-chart", style={
                "height": "610px", "width": "100%", "flexShrink": "0"}),
            html.Div([
                dcc.Graph(id="rollover-chart", style={"height": "610px", "width": "50%"}),
                dcc.Graph(id="forward-curve-chart", style={"height": "610px", "width": "50%"}),
            ], style={"display": "flex", "width": "100%", "height": "610px",
                      "flexShrink": "0", "marginTop": "12px"}),
            minute_charts_layout(),
        ], style={"position": "relative", "zIndex": "1001",
                  "display": "flex", "flexDirection": "column",
                  "backgroundColor": "#3E362E", "overflowY": "auto",
                  "width": "99%", "height": "90vh", "padding": "4px 4px 0"}),
    ], id="chart-modal", style={"display": "none"}),
    dcc.Store(id="product-year-preferences", storage_type="local", data={}),
    dcc.Store(id="data-version", data=0),
    dcc.Store(id="live-cell-updates"),
    dcc.Store(id="live-cell-update-ack"),
    dcc.Store(id="selected-cell"),
    dcc.Interval(id="refresh-results", interval=500, n_intervals=0, disabled=True),
    dcc.Interval(id="live-chart-refresh", interval=10_000, n_intervals=0),
], style={"backgroundColor": "#3E362E", "color": "#FFF8EF",
          "minHeight": "100vh", "padding": "8px"})


def build_rank_years(symbols, values, ids, saved_years=None):
    values_by_product = dict(saved_years or {})
    values_by_product.update({
        item["product"]: max(2, int(value or RANK_YEARS))
        for item, value in zip(ids or [], values or []) if value is not None
    })
    return {
        symbol: max(2, int(values_by_product.get(symbol) or RANK_YEARS))
        for symbol in symbols
    }


def start_dashboard_job(symbols, selected, live_refresh):
    with state_lock:
        if live_refresh:
            if dashboard_state["processing"]:
                return None
            dashboard_state["cache"] = {
                key: value
                for key, value in dashboard_state["cache"].items()
                if key[0] not in symbols
            }

        job_id = dashboard_state["job_id"] + 1
        frames = {symbol: product_frames(symbol) for symbol in symbols}
        dashboard_state.update({
            "frames": frames,
            "displays": {
                symbol: make_display(product_frames, symbol)
                for symbol, product_frames in frames.items()
            },
            "version": (
                dashboard_state["version"]
                if live_refresh
                else dashboard_state["version"] + 1
            ),
            "job_id": job_id,
            "processing": bool(selected),
            "updated_symbols": [],
            "live_updates": None,
        })
        return job_id


def launch_dashboard_job(symbols, selected, rank_years, job_id, live_refresh):
    if not selected or job_id is None:
        return
    Thread(
        target=calculate_dashboard,
        args=(
            universe,
            selected,
            symbols,
            rank_years,
            job_id,
            live_refresh,
        ),
        daemon=True,
    ).start()


def dashboard_response(displayed_version, live_updates=no_update, disabled=None):
    # Caller holds state_lock so the four outputs describe one snapshot.
    version = dashboard_state["version"]
    return (
        (f"Latest settlement date: {dashboard_state['settlement_date']} | "
         f"Latest live data: {dashboard_state['live_timestamp']}"),
        version if version != displayed_version else no_update,
        not dashboard_state["processing"] if disabled is None else disabled,
        live_updates,
    )


@app.callback(
    Output("data-status", "children"),
    Output("data-version", "data"),
    Output("refresh-results", "disabled"),
    Output("live-cell-updates", "data"),
    Input("apply-parameters", "n_clicks"),
    Input("refresh-results", "n_intervals"),
    Input("live-chart-refresh", "n_intervals"),
    Input({"type": "rank-apply", "product": ALL}, "n_clicks"),
    State("product-selector", "value"),
    State("column-selector", "value"),
    State({"type": "rank-years", "product": ALL}, "value"),
    State({"type": "rank-years", "product": ALL}, "id"),
    State("data-version", "data"),
    State("product-year-preferences", "data"),
)
def apply_parameters(
    _, __, ___, _rank_clicks, symbols, selected, rank_years, rank_ids,
    displayed_version, saved_years,
):
    trigger = ctx.triggered_id
    if trigger is None or trigger == "live-chart-refresh":
        # Refresh the header even when no products are selected or a job is busy.
        timestamp = latest_live_timestamp()
        with state_lock:
            dashboard_state["live_timestamp"] = timestamp
    if isinstance(trigger, dict) and trigger.get("type") == "rank-apply":
        # Inserting/rebuilding tables also fires their dynamic button inputs.
        # Only a real click should start an analysis job.
        clicked = next(
            (clicks for item, clicks in zip(ctx.inputs_list[3], _rank_clicks or [])
             if item["id"] == trigger),
            None,
        )
        if not clicked:
            return (no_update,) * 4
    if trigger == "refresh-results":
        with state_lock:
            live_updates = dashboard_state["live_updates"]
            dashboard_state["live_updates"] = None
            return dashboard_response(displayed_version, live_updates or no_update)
    symbols = list(dict.fromkeys(symbols or []))
    if not symbols:
        with state_lock:
            return dashboard_response(displayed_version, disabled=True)
    selected = [column for column in columns if column in (selected or [])]
    rank_years = build_rank_years(symbols, rank_years, rank_ids, saved_years)
    live_refresh = trigger == "live-chart-refresh"
    job_id = start_dashboard_job(symbols, selected, live_refresh)
    launch_dashboard_job(
        symbols, selected, rank_years, job_id, live_refresh
    )
    with state_lock:
        return dashboard_response(displayed_version)

def render_tables(displays, saved_years=None):
    rank_years = build_rank_years(list(displays), [], [], saved_years)
    table_style = {"overflowX": "auto", "flex": "0 0 auto", "margin": "0"}
    cell_style = {
        "backgroundColor": "#A69080",
        "color": "#3E362E",
        "border": "1px solid #93785B",
        "textAlign": "center",
        "minWidth": "98px",
        "width": "98px",
        "maxWidth": "98px",
        "height": "68px",
        "fontSize": "10px",
        "padding": "2px",
        "lineHeight": "16px",
    }
    header_style = {
        "backgroundColor": "#865D36",
        "color": "#FFF8EF",
        "fontWeight": "bold",
        "textAlign": "center",
    }
    table_options = {
        "style_table": table_style, "style_header": header_style,
        "style_data_conditional": [{"if": {"row_index": "odd"}, "backgroundColor": "#AC8968"}],
    }
    first_display = next(iter(displays.values()), make_display(empty_frames))
    contract_display = first_display[["Contract"]].copy()
    contract_display["Contract"] = contract_display["Contract"].map(
        format_contract_label
    )
    contract_table = html.Div(
        [
            html.H4(
                "Contract",
                style={
                    "color": "#FFF8EF",
                    "height": "22px",
                    "lineHeight": "22px",
                    "margin": "0",
                },
            ),
            dash_table.DataTable(
                id={"type": "contract-table", "product": "contracts"},
                data=contract_display.to_dict("records"),
                columns=[{"name": "Contract", "id": "Contract"}],
                style_cell={
                    **cell_style,
                    "minWidth": "128px",
                    "width": "128px",
                    "maxWidth": "128px",
                    "fontSize": "16px",
                    "fontWeight": "bold",
                    "lineHeight": "22px",
                },
                **table_options,
            ),
        ],
        id="contract-labels",
        style={"flex": "0 0 auto"},
    )
    product_tables = []
    for product, display in displays.items():
        product_tables.append(
            html.Div(
                [
                    html.H4(
                        [
                            html.Span(product),
                            dcc.Input(
                                id={"type": "rank-years", "product": product},
                                type="number",
                                value=rank_years[product],
                                min=2,
                                step=1,
                                debounce=True,
                                persistence=True,
                                persistence_type="local",
                                className="product-rank-years",
                            ),
                            html.Button(
                                "Analyse",
                                id={"type": "rank-apply", "product": product},
                                n_clicks=0,
                                className="product-rank-apply",
                            ),
                        ],
                        style={
                            "color": "#FFF8EF",
                            "height": "22px",
                            "lineHeight": "22px",
                            "margin": "0",
                            "display": "flex",
                            "alignItems": "center",
                            "gap": "4px",
                        },
                    ),
                    dash_table.DataTable(
                        id={"type": "contracts-table", "product": product},
                        data=display.drop(columns=["Contract"]).to_dict("records"),
                        columns=[
                            {"name": column, "id": column, "presentation": "markdown"}
                            for column in display.columns
                            if column != "Contract"
                        ],
                        markdown_options={"html": True},
                        style_cell=cell_style,
                        **table_options,
                    ),
                ],
                id=f"product-table-{product}",
                style={"flex": "0 0 auto"},
            )
        )
    return html.Div(
        [contract_table, *product_tables],
        style={
            "display": "flex",
            "gap": "8px",
            "alignItems": "flex-start",
            "overflowX": "auto",
            "paddingBottom": "12px",
        },
    )


@app.callback(
    Output("product-tables", "children"),
    Input("column-selector", "value"),
    Input("data-version", "data"),
    Input("product-selector", "value"),
    Input("apply-parameters", "n_clicks"),
    State("product-year-preferences", "data"),
    State({"type": "rank-years", "product": ALL}, "value"),
    State({"type": "rank-years", "product": ALL}, "id"),
)
def update_table_columns(selected, _, selected_products, __, saved_years, values, ids):
    selected = set(selected or [])
    with state_lock:
        displays = dashboard_state["displays"]
        filtered = {}
        for product, display in displays.items():
            filtered[product] = display[
                ["Contract", *[column for column in columns if column in selected]]
            ]
    if ctx.triggered_id != "data-version":
        rank_years = build_rank_years(list(filtered), values, ids, saved_years)
        return render_tables(filtered, rank_years)

    with state_lock:
        updated_symbols = set(dashboard_state.get("updated_symbols", []))
    if not updated_symbols:
        return no_update

    patch = Patch()
    for index, (product, display) in enumerate(filtered.items(), 1):
        if product in updated_symbols:
            patch["children"][index]["children"][1]["data"] = (
                display.drop(columns=["Contract"]).to_dict("records")
            )
    return patch

@app.callback(
    Output("seasonality-chart", "figure"),
    Output("chart-modal", "style"),
    Output("selected-cell", "data"),
    Input(
        {"type": "contracts-table", "product": ALL},
        "active_cell",
        allow_optional=True,
    ),
    Input("close-modal", "n_clicks"),
    Input("modal-backdrop", "n_clicks"),
    State("selected-cell", "data"),
    prevent_initial_call=True,
)
def toggle_chart(cells, close_clicks, backdrop_clicks, selected_cell):
    hidden = {"display": "none"}
    trigger = ctx.triggered_id
    if trigger == "close-modal" or trigger == "modal-backdrop":
        cleared = {"active_cell": None, "selected_cells": []}
        for item in ctx.inputs_list[0]:
            set_props(item["id"], cleared)
        set_props({"type": "contract-table", "product": "contracts"}, cleared)
        return no_update, hidden, None
    if not isinstance(trigger, dict):
        return no_update, no_update, no_update
    # One callback owns the shared popup, regardless of how many tables exist.
    cell = next(
        (cell for item, cell in zip(ctx.inputs_list[0], cells or [])
         if item["id"] == trigger),
        None,
    )
    if not cell or cell["column_id"] == "Contract":
        return go.Figure(), hidden, None
    with state_lock:
        results = dashboard_state["results"].copy()
        symbol = trigger["product"]
        display = dashboard_state["displays"][symbol].copy()
    contract = display.iloc[cell["row"]]["Contract"]
    expression = universe["contracts"][contract][cell["column_id"]]
    result = results.get((symbol, expression))
    if result is None:
        return go.Figure(), hidden, None
    modal = {
        "display": "flex",
        "position": "fixed",
        "inset": "0",
        "zIndex": "999",
        "alignItems": "center",
        "justifyContent": "center",
    }
    return (plotter.build_seasonality_figure(result, expression, height=610), modal,
            {"contract": contract, "strategy": cell["column_id"],
             "expression": expression, "symbol": symbol,
             "years": sorted(result.get("series", {})),
             "window_days": max(400, int(abs(result["combined"].index.min())))})

@app.callback(
    Output("rollover-chart", "figure"),
    Input("selected-cell", "data"),
    State('rollover-chart', "figure"),
)
@preserve_live_charts([0])
def update_rollover(selection):
    if not selection:
        return go.Figure()
    db = MarketData()
    try:
        result = StrategyRollover(db).calculate(
            selection["symbol"], selection["expression"]
        )
    finally:
        db.close()
    return plotter.build_rollover_figure(
        result, f"{selection['strategy']} Strategy Rollover", height=610
    )

@app.callback(
    Output("forward-curve-chart", "figure"),
    Input("selected-cell", "data"),
    State('forward-curve-chart', "figure"),
)
@preserve_live_charts([0])
def update_forward_curve(selection):
    if not selection:
        return go.Figure()
    curve = ForwardCurve()
    try:
        result = curve.calculate(selection["symbol"], selection["expression"])
    except Exception:
        result = {"live": [], "settlements": []}
    finally:
        curve.close()
    return plotter.build_forward_curve_figure(
        result, selection["symbol"], selection["expression"], height=610
    )


app.clientside_callback(
    """
    function(updates) {
        if (!updates) {
            return window.dash_clientside.no_update;
        }
        Object.keys(updates).forEach(function(product) {
            var table = document.getElementById("product-table-" + product);
            if (!table) {
                return;
            }
            Object.keys(updates[product]).forEach(function(column) {
                var values = updates[product][column];
                values.forEach(function(value, row) {
                    var cell = table.querySelector(
                        '[data-dash-row="' + row +
                        '"][data-dash-column="' + column + '"]'
                    );
                    if (!cell) {
                        return;
                    }
                    // Keep Dash's markdown wrapper and its layout styles.
                    var content = cell.querySelector(".dash-cell-value");
                    if (!content) {
                        return;
                    }
                    var incoming = document.createElement("div");
                    incoming.innerHTML = value;
                    var currentMarket = content.querySelector(".market-cell");
                    var nextMarket = incoming.querySelector(".market-cell");
                    if (currentMarket && nextMarket) {
                        [".market-live", ".market-change"].forEach(function(selector) {
                            var current = currentMarket.querySelector(selector);
                            var next = nextMarket.querySelector(selector);
                            if (current.innerHTML !== next.innerHTML) {
                                current.innerHTML = next.innerHTML;
                            }
                            if (current.style.color !== next.style.color) {
                                current.style.color = next.style.color;
                            }
                        });
                    } else if (content.innerHTML !== incoming.innerHTML) {
                        content.innerHTML = incoming.innerHTML;
                    }
                });
            });
        });
        return Date.now();
    }
    """,
    Output("live-cell-update-ack", "data"),
    Input("live-cell-updates", "data"),
)


app.clientside_callback(
    """
    function(values, ids, saved) {
        var preferences = Object.assign({}, saved || {});
        var changed = false;
        (ids || []).forEach(function(id, index) {
            var value = (values || [])[index];
            if (typeof value !== "number" || !Number.isFinite(value)) {
                return;
            }
            var years = Math.max(2, Math.trunc(value));
            if (preferences[id.product] !== years) {
                preferences[id.product] = years;
                changed = true;
            }
        });
        return changed ? preferences : window.dash_clientside.no_update;
    }
    """,
    Output("product-year-preferences", "data"),
    Input({"type": "rank-years", "product": ALL}, "value"),
    State({"type": "rank-years", "product": ALL}, "id"),
    State("product-year-preferences", "data"),
)


register_minute_chart_callbacks(app)
register_bookmark_ranges(app, universe, format_contract_label)

from pathlib import Path
from threading import Lock, Thread
import numpy as np
import pandas as pd
import plotly.graph_objects as go
from dash import Dash, Input, Output, State, ctx, dcc, html, dash_table, no_update

from market_data import MarketData
from analysis.expression import parse_expression
from analysis.feature_creation import FeatureCreator
from analysis.forward_curve import ForwardCurve
from analysis.plotting import SeasonalityPlotter
from analysis.rollover import StrategyRollover
from analysis.seasonality import Seasonality
from analysis.contract_universe import load_universe

RANK_YEARS = 4
ZSCORE_WINDOW = 42
SLOPE_DAYS = 10
AMAN_LOOKBACK = 45

def expression_metrics(sznlty, features, symbol, expression, rank_years,
                       zscore_window, slope_days,
                       aman_lookback=AMAN_LOOKBACK):
    legs = parse_expression(expression)
    codes = sorted({code for _, code in legs})
    histories = sznlty.db.get_contract_history(symbol, codes)
    if not legs or any(code not in histories or histories[code].empty for code in codes):
        return None, None, None, None, None, None, None
    common = None
    for code in codes:
        dates = pd.DatetimeIndex(histories[code].index).normalize()
        common = dates if common is None else common.intersection(dates)
    if common is None or common.empty:
        return None, None, None, None, None, None, None
    anchor = legs[0][1]
    expiry = sznlty.official_or_last_date(symbol, anchor, histories[anchor])
    required = max(0, int(np.busday_count(
        common.max().to_datetime64().astype("datetime64[D]"),
        expiry.to_datetime64().astype("datetime64[D]"),
    )))
    current_year = 2000 + int(anchor[1:])
    result = sznlty.working_day_expression_seasonality(
        symbol, expression, current_year - rank_years + 1, current_year,
        max(400, required + zscore_window + 10),
    )
    combined = result.get("combined", pd.DataFrame())
    current = next((column for column in combined if int(column) == current_year), None)
    if current is None or combined[current].dropna().empty:
        return None, None, None, None, None, None, None
    live = combined[current].dropna()
    day = live.index.max()
    rank_data = features.calculate_current_rank_ratio(combined, rank_years)
    if day not in rank_data.index or pd.isna(rank_data.at[day, "Rank"]):
        return None, None, None, None, None, None, None
    rank = f"{int(rank_data.at[day, 'Rank'])}/{int(rank_data.at[day, 'Count'])}"
    zscore = features.create_live_features(
        live, {"TECH_ZScore": {"window": zscore_window}}
    ).get("TECH_ZScore")
    slope_metrics = features.calculate_forward_slope_metrics(
        combined, slope_days, rank_years - 1
    ).loc[day]
    aman = features.calculate_bollinger_signal(live, aman_lookback)["signal"]
    return (float(live.iloc[-1]), rank,
            None if pd.isna(zscore) else float(zscore),
            None if pd.isna(slope_metrics["Median"]) else float(slope_metrics["Median"]),
            None if pd.isna(slope_metrics["Direction"]) else float(slope_metrics["Direction"]),
            aman, result)

def calculate_dashboard(universe, selected_columns, symbol, rank_years, zscore_window,
                        slope_days, job_id):
    names = ("values", "ranks", "zscores", "slopes", "directions", "aman")
    results, db = {}, MarketData()
    sznlty, features = Seasonality(db), FeatureCreator()
    empty_frame = pd.DataFrame(
        None, index=range(len(universe["contracts"])),
        columns=["Contract", *columns], dtype=object,
    )
    empty_frame["Contract"] = list(universe["contracts"])
    frames = {name: empty_frame.copy() for name in names}
    try:
        batches = [selected_columns[i:i + 2] for i in range(0, len(selected_columns), 2)]
        for batch_number, batch in enumerate(batches, 1):
            for row, (_, strategies) in enumerate(universe["contracts"].items()):
                for strategy in batch:
                    with state_lock:
                        if job_id != dashboard_state["job_id"]:
                            return
                    expression = strategies[strategy]
                    metrics = expression_metrics(
                        sznlty, features, symbol, expression, rank_years,
                        zscore_window, slope_days,
                    )
                    for name, value in zip(names, metrics[:6]):
                        frames[name].at[row, strategy] = value
                    if metrics[6] is not None:
                        results[expression] = metrics[6]
            with state_lock:
                if job_id != dashboard_state["job_id"]:
                    return
                snapshot = {name: frame.copy() for name, frame in frames.items()}
                dashboard_state.update({
                    "display": make_display(snapshot), "slopes": snapshot["slopes"],
                    "zscores": snapshot["zscores"], "aman": snapshot["aman"],
                    "results": results.copy(), "version": dashboard_state["version"] + 1,
                    "title": f"{symbol} — batch {batch_number}/{len(batches)} complete",
                })
    except Exception:
        with state_lock:
            if job_id == dashboard_state["job_id"]:
                dashboard_state.update({"processing": False,
                                        "title": f"{symbol} — processing failed",
                                        "version": dashboard_state["version"] + 1})
        raise
    finally:
        db.close()
    with state_lock:
        if job_id == dashboard_state["job_id"]:
            dashboard_state.update({"processing": False,
                                    "title": f"{symbol} — Latest Contract Values",
                                    "version": dashboard_state["version"] + 1})

def format_value(value):
    if value is None or pd.isna(value):
        return "-"

    return np.format_float_positional(float(value), precision=10, trim="-")

def interpolate_color(start, end, ratio):
    rgb = tuple(round(a + (b - a) * ratio) for a, b in zip(start, end))
    return f"#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}"

def zscore_color(zscore):
    if zscore is None or pd.isna(zscore):
        return "#64748b"
    zscore = max(-2.0, min(2.0, float(zscore)))
    start, end, ratio = (
        ((220, 38, 38), (250, 204, 21), (zscore + 2) / 2)
        if zscore < 0 else
        ((250, 204, 21), (22, 163, 74), zscore / 2)
    )
    return interpolate_color(start, end, ratio)

def rank_color(rank):
    if rank is None or pd.isna(rank):
        return "#64748b"
    position, total = map(int, rank.split("/"))
    ratio = 0 if total <= 1 else (position - 1) / (total - 1)
    start, end, ratio = (
        ((22, 163, 74), (250, 204, 21), ratio * 2)
        if ratio < 0.5 else
        ((250, 204, 21), (220, 38, 38), (ratio - 0.5) * 2)
    )
    return interpolate_color(start, end, ratio)

def format_cell(value, rank, zscore, slope, direction, aman):
    if all(item is None or pd.isna(item) for item in (value, rank, zscore, slope, direction, aman)):
        return ""
    valid = format_value(value) != "-" and rank is not None and not pd.isna(rank)
    value_text = format_value(value) if valid else "-"
    rank_text = rank if valid else "-"
    zscore_text = f"{zscore:.3f}" if valid and pd.notna(zscore) else "-"
    slope_text = format_value(slope) if valid and pd.notna(slope) else "-"
    direction_text = f"{direction:+.0f}%" if valid and pd.notna(direction) else "-"
    aman_signal = aman if isinstance(aman, str) else None
    aman_text = {"LONG": "B", "SHORT": "S", "NEUTRAL": "N"}.get(aman_signal, "-")
    aman_class = aman_signal.lower() if aman_signal else "missing"
    return (
        "<div class='metric-cell'><div class='metric-left'>"
        f"<div class='metric-value'>{value_text}</div><div>M = {slope_text}</div>"
        f"<div>D = {direction_text}</div>"
        f"<div class='aman-{aman_class}'>Aman = {aman_text}</div>"
        "</div><div class='metric-right'>"
        f"<div><span class='metric-square' style='background:{zscore_color(zscore)}'></span>{zscore_text}</div>"
        f"<div><span class='metric-square' style='background:{rank_color(rank if valid else None)}'></span>{rank_text}</div>"
        "</div></div>"
    )

# Build the current expression matrix from expiry data.
universe = load_universe()

columns = universe["columns"]

def make_display(frames):
    values, ranks = frames["values"], frames["ranks"]
    zscores, slopes = frames["zscores"], frames["slopes"]
    directions, aman = frames["directions"], frames["aman"]
    display = values.copy()
    for column in columns:
        display[column] = [
            format_cell(value, rank, zscore, slope, direction, signal)
            for value, rank, zscore, slope, direction, signal in zip(
                values[column], ranks[column], zscores[column], slopes[column],
                directions[column], aman[column]
            )
        ]
    return display

product_db = MarketData()
try:
    product_db.cursor.execute("SELECT DISTINCT symbol FROM seac_settlements ORDER BY symbol")
    products = [row[0] for row in product_db.cursor.fetchall()]
finally:
    product_db.close()

empty = pd.DataFrame({"Contract": list(universe["contracts"])})
frames = {name: empty.reindex(columns=["Contract", *columns])
          for name in ("values", "ranks", "zscores", "slopes", "directions", "aman")}
seasonality_results = {}
display_df = make_display(frames)
dashboard_state = {"display": display_df, "slopes": frames["slopes"],
                   "zscores": frames["zscores"], "aman": frames["aman"],
                   "results": seasonality_results,
                   "symbol": None, "version": 0, "job_id": 0,
                   "processing": False, "title": "Select a product"}
state_lock = Lock()

def control(label, component):
    return html.Label([html.Span(label), component], className="control-group")

def number_control(label, component_id, value, minimum, step):
    return control(label, dcc.Input(id=component_id, type="number", value=value,
                                    min=minimum, step=step, debounce=True,
                                    className="control-input"))

app = Dash(__name__)
app.title = "Market Curves"
plotter = SeasonalityPlotter()
app.layout = html.Div([
    html.H3("Select a product", id="page-title"),
    html.Div([
        control("Product", dcc.Dropdown(
            id="product-selector",
            options=[{"label": product, "value": product} for product in products],
            value=None, placeholder="Select product",
            clearable=True, className="product-dropdown")),
        number_control("Rank years", "rank-years", RANK_YEARS, 2, 1),
        number_control("Z-score window", "zscore-window", ZSCORE_WINDOW, 2, 1),
        number_control("Slope days", "slope-days", SLOPE_DAYS, 1, 1),
        number_control("|Median slope| >", "slope-threshold", 0.05, None, 0.0001),
        number_control("|Z-score| >", "zscore-threshold", 1.5, None, 0.1),
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
    html.Div([
        html.Span("Highlights:"),
        dcc.Checklist(
            id="highlight-selector",
            options=[{"label": "Slope", "value": "slope"},
                     {"label": "Z-score", "value": "zscore"},
                     {"label": "Aman B/S", "value": "aman"}],
            value=["slope", "zscore", "aman"],
            inline=True,
            persistence=True,
            persistence_type="local",
            inputStyle={"marginLeft": "8px", "marginRight": "3px"},
        ),
    ], className="highlight-options"),
    dash_table.DataTable(
        id="contracts-table",
        data=display_df.to_dict("records"),
        columns=[{"name": column, "id": column, "presentation": "markdown"}
                 for column in display_df.columns],
        markdown_options={"html": True},
        style_table={"overflowX": "auto"},
        style_header={"backgroundColor": "#17243a", "color": "#f8fafc",
                      "fontWeight": "bold", "textAlign": "center"},
        style_cell={"backgroundColor": "#111827", "color": "#f8fafc",
                    "border": "1px solid #354258", "textAlign": "center",
                    "minWidth": "98px", "width": "98px", "maxWidth": "98px",
                    "height": "90px", "fontSize": "10px", "padding": "2px"},
        style_cell_conditional=[{"if": {"column_id": "Contract"},
                                 "minWidth": "48px", "width": "48px", "maxWidth": "48px"}],
        style_data_conditional=[{"if": {"row_index": "odd"},
                                 "backgroundColor": "#151f30"}],
        css=[{"selector": "p", "rule": "margin:0"}],
    ),
    html.Div([
        html.Div(id="modal-backdrop", n_clicks=0,
                 style={"position": "absolute", "inset": "0"}),
        html.Div([
            html.Button("×", id="close-modal", style={
                "position": "absolute", "top": "10px", "right": "14px",
                "zIndex": "1002", "fontSize": "30px", "lineHeight": "30px",
                "width": "40px", "height": "40px", "color": "white",
                "backgroundColor": "#dc2626", "border": "none",
                "borderRadius": "6px", "cursor": "pointer",
            }),
            dcc.Graph(id="seasonality-chart", style={
                "height": "610px", "width": "100%", "flexShrink": "0"}),
            html.Div([
                dcc.Graph(id="rollover-chart", style={"height": "610px", "width": "50%"}),
                dcc.Graph(id="forward-curve-chart", style={"height": "610px", "width": "50%"}),
            ], style={"display": "flex", "width": "100%", "height": "610px",
                      "flexShrink": "0", "marginTop": "12px"}),
        ], style={"position": "relative", "zIndex": "1001",
                  "display": "flex", "flexDirection": "column",
                  "backgroundColor": "black", "overflowY": "auto",
                  "width": "99%", "height": "90vh", "padding": "4px 4px 0"}),
    ], id="chart-modal", style={"display": "none"}),
    dcc.Store(id="data-version", data=0),
    dcc.Store(id="selected-cell"),
    dcc.Interval(id="refresh-results", interval=500, n_intervals=0, disabled=True),
    dcc.Interval(id="live-chart-refresh", interval=10_000, n_intervals=0),
], style={"backgroundColor": "#0b1220", "color": "#f8fafc",
          "minHeight": "100vh", "padding": "8px"})

@app.callback(
    Output("contracts-table", "data"),
    Output("page-title", "children"),
    Output("data-version", "data"),
    Output("refresh-results", "disabled"),
    Input("apply-parameters", "n_clicks"),
    Input("product-selector", "value"),
    Input("refresh-results", "n_intervals"),
    State("rank-years", "value"),
    State("zscore-window", "value"),
    State("slope-days", "value"),
    State("column-selector", "value"),
    prevent_initial_call=True,
)
def apply_parameters(_, symbol, __, rank_years, zscore_window, slope_days, selected):
    if ctx.triggered_id == "refresh-results":
        with state_lock:
            return (dashboard_state["display"].to_dict("records"),
                    dashboard_state["title"], dashboard_state["version"],
                    not dashboard_state["processing"])
    if not symbol:
        return (display_df.to_dict("records"), "Select a product",
                dashboard_state["version"], True)
    selected = [column for column in columns if column in (selected or [])]
    blank = {name: frame.copy() for name, frame in frames.items()}
    with state_lock:
        job_id = dashboard_state["job_id"] + 1
        dashboard_state.update({
            "display": make_display(blank), "slopes": blank["slopes"],
            "zscores": blank["zscores"], "aman": blank["aman"], "results": {},
            "symbol": symbol, "version": dashboard_state["version"] + 1,
            "job_id": job_id, "processing": bool(selected),
            "title": f"{symbol} — processing 0/{len(selected)} categories",
        })
        current = (dashboard_state["display"].to_dict("records"),
                   dashboard_state["title"], dashboard_state["version"],
                   not dashboard_state["processing"])
    if selected:
        Thread(target=calculate_dashboard, args=(
            universe, selected, symbol, int(rank_years), int(zscore_window),
            int(slope_days), job_id,
        ), daemon=True).start()
    return current

@app.callback(
    Output("contracts-table", "columns"),
    Input("column-selector", "value"),
)
def select_columns(selected):
    selected = set(selected or [])
    visible = ["Contract", *[column for column in columns if column in selected]]
    return [{"name": column, "id": column, "presentation": "markdown"}
            for column in visible]

@app.callback(
    Output("contracts-table", "style_data_conditional"),
    Input("slope-threshold", "value"),
    Input("zscore-threshold", "value"),
    Input("highlight-selector", "value"),
    Input("data-version", "data"),
)
def highlight_cells(slope_threshold, zscore_threshold, selected, _):
    styles = [
        {"if": {"row_index": "even"}, "background": "#111827"},
        {"if": {"row_index": "odd"}, "background": "#151f30"},
    ]
    with state_lock:
        slopes = dashboard_state["slopes"].copy()
        zscores = dashboard_state["zscores"].copy()
        aman = dashboard_state["aman"].copy()
    selected = set(selected or [])
    for row in range(len(slopes)):
        for column in columns:
            colors = []
            slope, zscore, signal = (slopes.at[row, column], zscores.at[row, column],
                                     aman.at[row, column])
            if ("slope" in selected and slope_threshold is not None
                    and pd.notna(slope) and abs(slope) > abs(float(slope_threshold))):
                colors.append("#6b4f1d")
            if ("zscore" in selected and zscore_threshold is not None
                    and pd.notna(zscore) and abs(zscore) > abs(float(zscore_threshold))):
                colors.append("#164e63")
            if "aman" in selected and signal in {"LONG", "SHORT"}:
                colors.append("#14532d" if signal == "LONG" else "#7f1d1d")
            if colors:
                stops = 100 / len(colors)
                gradient = ", ".join(
                    f"{color} {index * stops:.0f}% {(index + 1) * stops:.0f}%"
                    for index, color in enumerate(colors)
                )
                styles.append({"if": {"row_index": row, "column_id": column},
                               "background": f"linear-gradient(135deg, {gradient})"})
    return styles

@app.callback(
    Output("seasonality-chart", "figure"),
    Output("chart-modal", "style"),
    Output("selected-cell", "data"),
    Input("contracts-table", "active_cell"),
    Input("close-modal", "n_clicks"),
    Input("modal-backdrop", "n_clicks"),
    Input("live-chart-refresh", "n_intervals"),
    State("selected-cell", "data"),
    prevent_initial_call=True,
)
def toggle_chart(cell, _, __, ___, selected_cell):
    hidden = {"display": "none"}
    if ctx.triggered_id in {"close-modal", "modal-backdrop"}:
        return go.Figure(), hidden, None
    if ctx.triggered_id == "live-chart-refresh":
        if not selected_cell:
            return no_update, no_update, no_update
        expression = selected_cell["expression"]
        symbol = selected_cell["symbol"]
        years = selected_cell["years"]
        db = MarketData()
        try:
            result = Seasonality(db).working_day_expression_seasonality(
                symbol, expression, min(years), max(years),
                selected_cell.get("window_days", 400),
            )
        finally:
            db.close()
        return plotter.build_seasonality_figure(
            result, expression, height=610
        ), no_update, no_update
    if not cell or cell["column_id"] == "Contract":
        return go.Figure(), hidden, None
    with state_lock:
        display = dashboard_state["display"].copy()
        results = dashboard_state["results"].copy()
        symbol = dashboard_state["symbol"]
    contract = display.iloc[cell["row"]]["Contract"]
    expression = universe["contracts"][contract][cell["column_id"]]
    result = results.get(expression)
    if result is None:
        return go.Figure(), hidden, None
    modal = {"display": "flex", "position": "fixed", "inset": "0", "zIndex": "1000",
             "backgroundColor": "rgba(0,0,0,0.75)", "alignItems": "center",
             "justifyContent": "center"}
    return (plotter.build_seasonality_figure(result, expression, height=610), modal,
            {"contract": contract, "strategy": cell["column_id"],
             "expression": expression, "symbol": symbol,
             "years": sorted(result.get("series", {})),
             "window_days": max(400, int(abs(result["combined"].index.min())))})

@app.callback(
    Output("rollover-chart", "figure"),
    Input("selected-cell", "data"),
    Input("live-chart-refresh", "n_intervals"),
)
def update_rollover(selection, _):
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
    Input("live-chart-refresh", "n_intervals"),
)
def update_forward_curve(selection, _):
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

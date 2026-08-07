import json
import numpy as np
import pandas as pd
import plotly.graph_objects as go
from dash import Dash, Input, Output, State, ctx, dcc, html, dash_table

from database.manager import DatabaseManager
from analysis.expression import parse_expression
from analysis.feature_creation import FeatureCreator
from analysis.plotting import SeasonalityPlotter
from analysis.seasonality import Seasonality

SYMBOL = "NG"
CONTRACTS_FILE = "contracts_list.json"
RANK_YEARS = 4
ZSCORE_WINDOW = 42
SLOPE_DAYS = 10
SLOPE_DROP_FRACTION = 0.20

def expression_metrics(sznlty, features, symbol, expression, rank_years,
                       zscore_window, slope_days, slope_drop_fraction):
    legs = parse_expression(expression)
    codes = sorted({code for _, code in legs})
    histories = sznlty.db.get_contract_history(symbol, codes)
    if not legs or any(code not in histories or histories[code].empty for code in codes):
        return None, None, None, None, None
    common = None
    for code in codes:
        dates = pd.DatetimeIndex(histories[code].index).normalize()
        common = dates if common is None else common.intersection(dates)
    if common is None or common.empty:
        return None, None, None, None, None
    anchor = legs[0][1]
    expiry = sznlty._official_or_last_date(symbol, anchor, histories[anchor])
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
        return None, None, None, None, None
    live = combined[current].dropna()
    day = live.index.max()
    rank_data = features.calculate_current_rank_ratio(combined, rank_years)
    if day not in rank_data.index or pd.isna(rank_data.at[day, "Rank"]):
        return None, None, None, None, None
    rank = f"{int(rank_data.at[day, 'Rank'])}/{int(rank_data.at[day, 'Count'])}"
    zscore = features.create_live_features(
        live, {"TECH_ZScore": {"window": zscore_window}}
    ).get("TECH_ZScore")
    slope = features.calculate_average_forward_slope(
        combined, slope_days, rank_years - 1, slope_drop_fraction
    ).get(day)
    return (float(live.iloc[-1]), rank,
            None if pd.isna(zscore) else float(zscore),
            None if pd.isna(slope) else float(slope), result)

def calculate_dashboard(universe, symbol, rank_years, zscore_window,
                        slope_days, slope_drop_fraction):
    columns = universe["columns"]
    records = {name: [] for name in ("values", "ranks", "zscores", "slopes")}
    results, db = {}, DatabaseManager()
    sznlty, features = Seasonality(db), FeatureCreator()
    try:
        for contract, strategies in universe["contracts"].items():
            rows = {name: {"Contract": contract} for name in records}
            for strategy in columns:
                expression = strategies[strategy]
                metrics = expression_metrics(
                    sznlty, features, symbol, expression, rank_years,
                    zscore_window, slope_days, slope_drop_fraction,
                )
                for name, value in zip(records, metrics[:4]):
                    rows[name][strategy] = value
                if metrics[4] is not None:
                    results[expression] = metrics[4]
            for name in records:
                records[name].append(rows[name])
    finally:
        db.close()
    return ({name: pd.DataFrame(rows, columns=["Contract", *columns])
             for name, rows in records.items()}, results)

def format_value(value):
    if value is None or pd.isna(value):
        return "-"

    # Display up to four decimals without unnecessary trailing zeroes.
    return f"{value:.4f}".rstrip("0").rstrip(".")

def zscore_color(zscore):
    if zscore is None or pd.isna(zscore):
        return "#64748b"
    zscore = max(-2.0, min(2.0, float(zscore)))
    start, end, ratio = (
        ((220, 38, 38), (250, 204, 21), (zscore + 2) / 2)
        if zscore < 0 else
        ((250, 204, 21), (22, 163, 74), zscore / 2)
    )
    rgb = tuple(round(a + (b - a) * ratio) for a, b in zip(start, end))
    return f"#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}"

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
    rgb = tuple(round(a + (b - a) * ratio) for a, b in zip(start, end))
    return f"#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}"

def format_cell(value, rank, zscore, slope):
    z_square = f"<span style='color:{zscore_color(zscore)};font-size:22px'>■</span>"
    if format_value(value) == "-" or rank is None or pd.isna(rank):
        rank_square = f"<span style='color:{rank_color(None)};font-size:22px'>■</span>"
        return f"-<br>{rank_square} -<br>{z_square} Z -<br>S -"
    rank_square = f"<span style='color:{rank_color(rank)};font-size:22px'>■</span>"
    zscore_text = "-" if pd.isna(zscore) else f"{zscore:.2f}"
    slope_text = "-" if slope is None or pd.isna(slope) else format_value(slope)
    return f"<b>{format_value(value)}</b><br>{rank_square} <b>{rank}</b><br>{z_square} <span style='font-size:14px'>Z {zscore_text}</span><br>S {slope_text}"

# Load the organized expression matrix.
with open(CONTRACTS_FILE, encoding="utf-8") as file:
    universe = json.load(file)

columns = universe["columns"]

def make_display(frames):
    values, ranks = frames["values"], frames["ranks"]
    zscores, slopes = frames["zscores"], frames["slopes"]
    display = values.copy()
    for column in columns:
        display[column] = [
            format_cell(value, rank, zscore, slope)
            for value, rank, zscore, slope in zip(
                values[column], ranks[column], zscores[column], slopes[column]
            )
        ]
    return display

product_db = DatabaseManager()
try:
    product_db.cursor.execute("SELECT DISTINCT symbol FROM seac_settlements ORDER BY symbol")
    products = [row[0] for row in product_db.cursor.fetchall()]
finally:
    product_db.close()

frames, seasonality_results = calculate_dashboard(
    universe, SYMBOL, RANK_YEARS, ZSCORE_WINDOW, SLOPE_DAYS, SLOPE_DROP_FRACTION
)
display_df = make_display(frames)
dashboard_state = {"display": display_df, "slopes": frames["slopes"],
                   "zscores": frames["zscores"], "results": seasonality_results,
                   "symbol": SYMBOL, "version": 0}

def control(label, component):
    return html.Label([html.Span(label), component], className="control-group")

def number_control(label, component_id, value, minimum, step):
    return control(label, dcc.Input(id=component_id, type="number", value=value,
                                    min=minimum, step=step, debounce=True,
                                    className="control-input"))

app = Dash(__name__)
plotter = SeasonalityPlotter()
app.layout = html.Div([
    html.H3(f"{SYMBOL} — Latest Contract Values", id="page-title"),
    html.Div([
        control("Product", dcc.Dropdown(
            id="product-selector",
            options=[{"label": product, "value": product} for product in products],
            value=SYMBOL if SYMBOL in products else products[0],
            clearable=False, className="product-dropdown")),
        number_control("Rank years", "rank-years", RANK_YEARS, 2, 1),
        number_control("Z-score window", "zscore-window", ZSCORE_WINDOW, 2, 1),
        number_control("Slope days", "slope-days", SLOPE_DAYS, 1, 1),
        number_control("|Slope| >", "slope-threshold", 0.05, None, 0.01),
        number_control("|Z-score| >", "zscore-threshold", 1.5, None, 0.1),
        html.Button("Apply", id="apply-parameters", n_clicks=0,
                    className="apply-button"),
    ], className="control-row"),
    html.Details([
        html.Summary("Columns"),
        dcc.Checklist(
            id="column-selector",
            options=[{"label": column, "value": column} for column in columns],
            value=columns,
            inline=True,
            inputStyle={"marginRight": "3px"},
            labelStyle={"marginRight": "10px"},
        ),
    ], className="columns-panel"),
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
                    "minWidth": "64px", "width": "64px", "maxWidth": "64px",
                    "height": "84px", "fontSize": "13px", "padding": "2px"},
        style_cell_conditional=[{"if": {"column_id": "Contract"},
                                 "minWidth": "58px", "width": "58px", "maxWidth": "58px"}],
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
            dcc.Graph(id="seasonality-chart"),
        ], style={"position": "relative", "zIndex": "1001",
                  "backgroundColor": "black", "width": "90%", "padding": "10px"}),
    ], id="chart-modal", style={"display": "none"}),
    dcc.Store(id="data-version", data=0),
], style={"backgroundColor": "#0b1220", "color": "#f8fafc",
          "minHeight": "100vh", "padding": "8px"})

@app.callback(
    Output("contracts-table", "data"),
    Output("page-title", "children"),
    Output("data-version", "data"),
    Input("apply-parameters", "n_clicks"),
    State("product-selector", "value"),
    State("rank-years", "value"),
    State("zscore-window", "value"),
    State("slope-days", "value"),
    prevent_initial_call=True,
)
def apply_parameters(_, symbol, rank_years, zscore_window, slope_days):
    frames, results = calculate_dashboard(
        universe, symbol, int(rank_years), int(zscore_window), int(slope_days),
        SLOPE_DROP_FRACTION,
    )
    display = make_display(frames)
    dashboard_state.update({"display": display, "slopes": frames["slopes"],
                            "zscores": frames["zscores"], "results": results,
                            "symbol": symbol,
                            "version": dashboard_state["version"] + 1})
    return display.to_dict("records"), f"{symbol} — Latest Contract Values", dashboard_state["version"]

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
    Input("data-version", "data"),
)
def highlight_cells(slope_threshold, zscore_threshold, _):
    styles = [{"if": {"row_index": "odd"}, "backgroundColor": "#151f30"}]
    slopes = dashboard_state["slopes"]
    zscores = dashboard_state["zscores"]
    for row in range(len(slopes)):
        for column in columns:
            if slope_threshold is not None:
                slope = slopes.at[row, column]
                if pd.notna(slope) and abs(slope) > abs(float(slope_threshold)):
                    styles.append({
                        "if": {"row_index": row, "column_id": column},
                        "backgroundColor": "#4a3412",
                    })
            if zscore_threshold is not None:
                zscore = zscores.at[row, column]
                if pd.notna(zscore) and abs(zscore) > abs(float(zscore_threshold)):
                    styles.append({
                        "if": {"row_index": row, "column_id": column},
                        "border": "3px solid #22d3ee",
                    })
    return styles

@app.callback(
    Output("seasonality-chart", "figure"),
    Output("chart-modal", "style"),
    Input("contracts-table", "active_cell"),
    Input("close-modal", "n_clicks"),
    Input("modal-backdrop", "n_clicks"),
    prevent_initial_call=True,
)
def toggle_chart(cell, _, __):
    hidden = {"display": "none"}
    if ctx.triggered_id in {"close-modal", "modal-backdrop"} or not cell or cell["column_id"] == "Contract":
        return go.Figure(), hidden
    display = dashboard_state["display"]
    contract = display.iloc[cell["row"]]["Contract"]
    expression = universe["contracts"][contract][cell["column_id"]]
    result = dashboard_state["results"].get(expression)
    if result is None:
        return go.Figure(), hidden
    modal = {"display": "flex", "position": "fixed", "inset": "0", "zIndex": "1000",
             "backgroundColor": "rgba(0,0,0,0.75)", "alignItems": "center",
             "justifyContent": "center"}
    return plotter.build_seasonality_figure(result, expression), modal

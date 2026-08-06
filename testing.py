import json
import re
import pandas as pd
import numpy as np
import plotly.graph_objects as go
import webbrowser
from threading import Timer
from dash import Dash, Input, Output, State, ctx, dcc, html, dash_table

from database.manager import DatabaseManager
from analysis.seasonality import Seasonality
from analysis.feature_creation import FeatureCreator


SYMBOL = "NG"
CONTRACTS_FILE = "contracts_list.json"
RANK_YEARS = 4
ZSCORE_WINDOW = 42
SLOPE_DAYS = 10
SLOPE_DROP_FRACTION = 0.20


def parse_expression(expression):
    """Return [(coefficient, contract_code), ...]."""
    compact = expression.replace(" ", "")
    terms = re.findall(
        r"([+-]?)(?:(\d+(?:\.\d+)?)\*)?([FGHJKMNQUVXZ]\d{2})",
        compact,
    )

    legs = []
    for sign, coefficient, contract_code in terms:
        multiplier = float(coefficient) if coefficient else 1.0
        if sign == "-":
            multiplier *= -1
        legs.append((multiplier, contract_code))

    return legs


def expression_metrics(sznlty, features, symbol, expression,
                       rank_years=RANK_YEARS, zscore_window=ZSCORE_WINDOW,
                       slope_days=SLOPE_DAYS,
                       slope_drop_fraction=SLOPE_DROP_FRACTION):
    match = re.search(r"[FGHJKMNQUVXZ](\d{2})", expression)
    if not match:
        return None, None, None, None, None

    legs = parse_expression(expression)
    contract_codes = sorted({code for _, code in legs})
    histories = sznlty.db.get_contract_history(symbol, contract_codes)
    if any(
        code not in histories or histories[code].empty
        for code in contract_codes
    ):
        return None, None, None, None, None

    common_dates = None
    for code in contract_codes:
        dates = pd.DatetimeIndex(histories[code].index).normalize()
        common_dates = dates if common_dates is None else common_dates.intersection(dates)
    if common_dates is None or common_dates.empty:
        return None, None, None, None, None

    anchor_code = legs[0][1]
    expiry = sznlty._official_or_last_date(
        symbol, anchor_code, histories[anchor_code]
    )
    latest_common_date = common_dates.max()
    required_window = max(
        0,
        int(np.busday_count(
            latest_common_date.to_datetime64().astype("datetime64[D]"),
            expiry.to_datetime64().astype("datetime64[D]"),
        )),
    )
    window_days = max(400, required_window + zscore_window + 10)

    current_year = 2000 + int(match.group(1))
    result = sznlty.working_day_expression_seasonality(
        symbol=symbol,
        expression=expression,
        start_year=current_year - rank_years + 1,
        end_year=current_year,
        window_days=window_days,
    )
    combined = result.get("combined", pd.DataFrame())
    current_column = next(
        (column for column in combined.columns if int(column) == current_year),
        None,
    )
    if current_column is None:
        return None, None, None, None, None

    current_series = combined[current_column].dropna()
    if current_series.empty:
        return None, None, None, None, None

    latest_day_to_expiry = current_series.index.max()
    comparable = combined.loc[latest_day_to_expiry].dropna()
    comparable = comparable[
        [
            column for column in comparable.index
            if current_year - rank_years + 1 <= int(column) <= current_year
        ]
    ]
    if comparable.empty or current_column not in comparable.index:
        return None, None, None, None, None

    rank = int(comparable.rank(ascending=False, method="min")[current_column])
    zscore = features.create_live_features(
        current_series, {"TECH_ZScore": {"window": zscore_window}}
    ).get("TECH_ZScore")
    zscore = None if pd.isna(zscore) else float(zscore)
    slope = features.calculate_average_forward_slope(
        combined, days=slope_days, years=rank_years - 1,
        drop_least_correlated=slope_drop_fraction
    ).get(latest_day_to_expiry)
    slope = None if pd.isna(slope) else float(slope)
    return float(current_series.iloc[-1]), f"{rank}/{len(comparable)}", zscore, slope, result


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


def seasonality_figure(result, title):
    figure = go.Figure()
    colors = ['#636EFA', '#EF553B', '#00CC96', '#AB63FA', '#FFA15A',
              '#19D3F3', '#FF6692', '#B6E880', '#FF97FF', '#FECB52']
    series = result.get("series", {})
    current_year = max(series) if series else None
    color_index = 0
    for year, frame in sorted(series.items()):
        current = year == current_year
        figure.add_trace(go.Scatter(
            x=frame.index,
            y=frame["value"],
            name=str(year),
            mode="lines",
            line=dict(width=2 if current else 1,
                      color="white" if current else colors[color_index % len(colors)]),
            customdata=frame["date"].dt.strftime("%Y-%m-%d"),
            hovertemplate=f"{year}<br>Date: %{{customdata}}<br>Value: %{{y:.2f}}<extra></extra>",
        ))
        if not current:
            color_index += 1
    tickvals, ticktext = result.get("ticks", ([], []))
    figure.update_layout(
        template="plotly_dark",
        paper_bgcolor="black",
        plot_bgcolor="black",
        title=title,
        xaxis=dict(title=result.get("xaxis_title", "Working days to expiry"),
                   tickvals=tickvals, ticktext=ticktext, gridcolor="#333"),
        yaxis=dict(title="Value", gridcolor="#333"),
        hovermode="x unified",
        autosize=True,
        height=900,
    )
    return figure


# Load the organized expression matrix.
with open(CONTRACTS_FILE, encoding="utf-8") as file:
    universe = json.load(file)

columns = universe["columns"]


def calculate_dashboard(symbol, rank_years, zscore_window, slope_days):
    rows, rank_rows, zscore_rows, slope_rows = [], [], [], []
    results = {}
    db = DatabaseManager()
    sznlty = Seasonality(db)
    features = FeatureCreator()
    try:
        for contract, strategies in universe["contracts"].items():
            row, rank_row = {"Contract": contract}, {"Contract": contract}
            zscore_row, slope_row = {"Contract": contract}, {"Contract": contract}
            for strategy in columns:
                expression = strategies[strategy]
                value, rank, zscore, slope, result = expression_metrics(
                    sznlty, features, symbol, expression,
                    rank_years=rank_years, zscore_window=zscore_window,
                    slope_days=slope_days,
                )
                row[strategy], rank_row[strategy] = value, rank
                zscore_row[strategy], slope_row[strategy] = zscore, slope
                if result is not None:
                    results[expression] = result
            rows.append(row)
            rank_rows.append(rank_row)
            zscore_rows.append(zscore_row)
            slope_rows.append(slope_row)
    finally:
        db.close()
    values = pd.DataFrame(rows, columns=["Contract", *columns])
    ranks = pd.DataFrame(rank_rows, columns=["Contract", *columns])
    zscores = pd.DataFrame(zscore_rows, columns=["Contract", *columns])
    slopes = pd.DataFrame(slope_rows, columns=["Contract", *columns])
    display = values.copy()
    for column in columns:
        display[column] = [
            format_cell(value, rank, zscore, slope)
            for value, rank, zscore, slope in zip(
                values[column], ranks[column], zscores[column], slopes[column]
            )
        ]
    return display, slopes, results


product_db = DatabaseManager()
try:
    product_db.cursor.execute("SELECT DISTINCT symbol FROM seac_settlements ORDER BY symbol")
    products = [row[0] for row in product_db.cursor.fetchall()]
finally:
    product_db.close()

display_df, slope_df, seasonality_results = calculate_dashboard(
    SYMBOL, RANK_YEARS, ZSCORE_WINDOW, SLOPE_DAYS
)
dashboard_state = {"display": display_df, "slopes": slope_df,
                   "results": seasonality_results, "symbol": SYMBOL, "version": 0}

app = Dash(__name__)
app.layout = html.Div([
    html.H3(f"{SYMBOL} — Latest Contract Values", id="page-title"),
    html.Div([
        html.Label("Product"),
        dcc.Dropdown(id="product-selector",
                     options=[{"label": product, "value": product} for product in products],
                     value=SYMBOL if SYMBOL in products else products[0],
                     clearable=False, style={"width": "110px", "color": "black"}),
        html.Label("Rank years"),
        dcc.Input(id="rank-years", type="number", value=RANK_YEARS, min=2, step=1),
        html.Label("Z-score window"),
        dcc.Input(id="zscore-window", type="number", value=ZSCORE_WINDOW, min=2, step=1),
        html.Label("Slope days"),
        dcc.Input(id="slope-days", type="number", value=SLOPE_DAYS, min=1, step=1),
        html.Button("Apply", id="apply-parameters", n_clicks=0),
    ], style={"display": "flex", "alignItems": "center", "gap": "8px", "color": "#6C6C6D", "marginBottom": "10px",
              "marginBottom": "10px"}),
    dcc.Checklist(
        id="column-selector",
        options=[{"label": column, "value": column} for column in columns],
        value=columns,
        inline=True,
        style={"marginBottom": "10px"},
        inputStyle={"marginLeft": "10px", "marginRight": "4px"},
        labelStyle={"color": "#f8fafc", "display": "inline-block",
                    "marginRight": "12px"},
    ),
    html.Div([
        html.Label("Highlight |slope| above: ", style={"marginRight": "8px"}),
        dcc.Input(id="slope-threshold", type="number", value=0.05,
                  step=0.01, debounce=True,
                  style={"width": "90px", "marginBottom": "10px" ,"color": "#f8fafc", "backgroundColor": "#1f2937",}),
    ]),
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
    display, slopes, results = calculate_dashboard(
        symbol, int(rank_years), int(zscore_window), int(slope_days)
    )
    dashboard_state.update({"display": display, "slopes": slopes,
                            "results": results, "symbol": symbol,
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
    Input("data-version", "data"),
)
def highlight_slopes(threshold, _):
    styles = [{"if": {"row_index": "odd"}, "backgroundColor": "#151f30"}]
    if threshold is None:
        return styles
    slopes = dashboard_state["slopes"]
    for row in range(len(slopes)):
        for column in columns:
            slope = slopes.at[row, column]
            if pd.notna(slope) and abs(slope) > abs(float(threshold)):
                styles.append({
                    "if": {"row_index": row, "column_id": column},
                    "backgroundColor": "#5b21b6",
                    "border": "2px solid #facc15",
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
    return seasonality_figure(result, expression), modal


if __name__ == "__main__":
    Timer(1, lambda: webbrowser.open("http://127.0.0.1:8050")).start()
    app.run(host="0.0.0.0", port=8050, debug=False)

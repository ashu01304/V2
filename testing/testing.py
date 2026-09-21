"""Dash interface for interactive range testing."""

import plotly.graph_objects as go
from dash import Dash, Input, Output, State, dash_table, dcc, html

from product_config import PRODUCT_CONFIG
from testing.config import *
from testing.engine import (
    DEFAULT_ROW, DEFAULT_ROWS, DEFAULT_STRATEGY, EXPRESSION_ROWS,
    accuracy, calculate_expression, visible_expression_rows,
)
from testing.charts import make_figure
from testing.universe import STRATEGIES

app = Dash(__name__)
app.title = "Range Testing"
INPUT_STYLE = {
    "width": "110px", "backgroundColor": "white", "color": "black",
    "border": "1px solid #94a3b8", "borderRadius": "4px",
    "padding": "6px 8px", "fontWeight": "600",
}


def number_control(label, control_id, value, minimum=1, step=1):
    return html.Label([
        html.Span(label, style={"display": "block", "marginBottom": "5px"}),
        dcc.Input(id=control_id, type="number", min=minimum, step=step,
                  value=value, debounce=True, style=INPUT_STYLE,
                  persistence=True, persistence_type="local"),
    ])


def dropdown_control(label, control_id, options, value, width="145px"):
    return html.Label([
        html.Span(label, style={"display": "block", "marginBottom": "5px"}),
        dcc.Dropdown(id=control_id, options=options, value=value, clearable=False,
                     persistence=True, persistence_type="local",
                     style={"width": width, "color": "black"}),
    ])


def result_table(table_id, columns, width, **kwargs):
    return dash_table.DataTable(
        id=table_id,
        columns=[{"name": column, "id": column} for column in columns],
        style_table={"width": width, "maxWidth": "calc(100vw - 40px)",
                     "overflowX": "auto", "margin": "0 auto 30px"},
        style_header={"backgroundColor": "#1e293b", "color": "white",
                      "fontWeight": "700"},
        style_cell={"backgroundColor": "#0f172a", "color": "white",
                    "border": "1px solid #334155", "padding": "7px",
                    "textAlign": "center"},
        **kwargs,
    )


CONTROLS = (
    ("Peak lookback", "peak-lookback", DEFAULT_PEAK_LOOKBACK, 1),
    ("Peak rank", "peak-rank", DEFAULT_PEAK_RANK, 1),
    ("Valley lookback", "valley-lookback", DEFAULT_VALLEY_LOOKBACK, 1),
    ("Valley rank", "valley-rank", DEFAULT_VALLEY_RANK, 1),
    ("Max parallel positions", "max-parallel-positions", DEFAULT_MAX_PARALLEL_POSITIONS, 1),
    ("Max consecutive one direction", "max-consecutive-direction", DEFAULT_MAX_CONSECUTIVE_DIRECTION, 1),
    ("No trade days before expiry", "no-trade-days", DEFAULT_NO_TRADE_DAYS, 0),
    ("Maximum days before expiry", "max-trade-days", DEFAULT_MAX_TRADE_DAYS, 1),
    ("Swing lookback", "swing-lookback", SWING_DIFFERENCE_LOOKBACK, 1),
    ("Largest swings", "swing-top-count", SWING_DIFFERENCE_TOP_COUNT, 1),
    ("Minimum swing ticks", "minimum-swing-ticks",
     next(iter(PRODUCT_CONFIG[DEFAULT_SYMBOL]["buckets"].values()))[
         "minimum_swing_ticks"
     ][0], 1),
    ("Historical years", "history-years", DEFAULT_HISTORY_YEARS, 1),
)


app.layout = html.Div([
    html.Div([
        html.Strong("Range settings", style={"alignSelf": "center", "color": "#f97316"}),
        dropdown_control("Symbol", "symbol", TEST_SYMBOLS, DEFAULT_SYMBOL, "110px"),
        dropdown_control("Calculation", "mode", [
            {"label": "Peaks / valleys", "value": "extrema"},
            {"label": "All trailing days", "value": "days"},
        ], DEFAULT_MODE, "175px"),
        *[number_control(label, control_id, value, minimum)
          for label, control_id, value, minimum in CONTROLS],
    ], style={
        "display": "grid",
        "gridTemplateColumns": "repeat(auto-fit, minmax(145px, 1fr))",
        "alignItems": "end", "gap": "16px", "padding": "18px 24px",
        "background": "#111827", "color": "white",
        "borderBottom": "1px solid #334155",
    }),
    html.Div([
        html.Strong("Expression columns", style={"color": "#f97316"}),
        dcc.Checklist(
            id="column-selector",
            options=[{"label": strategy, "value": strategy} for strategy in STRATEGIES],
            value=STRATEGIES,
            persistence=True,
            persistence_type="local",
            inline=True,
            labelStyle={"marginRight": "18px"},
        ),
    ], style={"padding": "12px 20px", "background": "#111827", "color": "white"}),
    html.Div(id="selected-expression", style={
        "padding": "12px 20px", "background": "#0f172a", "color": "white",
        "fontSize": "18px", "fontWeight": "600",
    }),
    dash_table.DataTable(
        id="expression-table",
        columns=[{"name": "Contract", "id": "Contract"}]
                + [{"name": strategy, "id": strategy} for strategy in STRATEGIES],
        data=DEFAULT_ROWS,
        active_cell={"row": DEFAULT_ROW, "column": STRATEGIES.index(DEFAULT_STRATEGY) + 1,
                     "column_id": DEFAULT_STRATEGY, "row_id": None},
        fixed_rows={"headers": True},
        style_table={"width": "fit-content", "maxWidth": "100%",
                     "margin": "0", "overflowX": "auto", "maxHeight": "420px",
                     "border": "1px solid #334155"},
        style_header={"backgroundColor": "#1e293b", "color": "white",
                      "fontWeight": "700"},
        style_cell={"backgroundColor": "#0f172a", "color": "white",
                    "border": "1px solid #334155", "padding": "7px",
                    "width": "160px", "minWidth": "160px", "maxWidth": "160px",
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
            {"if": {"column_id": "Contract"}, "width": "90px",
             "minWidth": "90px", "maxWidth": "90px"},
            {"if": {"state": "active"}, "backgroundColor": "#1d4ed8",
             "border": "2px solid #93c5fd"},
        ],
    ),
    html.Div(id="accuracy-summary", style={
        "display": "flex", "gap": "30px", "padding": "12px 20px",
        "background": "#0f172a", "color": "white", "fontSize": "18px",
    }),
    html.Div(id="parameter-error", style={"color": "#f87171", "padding": "0 20px"}),
    dcc.Loading(dcc.Graph(id="seasonality-chart"), type="circle"),
    html.H3("Trade analysis", style={"color": "white", "textAlign": "center"}),
    result_table("trade-table", (
        "Direction", "Entry price", "Exit price", "Outcome", "TP ticks",
        "SL ticks", "Holding days", "PnL ticks",
    ), "fit-content", page_size=25, sort_action="native", filter_action="native",
        style_data_conditional=[
            {"if": {"filter_query": "{PnL ticks} > 0", "column_id": "PnL ticks"},
             "color": "#4ade80"},
            {"if": {"filter_query": "{PnL ticks} < 0", "column_id": "PnL ticks"},
             "color": "#f87171"},
        ]),
    html.H3("Year-wise success rate", style={"color": "white", "textAlign": "center"}),
    result_table("yearly-table",
                 ("Year", "Wins", "Resolved", "Unresolved", "Win rate"), "650px"),
], style={"background": "black", "minHeight": "100vh"})


@app.callback(
    Output("expression-table", "columns"),
    Input("column-selector", "value"),
)
def select_expression_columns(selected):
    selected = selected or []
    return ([{"name": "Contract", "id": "Contract"}]
            + [{"name": strategy, "id": strategy}
               for strategy in STRATEGIES if strategy in selected])


@app.callback(
    Output("expression-table", "active_cell"),
    Input("symbol", "value"),
)
def select_symbol(symbol):
    rows = EXPRESSION_ROWS.get(symbol, [])
    if not rows:
        return None
    strategy = next((name for name in STRATEGIES if rows[0].get(name)), None)
    active = ({"row": 0, "column": STRATEGIES.index(strategy) + 1,
               "column_id": strategy, "row_id": None} if strategy else None)
    return active


@app.callback(
    Output("seasonality-chart", "figure"),
    Output("accuracy-summary", "children"),
    Output("parameter-error", "children"),
    Output("selected-expression", "children"),
    Output("trade-table", "data"),
    Output("yearly-table", "data"),
    Output("expression-table", "data"),
    Input("symbol", "value"),
    Input("expression-table", "active_cell"),
    Input("mode", "value"),
    Input("peak-lookback", "value"), Input("peak-rank", "value"),
    Input("valley-lookback", "value"), Input("valley-rank", "value"),
    Input("max-parallel-positions", "value"),
    Input("max-consecutive-direction", "value"),
    Input("no-trade-days", "value"),
    Input("max-trade-days", "value"),
    Input("swing-lookback", "value"),
    Input("swing-top-count", "value"),
    Input("minimum-swing-ticks", "value"),
    Input("history-years", "value"),
    Input("column-selector", "value"),
    State("expression-table", "data"),
)
def update_expression(symbol, active_cell, mode, peak_lookback, peak_rank,
                      valley_lookback, valley_rank, max_parallel_positions,
                      max_consecutive_direction, no_trade_days, max_trade_days,
                      swing_lookback, swing_top_count, minimum_swing_ticks,
                      history_years, selected_strategies, existing_table_rows):
    try:
        if not active_cell or active_cell.get("column_id") == "Contract":
            raise ValueError("Click a strategy expression cell in the table")
        row = int(active_cell["row"])
        rows_for_symbol = visible_expression_rows(symbol, selected_strategies)
        contract = rows_for_symbol[row]["Contract"]
        strategy = active_cell["column_id"]
        if not rows_for_symbol[row].get(strategy):
            raise ValueError("This expression has unavailable contract legs")
        result = calculate_expression(
            symbol, contract, strategy, mode, peak_lookback, peak_rank,
            valley_lookback, valley_rank, max_parallel_positions,
            max_consecutive_direction, no_trade_days, max_trade_days, swing_lookback,
            swing_top_count, minimum_swing_ticks, history_years,
        )
        current = result["current_outcomes"]
        historical = result["historical_outcomes"]
        rows = result["trade_rows"]
        total_pnl = sum(row["PnL ticks"] or 0 for row in rows)
        total_holding_days = sum(row["Holding days"] or 0 for row in rows)
        summary = [
            html.Span(
                f"Current: {current.count('TP')}/{len(current)} wins "
                f"({accuracy(current):.1%}), unresolved: {result['current_open']}"
            ),
            html.Span(
                f"Historical: {historical.count('TP')}/{len(historical)} wins "
                f"({accuracy(historical):.1%}), unresolved: {result['historical_open']} | "
                f"PnL: {result['historical_pnl']:+d} ticks"
            ),
            html.Span(f"Total realized PnL: {total_pnl:+d} ticks"),
            html.Span(f"Total holding days: {total_holding_days}"),
        ]
        selected = f"Selected: {symbol} / {contract} / {strategy} / {result['expression']}"
        source_rows = rows_for_symbol
        expected_contracts = [item["Contract"] for item in source_rows]
        existing_contracts = [item.get("Contract") for item in (existing_table_rows or [])]
        table_rows = ([dict(item) for item in existing_table_rows]
                      if existing_contracts == expected_contracts
                      else [dict(item) for item in source_rows])
        table_rows[row][strategy] = (
            f"{accuracy(historical):.1%} | {len(historical)} trades"
            if historical else "No historical trades"
        )
        return (make_figure(result), summary, "", selected, rows,
                result["yearly_results"], table_rows)
    except Exception as error:
        return (go.Figure(), [], str(error), "No expression selected", [], [],
                visible_expression_rows(symbol, selected_strategies))


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8051, debug=False)

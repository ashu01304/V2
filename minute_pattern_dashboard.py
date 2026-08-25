import os
from threading import Lock, Thread

import plotly.express as px
from dash import Dash, Input, Output, State, ctx, dash_table, dcc, html

from analysis.minute_patterns import available_minute_products, scan_minute_suggestions


products = available_minute_products()
PRODUCT_LABELS = {"LCO": "CO"}
product_options = [
    {"label": PRODUCT_LABELS.get(product, product), "value": product}
    for product in products
]
app = Dash(__name__)
app.title = "Intraday Hidden Patterns"
state_lock = Lock()
scan_state = {"job": 0, "processing": False, "status": "Ready.",
              "figure": {}, "data": [], "columns": []}


def control(label, component):
    return html.Label([html.Span(label), component])


app.layout = html.Div([
    html.H2("Minute-Data Hidden Pattern Discovery"),
    html.P("Builds candles from stored minute spreads and scans related structures."),
    html.Div([
        control("Product", dcc.Dropdown(
            product_options, "CL" if "CL" in products else products[0],
            id="minute-product", clearable=False,
        )),
        control("Candle interval", dcc.Dropdown(
            [{"label": "30 minutes", "value": "30min"},
             {"label": "1 hour", "value": "60min"}],
            "30min", id="minute-interval", clearable=False,
        )),
        control("History days", dcc.Input(
            id="minute-history", type="number", value=150, min=30, max=365,
        )),
        control("Prediction horizon (bars)", dcc.Input(
            id="minute-horizon", type="number", value=6, min=1, max=24,
        )),
        html.Button("Analyse intraday patterns", id="minute-run", n_clicks=0),
    ], className="minute-controls"),
    html.Div("Choose parameters and start analysis.", id="minute-status"),
    dcc.Graph(id="minute-chart", style={"height": "500px"}),
    dash_table.DataTable(
        id="minute-table", page_size=25, sort_action="native",
        style_table={"overflowX": "auto"},
        style_cell={"color": "#111827", "backgroundColor": "white",
                    "padding": "6px", "textAlign": "right"},
        style_header={"color": "#111827", "backgroundColor": "#e5e7eb",
                      "fontWeight": "bold"},
    ),
    dcc.Interval(id="minute-refresh", interval=1000, n_intervals=0, disabled=True),
], className="minute-pattern-page")


@app.callback(
    Output("minute-status", "children"), Output("minute-chart", "figure"),
    Output("minute-table", "data"), Output("minute-table", "columns"),
    Output("minute-refresh", "disabled"),
    Input("minute-run", "n_clicks"), Input("minute-refresh", "n_intervals"),
    State("minute-product", "value"), State("minute-interval", "value"),
    State("minute-history", "value"), State("minute-horizon", "value"),
    prevent_initial_call=True,
)
def run_scan(_, __, product, interval, history, horizon):
    if ctx.triggered_id == "minute-refresh":
        with state_lock:
            return (scan_state["status"], scan_state["figure"], scan_state["data"],
                    scan_state["columns"], not scan_state["processing"])
    with state_lock:
        job = scan_state["job"] + 1
        scan_state.update(job=job, processing=True, status="Starting workers...",
                          figure={}, data=[], columns=[])
    Thread(target=_worker, args=(job, product, interval, int(history), int(horizon)),
           daemon=True).start()
    return "Starting workers...", {}, [], [], False


def _worker(job, product, interval, history, horizon):
    def progress(done, total, contract, partial):
        display = _display(partial, product, interval, horizon,
                           f"Processed {done}/{total} contract families: {contract}.")
        with state_lock:
            if job == scan_state["job"]:
                scan_state.update(status=display[0], figure=display[1],
                                  data=display[2], columns=display[3])
    try:
        summary = scan_minute_suggestions(
            product, interval, history, horizon,
            workers=min(8, os.cpu_count() or 1), progress=progress,
        )
        display = _display(summary, product, interval, horizon, "Scan complete.")
    except Exception as error:
        display = f"Intraday analysis failed: {error}", {}, [], []
    with state_lock:
        if job == scan_state["job"]:
            scan_state.update(processing=False, status=display[0], figure=display[1],
                              data=display[2], columns=display[3])


def _display(summary, product, interval, horizon, prefix):
    if summary.empty:
        return f"{prefix} No suggestions yet.", {}, [], []
    summary = summary.copy()
    numeric = ["Current_Value", "ML_Expected_Move", "Historical_Move",
               "Probability", "Pattern_Probability", "Ensemble_Accuracy", "Score",
               "Support", "Resistance", "Available_Room"]
    for column in numeric:
        summary[column] = summary[column].round(3)
    trades = summary[summary["Quality"] == "TRADE"]
    plotted = trades if not trades.empty else summary.head(20)
    display_product = PRODUCT_LABELS.get(product, product)
    figure = px.bar(
        plotted, x="Expression", y="Probability", color="Side",
        title=f"Best {display_product} {interval} predictions for the next {horizon} bars",
        color_discrete_map={"LONG": "#16a34a", "SHORT": "#dc2626"},
    )
    columns = ["Quality", "Side", "Expression", "Current_Value",
               "ML_Expected_Move", "Probability", "Model_Votes",
               "Ensemble_Accuracy", "Pattern_Probability", "Similar_Cases",
               "Months_Seen", "Support", "Resistance", "Available_Room",
               "Level_Allows_Trade", "Interval", "Horizon_Bars", "Score"]
    columns = [column for column in columns if column in summary]
    table = trades[columns] if not trades.empty else summary[columns]
    status = (f"{prefix} Showing {len(trades)} trades and "
              f"{len(summary) - len(trades)} watch patterns.")
    return status, figure, table.to_dict("records"), [
        {"name": column.replace("_", " "), "id": column} for column in columns
    ]


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8053, debug=False)

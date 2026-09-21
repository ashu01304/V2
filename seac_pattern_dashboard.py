import plotly.express as px
import os
from threading import Lock, Thread

from dash import Dash, Input, Output, State, ctx, dash_table, dcc, html

from analysis.seac_patterns import available_products, load_universe, scan_trade_suggestions


universe = load_universe()
products = available_products()
app = Dash(__name__)
app.title = "CL Patterns"
scan_lock = Lock()
scan_state = {"job": 0, "processing": False, "status": "Ready.",
              "figure": {}, "data": [], "columns": []}


def control(label, component):
    return html.Label([
        html.Span(label, style={"display": "block", "marginBottom": "4px",
                               "color": "#f8fafc"}),
        component,
    ])


app.layout = html.Div([
    html.H2("SEAC Hidden Pattern Discovery", style={"color": "#f8fafc"}),
    html.P("The model scans every spread and fly, finds the current hidden market state, "
           "and ranks trades by what followed similar historical states.",
           style={"color": "#cbd5e1"}),
    html.Div([
        control("Product", dcc.Dropdown(
            products, "CL" if "CL" in products else products[0],
            id="pattern-product", clearable=False,
        )),
        control("History (years)", dcc.Input(
            id="pattern-years", type="number", value=12, min=4, max=20,
            style={"width": "100%", "color": "#111827", "backgroundColor": "white"}
        )),
        control("Prediction horizon (days)", dcc.Input(
            id="pattern-horizon", type="number", value=10, min=1, max=15,
            style={"width": "100%", "color": "#111827", "backgroundColor": "white"}
        )),
        control("Stop entries before expiry (days)", dcc.Input(
            id="stop-before-expiry", type="number", value=42, min=0, max=200,
            style={"width": "100%", "color": "#111827", "backgroundColor": "white"}
        )),
        control("Maximum rollover influence (%)", dcc.Input(
            id="rollover-weight", type="number", value=30, min=0, max=60,
            style={"width": "100%", "color": "#111827", "backgroundColor": "white"}
        )),
        html.Button("Analyse and suggest trades", id="discover-patterns", n_clicks=0),
    ], style={"display": "grid", "gridTemplateColumns": "1fr 1fr 1fr 1fr 1fr 1.5fr",
              "gap": "8px"}),
    dcc.Loading(html.Div(
        "Choose years and horizon, then start the analysis.", id="pattern-status",
        style={"padding": "12px 0", "color": "#e2e8f0"},
    )),
    dcc.Loading(dcc.Graph(id="pattern-map", style={"height": "500px"})),
    dcc.Loading(dash_table.DataTable(
        id="pattern-summary", sort_action="native", page_size=25,
        fixed_rows={"headers": True},
        style_table={"overflowX": "auto", "maxHeight": "620px",
                     "border": "1px solid #475569"},
        style_cell={"padding": "6px", "textAlign": "right",
                    "color": "#111827", "backgroundColor": "#ffffff"},
        style_header={"fontWeight": "bold", "color": "#111827",
                      "backgroundColor": "#e5e7eb"},
    )),
    dcc.Interval(id="pattern-refresh", interval=1000, n_intervals=0, disabled=True),
], className="seac-pattern-page",
   style={"width": "98vw", "maxWidth": "none", "margin": "auto", "padding": "16px",
          "color": "#f8fafc"})


@app.callback(
    Output("pattern-status", "children"),
    Output("pattern-map", "figure"),
    Output("pattern-summary", "data"),
    Output("pattern-summary", "columns"),
    Output("pattern-refresh", "disabled"),
    Input("discover-patterns", "n_clicks"),
    Input("pattern-refresh", "n_intervals"),
    State("pattern-product", "value"),
    State("pattern-years", "value"),
    State("pattern-horizon", "value"),
    State("stop-before-expiry", "value"),
    State("rollover-weight", "value"),
    prevent_initial_call=True,
)
def run_discovery(_, __, product, years, horizon, stop_before_expiry,
                  rollover_weight):
    if ctx.triggered_id == "pattern-refresh":
        with scan_lock:
            return (scan_state["status"], scan_state["figure"], scan_state["data"],
                    scan_state["columns"], not scan_state["processing"])
    with scan_lock:
        job = scan_state["job"] + 1
        workers = min(8, os.cpu_count() or 1)
        scan_state.update({"job": job, "processing": True,
                           "status": f"Starting {workers} parallel workers...",
                           "figure": {}, "data": [], "columns": []})
    Thread(target=_scan_worker, args=(job, product, int(years), int(horizon),
                                     int(stop_before_expiry or 0),
                                     float(rollover_weight or 0) / 100, workers),
           daemon=True).start()
    return f"Starting {workers} parallel workers...", {}, [], [], False


def _scan_worker(job, product, years, horizon, stop_before_expiry,
                 rollover_weight, workers):
    def progress(done, total, contract, partial):
        expressions_per_family = len(universe["columns"])
        status, figure, data, columns = _display(
            partial, product, horizon,
            f"Analysed {done * expressions_per_family}/"
            f"{total * expressions_per_family} expressions "
            f"({done}/{total} contract families); latest: {contract}."
        )
        with scan_lock:
            if job == scan_state["job"]:
                scan_state.update(status=status, figure=figure, data=data, columns=columns)
    try:
        summary = scan_trade_suggestions(
            product=product, years=years, horizon=horizon,
            workers=workers, progress=progress,
            stop_before_expiry=stop_before_expiry,
            max_rollover_weight=rollover_weight,
        )
        status, figure, data, columns = _display(
            summary, product, horizon, f"{product} scan complete."
        )
    except Exception as error:
        status, figure, data, columns = f"Pattern discovery failed: {error}", {}, [], []
    with scan_lock:
        if job == scan_state["job"]:
            scan_state.update(processing=False, status=status, figure=figure,
                              data=data, columns=columns)


def _display(summary, product, horizon, prefix):
    if summary.empty:
        return f"{prefix} No suggestions available yet.", {}, [], []
    summary = summary.copy()
    summary = summary.drop(columns=["_Backtest_Details"], errors="ignore")
    for column in ("Current_Value", "Expected_Move", "ML_Expected_Move",
                   "Rollover_Expected_Move"):
        summary[column] = summary[column].round(4)
    for column in ("Probability", "Pattern_Probability", "LightGBM_Probability",
                   "ExtraTrees_Probability", "Logistic_Probability",
                   "LightGBM_Accuracy", "ExtraTrees_Accuracy",
                   "Logistic_Accuracy", "HDBSCAN_Accuracy",
                   "Ensemble_Accuracy", "Score"):
        summary[column] = summary[column].round(1)
    summary["Rollover_Probability"] = summary["Rollover_Probability"].round(1)
    summary["Rollover_Weight"] = summary["Rollover_Weight"].round(1)
    trades = summary[summary["Quality"] == "TRADE"]
    plotted = trades.head(25) if not trades.empty else summary.head(25)
    figure = px.bar(
        plotted, x="Expression", y="Probability", color="Side",
        hover_data=["ML_Expected_Move", "Expected_Move", "Pattern_Probability",
                    "Rollover_Probability", "Rollover_Weight", "Model_Votes",
                    "Similar_Cases", "Years_Seen", "Pattern"],
        title=f"Best {product} suggestions for the next {horizon} working days",
        color_discrete_map={"LONG": "#16a34a", "SHORT": "#dc2626"},
    )
    figure.update_layout(margin={"l": 40, "r": 20, "t": 55, "b": 130})
    status = (f"{prefix} Showing {len(trades)} trade candidates and "
              f"{len(summary) - len(trades)} watch-list patterns. "
              f"Walk-forward accuracy: Ensemble {summary['Ensemble_Accuracy'].mean():.1f}%, "
              f"LightGBM {summary['LightGBM_Accuracy'].mean():.1f}%, "
              f"Extra Trees {summary['ExtraTrees_Accuracy'].mean():.1f}%, "
              f"Logistic {summary['Logistic_Accuracy'].mean():.1f}%, "
              f"HDBSCAN {summary['HDBSCAN_Accuracy'].mean():.1f}%.")
    table_columns = [
        "Quality", "Side", "Contract", "Structure", "Expression",
        "Current_Value", "ML_Expected_Move", "Probability", "Model_Votes",
        "Ensemble_Accuracy", "Pattern_Probability", "Rollover_Probability",
        "Rollover_Weight", "Similar_Cases", "Years_Seen", "Days_To_Expiry",
        "Score",
    ]
    table_columns = [column for column in table_columns if column in summary.columns]
    table = trades[table_columns].copy() if not trades.empty else summary[table_columns].copy()
    columns = [{"name": column.replace("_", " "), "id": column}
               for column in table.columns]
    return status, figure, table.to_dict("records"), columns


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8052, debug=False)

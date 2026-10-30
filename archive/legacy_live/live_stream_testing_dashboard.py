"""Read-only monitor of all configured directly quoted contracts. Default port: 8058."""

import argparse
import atexit
from collections import Counter

from dash import Dash, Input, Output, State, ctx, no_update, dash_table, dcc, html

from market_data.live_monitor import LiveContractMonitor, KINDS, active_contracts

monitor = LiveContractMonitor()
atexit.register(monitor.stop)
app = Dash(__name__, title="Live contract monitor", assets_folder="live_stream_testing_assets")

COLUMNS = [
    {"name": "Product", "id": "product"}, {"name": "Type", "id": "kind"},
    {"name": "Contract", "id": "contract"},
    *[{"name": label, "id": key, "type": "numeric"} for label, key in (
        ("LTP", "LastPrice"), ("Bid", "BidPrice"), ("Ask", "AskPrice"),
        ("Bid size", "BidSize"), ("Ask size", "AskSize"), ("Last size", "LastSize"),
        ("Admin price", "AdminPrice"), ("Volume", "TotalTradedVolume"), ("WAP", "WAP"),
        ("Prev settlement", "PreviousSettlement"), ("Current settlement", "CurrentSettlement"),
        ("Live updates", "live_updates"), ("Age (s)", "age_seconds"))],
    {"name": "Name", "id": "name"}, {"name": "Last received (UTC)", "id": "received_at_utc"},
    {"name": "Feed timestamp", "id": "feed_timestamp"}, {"name": "TT item", "id": "item"},
]

app.layout = html.Main([
    html.Header([html.H3("Live contract monitor"), html.Span(id="monitor-status")], className="monitor-header"),
    html.P("All CL, CO/BRN, G, HO, NG and RB outrights, spreads and flies, plus CL box spreads. "
           "Only contracts receiving fresh post-snapshot updates with prices are shown. "
           "LTP is LastPrice; unavailable fields stay empty.", className="monitor-description"),
    html.Div([
        dcc.Dropdown([{"label": value, "value": value} for value in ("CL", "CO", "G", "HO", "NG", "RB", "+1xCL")],
            [], multi=True, id="monitor-products", placeholder="All products", className="monitor-small-filter"),
        dcc.Dropdown([{"label": label, "value": key} for key, label in KINDS.items()], [], multi=True,
            id="monitor-kinds", placeholder="All types", className="monitor-small-filter"),
        dcc.Dropdown([], [], multi=True, id="monitor-contracts", placeholder="All matching contracts — select specific contracts here",
            className="monitor-contract-filter"),
        html.Button("Apply subscriptions", id="monitor-apply", n_clicks=0),
    ], className="monitor-filters"),
    html.Div([html.Span(id="monitor-matches"), html.Span(id="monitor-selection-status")], className="monitor-filter-note"),
    html.Div([
        html.Label("Subscribe by ID", htmlFor="monitor-manual-ids"),
        dcc.Input(id="monitor-manual-ids", type="text", placeholder="TT-543741535855301483 or 543741535855301483; comma-separated for several",
                  debounce=True, className="monitor-manual-input"),
        html.Button("Subscribe to IDs only", id="monitor-manual-apply", n_clicks=0),
    ], className="monitor-filters"),
    html.Div(["Manual IDs replace the current subscription and ignore product/type filters. ",
              html.Span(id="monitor-manual-status")], className="monitor-filter-note"),
    dcc.Store(id="monitor-options-version"),
    html.Div(id="monitor-counts", className="monitor-counts"),
    html.Div(id="monitor-error", className="monitor-error"),
    html.Div(id="monitor-coverage", className="monitor-coverage"),
    dash_table.DataTable(id="monitor-prices", columns=COLUMNS, data=[],
        filter_action="native", sort_action="native", sort_mode="multi", page_size=30,
        fixed_rows={"headers": True},
        style_table={"overflowX": "auto", "overflowY": "auto", "height": "calc(100vh - 245px)", "minHeight": "280px"},
        style_cell={"textAlign": "right", "fontSize": 12, "padding": "8px", "minWidth": 75,
                    "maxWidth": 380, "overflow": "hidden", "textOverflow": "ellipsis",
                    "backgroundColor": "#080808", "color": "#ffffff", "border": "1px solid #292929"},
        style_cell_conditional=[{"if": {"column_id": key}, "textAlign": "left"} for key in ("product", "kind", "contract", "name", "item")],
        style_header={"backgroundColor": "#202020", "color": "white", "fontWeight": "bold", "border": "1px solid #444444"},
        style_filter={"backgroundColor": "#101010", "color": "white"}),
    html.Div(id="monitor-empty", className="monitor-empty"),
    html.Details([html.Summary("Connection details"), html.Pre(id="monitor-events", style={"whiteSpace": "pre-wrap"})]),
    dcc.Interval(id="monitor-poll", interval=1000),
], className="monitor-page")


@app.callback(Output("monitor-contracts", "options"), Output("monitor-contracts", "value"),
              Output("monitor-options-version", "data"), Output("monitor-matches", "children"),
              Input("monitor-poll", "n_intervals"), Input("monitor-products", "value"), Input("monitor-kinds", "value"),
              State("monitor-options-version", "data"), State("monitor-contracts", "value"))
def contract_options(_, products, kinds, version, selected):
    with monitor.lock:
        loaded_at = monitor.loaded_at
    if ctx.triggered_id == "monitor-poll" and version == loaded_at:
        return (no_update,) * 4
    options = monitor.options(products, kinds)
    allowed = {option["value"] for option in options}
    return options, [item for item in selected or [] if item in allowed], loaded_at, (
        f"{len(options):,} matches. Leave contracts blank for all matches. Apply to change the shared live subscription. ")


@app.callback(Output("monitor-selection-status", "children"), Input("monitor-apply", "n_clicks"),
              State("monitor-products", "value"), State("monitor-kinds", "value"), State("monitor-contracts", "value"),
              prevent_initial_call=True)
def apply_subscriptions(_, products, kinds, selected):
    try:
        return monitor.apply_selection(products, kinds, selected)
    except Exception as error:
        return f"Cannot change subscription: {error}"


@app.callback(Output("monitor-manual-status", "children"), Input("monitor-manual-apply", "n_clicks"),
              Input("monitor-manual-ids", "n_submit"), State("monitor-manual-ids", "value"), prevent_initial_call=True)
def subscribe_ids(_, _enter, text):
    try:
        return monitor.apply_ids(text)
    except Exception as error:
        return f"Cannot subscribe: {error}"


@app.callback(Output("monitor-prices", "data"), Output("monitor-status", "children"),
              Output("monitor-counts", "children"), Output("monitor-coverage", "children"),
              Output("monitor-error", "children"), Output("monitor-events", "children"),
              Output("monitor-empty", "children"), Input("monitor-poll", "n_intervals"))
def refresh(_):
    monitor.ensure_started()
    state = monitor.snapshot()
    stream = state["stream"]
    rows = active_contracts(stream)
    counts = Counter(row["kind"] for row in rows)
    cards = [html.Div([html.Strong(str(len(rows))), html.Span("Live now")], className="monitor-count")]
    cards += [html.Div([html.Strong(str(counts[key])), html.Span(label),
               html.Small(f"of {state['discovered'].get(key, 0):,} discovered")], className="monitor-count")
              for key, label in KINDS.items()]
    total = sum(state["discovered"].values())
    subscribed = len((stream["settings"] or {}).get("items", []))
    status = "Discovering all instruments..." if state["loading"] else stream["status"]
    coverage = (f"{subscribed:,} subscribed | {total:,} discovered | {len(stream['rows']):,} items received | "
                "Fresh = live update received within 120 seconds | Display refresh: 1 second")
    if state["loaded_at"]:
        coverage += " | Catalogue loaded: " + state["loaded_at"][:19].replace("T", " ") + " UTC"
    errors = list(state["errors"])
    errors += [event["message"] for event in stream["events"] if "error" in event["message"].lower()]
    empty = "" if rows else ("Discovering instruments and connecting automatically..." if state["loading"] else
        "No fresh live prices yet. Snapshot-only, empty and stale items are hidden; see connection details if updates do not arrive.")
    events = "\n".join(event["time"] + "  " + event["message"] for event in stream["events"])
    return rows, status, cards, coverage, " | ".join(errors), events, empty


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8058)
    args = parser.parse_args()
    monitor.ensure_started()
    try:
        app.run(host="127.0.0.1", port=args.port, debug=False)
    finally:
        monitor.stop()

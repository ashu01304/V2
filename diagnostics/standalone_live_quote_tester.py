"""One-file live quote test. Install: pip install dash lightstreamer-client-lib
Run: python -m diagnostics.standalone_live_quote_tester  (http://127.0.0.1:8059).
Contract IDs load from the saved JSON; nothing connects until Subscribe is clicked.
"""
import atexit
import json
import math
import re
import ssl
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen

from dash import Dash, Input, Output, State, ctx, no_update, dcc, html, dash_table
from lightstreamer.client import LightstreamerClient, Subscription, ClientListener, SubscriptionListener

CATALOGUE_FILE = Path(__file__).resolve().parents[1] / "data" / "quoted_live_probe_full_20261029_131318.json"
TOKEN_URL = "https://insight.corp.hertshtengroup.com/test/amrit/externalapi.php?request=gettmsaltokenwithuser"
SERVER, ADAPTER_SET, DATA_ADAPTER = "https://ls.prod-live.hertshtengroup.com", "MARKET_DATA", "MDS"
FIELDS = "key command instrumentId exchangeTimeNs lsAdapterReceiveTimeNs lsAdapterSendTimeNs mdsSentTimeNs settlementTimeNs bidPrice bidQuantity askPrice askQuantity impliedBidPrice impliedBidQuantity impliedAskPrice impliedAskQuantity open close high low settlementPrice settlementType tradePrice tradeQuantity totalTradedQuantity state VWAP indicativeOpen cumLtq tradeSide".split()
DISPLAY = "tradePrice bidPrice askPrice bidQuantity askQuantity tradeQuantity impliedBidPrice impliedAskPrice totalTradedQuantity VWAP".split()
DISPLAY += [field for field in FIELDS if field not in DISPLAY]
lock, clients, generation, catalogue = threading.RLock(), [], 0, {}
state = {"connection": "Stopped", "subscription": "Not subscribed", "rows": {}, "events": []}


def has_price(row):
    for field in ("tradePrice", "bidPrice", "askPrice", "impliedBidPrice", "impliedAskPrice"):
        try:
            if math.isfinite(float(row.get(field))):
                return True
        except (TypeError, ValueError):
            pass
    return False


def stop():
    global clients, generation
    with lock:
        generation += 1
        previous, clients = clients, []
        state.update(connection="Stopped", subscription="Not subscribed")
        for batch in state.get("batches", {}).values():
            batch["status"] = "Stopped"
        for user in state.get("users", {}).values():
            user.update(connection="Stopped", subscription="Not subscribed")
    for client in previous:
        client.disconnect()


def item_name(value):
    match = re.fullmatch(r"(?:TT-)?([0-9]+)(?:-2)?", value.strip(), re.I)
    if not match:
        raise ValueError("Use a numeric ID, TT-<ID>, or <ID>-2")
    return match[1] + "-2"


def load_saved_catalogue(path=CATALOGUE_FILE):
    with path.open(encoding="utf-8-sig") as source:
        groups = json.load(source)["contracts"]
    result = {}
    for product, structures in groups.items():
        for structure, records in ({"boxes": structures} if isinstance(structures, list) else structures).items():
            for record in records:
                item = item_name(str(record["id"]))
                result[item] = {"name": record["name"], "qh_code": record.get("code", ""),
                                "product": "+1xCL" if product == "Boxes" else product, "structure": structure}
    return result


try:
    catalogue.update(load_saved_catalogue())
    catalogue_status = f"{len(catalogue):,} saved contracts loaded. No catalogue requests are sent."
except (OSError, ValueError, KeyError, TypeError) as error:
    catalogue_status = f"Cannot load saved catalogue: {error}"
catalogue_options = [{"label": f"{r['product']} | {r['qh_code'] or r['name']} | {item}", "value": item}
                     for item, r in catalogue.items()]


def saved_contract_rows():
    months = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
    pattern = r"\b(" + "|".join(months) + r")(\d{2})\b"
    rows = []
    for item, record in catalogue.items():
        dates = [(2000 + int(year)) * 12 + months.index(month) for month, year in re.findall(pattern, record["name"])]
        spacing = re.search(r"(\d+)mo Butterfly", record["name"])
        if spacing and dates:
            dates = [dates[0] + i * int(spacing[1]) for i in range(3)]
        gaps = [abs(b - a) for a, b in zip(dates, dates[1:])]
        rows.append({"product": record["product"], "structure": record["structure"], "name": record["name"],
                     "code": record["qh_code"], "instrument_id": item[:-2], "item": item,
                     "legs": " / ".join(f"{months[d % 12]} {d // 12}" for d in dates),
                     "gaps": " / ".join(map(str, gaps)), "max_gap": max(gaps, default=0),
                     "first": min(dates, default=0)})
    return sorted(rows, key=lambda r: (r["product"], r["structure"], r["first"], r["max_gap"], r["name"]))


saved_rows = saved_contract_rows()


def credentials():
    with urlopen(TOKEN_URL, timeout=30, context=ssl.create_default_context()) as response:
        records = json.load(response)
    if not isinstance(records, list):
        raise ValueError("Token service did not return the expected product/user list")
    result = {}
    for record in records:
        if not record.get("email") or not record.get("token"):
            continue
        user = record["email"].split("@")[0]
        for product in record.get("product", "").split(","):
            if product.strip():
                result[product.strip().upper()] = (user, record["token"])
    return result  # Credentials remain in memory and never enter displayed state.


def start(items, product="CL"):
    auth, groups, unmapped = credentials(), {}, {}
    for item in items:
        route = (catalogue.get(item, {}).get("product") or product).strip().upper()
        if route == "CO" and route not in auth:
            route = "BRN"
        if route not in auth:
            unmapped[item] = f"No token/user mapping for {route}"
            continue
        groups.setdefault(auth[route], []).append(item)
    stop()
    current = generation
    with lock:
        state.update(connection="Connecting" if groups else "No mapped credentials", subscription="Pending",
                     rows={}, events=[], items=items, users={}, batches={}, unmapped=unmapped)
    tokens = {token for user, token in groups}

    def event(user, message, **values):
        with lock:
            if current != generation:
                return
            for token in tokens:
                message = message.replace(token, "[redacted]")
            state["users"].setdefault(user, {}).update(values)
            for key in ("connection", "subscription"):
                state[key] = " | ".join(f"{u}: {v.get(key, 'Pending')}" for u, v in state["users"].items())
            state["events"] = (state["events"] + [user + ": " + message])[-30:]

    def connect(user, token, group):
        class Connection(ClientListener):
            def onStatusChange(self, status):
                event(user, status, connection=status)

            def onServerError(self, code, message):
                event(user, f"Server error {code}: {message}")

        class Prices(SubscriptionListener):
            def __init__(self, batch):
                self.batch = batch

            def batch_status(self, status):
                with lock:
                    if current == generation:
                        state["batches"][self.batch]["status"] = status

            def onSubscription(self):
                self.batch_status("Accepted")
                event(user, "Subscription accepted", subscription="Accepted")

            def onSubscriptionError(self, code, message):
                self.batch_status("Rejected")
                event(user, f"Subscription error {code}: {message}", subscription="Error")

            def onUnsubscription(self):
                self.batch_status("Pending")

            def onItemUpdate(self, update):
                with lock:
                    if current != generation:
                        return
                    item, snapshot = update.getItemName(), update.isSnapshot()
                    previous = state["rows"].get(item, {})
                    received = datetime.now(timezone.utc).isoformat()
                    state["rows"][item] = {
                        "user": user, "snapshot": snapshot, "snapshot_count": previous.get("snapshot_count", 0) + int(snapshot),
                        "live_count": previous.get("live_count", 0) + int(not snapshot),
                        "received_utc": received, "last_live_utc": previous.get("last_live_utc") if snapshot else received,
                        **{field: update.getValue(field) for field in FIELDS},
                    }

        client = LightstreamerClient(SERVER, ADAPTER_SET)
        client.connectionDetails.setUser(user)
        client.connectionDetails.setPassword(token)
        client.connectionOptions.setReconnectTimeout(2500)
        client.addListener(Connection())
        clients.append(client)
        for offset in range(0, len(group), 250):
            batch = str(len(state["batches"]))
            entries = group[offset:offset + 250]
            with lock:
                state["batches"][batch] = {"items": entries, "status": "Pending", "user": user}
            subscription = Subscription("MERGE", entries, FIELDS)
            subscription.setDataAdapter(DATA_ADAPTER)
            subscription.setRequestedSnapshot("yes")
            subscription.setRequestedMaxFrequency("1")
            subscription.addListener(Prices(batch))
            client.subscribe(subscription)
        client.connect()
    try:
        for (user, token), group in groups.items():
            connect(user, token, group)
    except Exception:
        stop()
        raise


def coverage():
    now, rows, counts = datetime.now(timezone.utc), [], dict(requested=len(state.get("items", [])), accepted=0, received=0, priced=0, live=0)
    batches = {item: batch["status"] for batch in state.get("batches", {}).values() for item in batch["items"]}
    for item in state.get("items", []):
        row, meta = state["rows"].get(item, {}), catalogue.get(item, {})
        accepted = batches.get(item) == "Accepted"
        age = (now - datetime.fromisoformat(row["last_live_utc"])).total_seconds() if row.get("last_live_utc") else None
        connected = state.get("users", {}).get(row.get("user"), {}).get("connection", "").startswith("CONNECTED:")
        live = bool(accepted and connected and has_price(row) and age is not None and 0 <= age <= 120)
        label = "Live prices" if live else "Live updates, no prices" if accepted and connected and age is not None and 0 <= age <= 120 else (
            "Stale / disconnected" if row.get("live_count") else "Snapshot prices only" if has_price(row) else "Empty snapshot" if row else "Waiting")
        label = state.get("unmapped", {}).get(item) or ("Rejected" if batches.get(item) == "Rejected" else label)
        counts["accepted"] += int(accepted)
        counts["received"] += int(bool(row))
        counts["priced"] += int(has_price(row))
        counts["live"] += int(live)
        rows.append({"id": item, "item": item, "contract": meta.get("qh_code") or meta.get("name") or item,
            "product": meta.get("product", ""), "status": label, "subscription": batches.get(item, "Not submitted"),
            "LTP": row.get("tradePrice"), "bid": row.get("bidPrice"), "ask": row.get("askPrice"),
            "live_updates": row.get("live_count", 0), "age_seconds": round(age, 1) if age is not None else None})
    return counts, rows


app = Dash(__name__, title="Standalone live quote test", include_assets_files=False)
app.index_string = app.index_string.replace("</head>", """<style>
:root {color-scheme:dark;--Dash-Text-Strong:white;--Dash-Text-Weak:#ccc;--Dash-Fill-Inverse-Strong:#111;--Dash-Fill-Interactive-Strong:white;--Dash-Stroke-Strong:#666;}
body{background:black;color:white;font:13px Arial;margin:20px}input,button,textarea{background:#111;color:white;border:1px solid #666;padding:8px;font:inherit}
button{cursor:pointer}pre{white-space:pre-wrap;background:#111;border:1px solid #333;padding:12px}summary{cursor:pointer}label{margin-right:8px}
#prices table{border-collapse:collapse;width:100%;table-layout:fixed;font:12px Consolas,monospace}#prices th,#prices td{padding:8px;border:1px solid #333;text-align:right;white-space:normal;overflow-wrap:anywhere}
#prices th{background:#222}#prices td:first-child,#prices th:first-child{text-align:left;width:45%}#prices{margin:12px 0;max-width:750px}#prices h4{overflow-wrap:anywhere}
#counts{display:flex;gap:8px;flex-wrap:wrap;margin:12px 0}#counts>div{background:#111;border:1px solid #444;padding:10px 15px}#counts strong{font-size:22px;margin-right:8px}
.dash-table-container{--accent:white;--selected-background:#222}
*{box-sizing:border-box}body{margin:0;background:#080808;color:#eee;font:14px 'Segoe UI',Arial,sans-serif}
main{max-width:1600px;margin:auto;padding:28px 32px}h3{font-size:24px;font-weight:600;margin:0 0 6px}p{line-height:1.5;color:#aaa}
input,button,textarea{border-radius:6px;min-height:38px}button{padding:8px 14px;margin:4px 6px 4px 0}button:hover{background:#292929;border-color:#aaa}
small{color:#888}#live-view,#saved-view{padding-top:22px}#view .tab{padding:12px 22px!important;border:0!important;border-bottom:2px solid transparent!important;flex:0 0 auto!important}
#view .tab--selected{border-bottom-color:white!important;background:#181818!important}#view{border-bottom:1px solid #333}
.saved-heading{display:flex;align-items:center;justify-content:space-between;gap:20px;margin-bottom:20px}.saved-heading h4{font-size:20px;margin:0 0 5px}.saved-heading p{margin:0}
.total-badge{background:#191919;border:1px solid #333;border-radius:8px;padding:10px 16px;white-space:nowrap;color:#aaa}.total-badge strong{color:white;font-size:23px;margin-right:8px}
.saved-toolbar{display:grid;grid-template-columns:220px 220px minmax(220px,1fr);gap:14px;background:#111;border:1px solid #292929;border-radius:8px;padding:16px;margin-bottom:16px}
.saved-toolbar label{display:block;font-size:12px;color:#aaa;margin-bottom:7px}.saved-toolbar input{width:100%;border-color:#444}
#saved-count{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:18px}#saved-count>div{border:1px solid #303030;border-radius:6px;padding:9px 14px;color:#aaa;background:#111}
#saved-count strong{color:white;font-size:18px;margin-right:7px}.table-options{display:flex;justify-content:space-between;align-items:center;gap:12px;color:#999;font-size:12px;margin:12px 0}
#saved-table{border:1px solid #303030;border-radius:8px;overflow:hidden}#saved-table .previous-next-container{padding:10px}
#saved-table input{min-height:28px}#saved-view>small{display:block;margin-top:12px;line-height:1.5}
@media(max-width:800px){main{padding:18px 12px}.saved-toolbar{grid-template-columns:1fr}.saved-heading{align-items:flex-start}.table-options{align-items:flex-start;flex-direction:column}}
</style></head>""")
app.layout = html.Main([
    html.H3("Contract monitor"),
    html.P("Browse saved contracts and check their live quotes.", style={"marginTop": 0}),
    dcc.Tabs(id="view", value="live", children=[
        dcc.Tab(label="Live quotes", value="live", style={"backgroundColor": "#111", "color": "white"},
                selected_style={"backgroundColor": "#333", "color": "white"}),
        dcc.Tab(label="Saved contracts", value="saved", style={"backgroundColor": "#111", "color": "white"},
                selected_style={"backgroundColor": "#333", "color": "white"})], style={"marginTop": 12}),
    html.Div([
    html.Small(catalogue_status),
    dcc.Dropdown(id="instruments", options=catalogue_options, value=[], multi=True, placeholder="Choose saved contracts", style={"margin": "12px 0"}),
    dcc.Input(id="ids", value="", placeholder="Optional manual numeric IDs, TT-IDs or ID-2, separated by commas", style={"width": "55%"}),
    html.Label("Token product"), dcc.Input(id="product", value="CL", style={"width": 85}),
    html.Button("Subscribe", id="subscribe"), html.Button("Stop", id="stop"),
    html.Button("Subscribe all saved contracts", id="subscribe-all"),
    html.P(id="action"), html.P(id="live-status"), html.Div(id="counts"),
    html.P("Accepted = server accepted the subscription batch; it does not guarantee prices. Live prices = post-snapshot updates within 120 seconds with a valid price. Subscriptions use batches of 250 per authenticated user."),
    dash_table.DataTable(id="coverage", data=[], columns=[{"name": label, "id": key} for label, key in (
        ("Contract", "contract"), ("MDS item", "item"), ("Product", "product"), ("Subscription", "subscription"), ("Data status", "status"),
        ("LTP", "LTP"), ("Bid", "bid"), ("Ask", "ask"), ("Live updates", "live_updates"), ("Age (s)", "age_seconds"))],
        page_size=15, sort_action="native", filter_action="native", style_table={"overflowX": "auto"},
        style_cell={"backgroundColor": "#111", "color": "white", "border": "1px solid #333", "padding": 7, "maxWidth": 300, "overflow": "hidden", "textOverflow": "ellipsis"},
        style_header={"backgroundColor": "#222", "color": "white"}, style_filter={"backgroundColor": "#111", "color": "white"}),
    html.H4("Received fields for one ID"),
    dcc.Dropdown(id="detail", options=[], placeholder="Choose an ID to inspect its received fields"), html.Div(id="prices"),
    html.P("Updates every second. LTP = tradePrice. Empty fields mean the feed supplied no value; snapshots are labelled separately from live updates."),
    html.Details([html.Summary("Raw received data and events"), html.Pre(id="result")]),
    html.Details([html.Summary("Exact connection and subscription settings"), html.Pre(json.dumps({
        "server": SERVER, "adapter_set": ADAPTER_SET, "data_adapter": DATA_ADAPTER,
        "user": "Product-mapped username from token service", "token_service": TOKEN_URL,
        "item_format": "<instrument ID>-2", "mode": "MERGE", "snapshot": "yes", "max_frequency": "1", "fields": FIELDS}, indent=2))]),
    ], id="live-view"),
    html.Div([
        html.Div([
            html.Div([html.H4("Your contract list"), html.P("Review products, structures and delivery months.")]),
            html.Div([html.Strong(f"{len(saved_rows):,}"), "saved contracts"], className="total-badge"),
        ], className="saved-heading"),
        html.Div([
            html.Div([html.Label("Product"), dcc.Dropdown(id="saved-product", options=[{"label": "CL–BZ boxes" if p == "+1xCL" else p, "value": p} for p in sorted({r['product'] for r in saved_rows})],
                         multi=True, placeholder="All products")]),
            html.Div([html.Label("Structure"), dcc.Dropdown(id="saved-structure", options=[{"label": s.title(), "value": s} for s in ("outrights", "spreads", "flies", "boxes")],
                         multi=True, placeholder="All structures")]),
            html.Div([html.Label("Search contracts"), dcc.Input(id="saved-search", value="", debounce=0.25,
                         placeholder="Contract name, month or instrument ID…", type="search")]),
        ], className="saved-toolbar"),
        html.Div(id="saved-count"),
        html.Div([html.Span("Click a column heading to sort. Gaps are in months."),
                  dcc.Checklist(id="saved-identifiers", options=[{"label": " Show IDs and saved codes", "value": "show"}], value=[])], className="table-options"),
        dash_table.DataTable(id="saved-table", data=saved_rows, columns=[{"name": label, "id": key} for label, key in (
            ("Product", "product"), ("Structure", "structure"), ("Full contract name", "name"), ("Saved code", "code"),
            ("Delivery months", "legs"), ("Leg gaps", "gaps"), ("Instrument ID", "instrument_id"), ("MDS item", "item"))],
            hidden_columns=["code", "instrument_id", "item"], css=[{"selector": ".show-hide", "rule": "display:none"}],
            page_size=20, sort_action="native", sort_mode="multi", style_table={"overflowX": "auto"},
            style_cell={"backgroundColor": "#101010", "color": "#ddd", "border": "none", "borderBottom": "1px solid #262626", "padding": "13px 16px",
                        "fontFamily": "Segoe UI, Arial, sans-serif", "fontSize": 13, "textAlign": "left", "whiteSpace": "normal", "minWidth": 85, "maxWidth": 420},
            style_cell_conditional=[{"if": {"column_id": "name"}, "fontWeight": "600", "color": "white", "minWidth": 240},
                                    {"if": {"column_id": "legs"}, "minWidth": 240},
                                    {"if": {"column_id": "instrument_id"}, "fontFamily": "Consolas, monospace"}],
            style_data_conditional=[{"if": {"row_index": "odd"}, "backgroundColor": "#171717"}],
            style_header={"backgroundColor": "#202020", "color": "#aaa", "fontWeight": "600", "fontSize": 12}),
        html.Small("Leg months come from full contract names, including inferred butterfly legs. Some saved codes repeat months incorrectly. This list does not indicate live-data availability."),
    ], id="saved-view", style={"display": "none"}),
    dcc.Interval(id="tick", interval=1000),
    dcc.Store(id="detail-generation"),
])


@app.callback(Output("live-view", "style"), Output("saved-view", "style"), Input("view", "value"))
def switch_view(view):
    return ({"display": "none"}, {}) if view == "saved" else ({}, {"display": "none"})


@app.callback(Output("saved-table", "data"), Output("saved-table", "page_current"),
              Input("saved-product", "value"), Input("saved-structure", "value"), Input("saved-search", "value"))
def filter_saved(products, structures, search=""):
    words = (search or "").lower().split()
    return [r for r in saved_rows if (not products or r["product"] in products)
            and (not structures or r["structure"] in structures)
            and all(word in " ".join(str(v) for v in r.values()).lower() for word in words)], 0


@app.callback(Output("saved-table", "hidden_columns"), Input("saved-identifiers", "value"))
def show_identifiers(values):
    return [] if values else ["code", "instrument_id", "item"]


@app.callback(Output("saved-count", "children"), Input("saved-table", "derived_virtual_data"))
def saved_count(rows):
    rows = saved_rows if rows is None else rows
    counts = {kind: sum(r["structure"] == kind for r in rows) for kind in ("outrights", "spreads", "flies", "boxes")}
    return [html.Div([html.Strong(f"{len(rows):,}"), "Matching contracts"])] + [
        html.Div([html.Strong(f"{n:,}"), kind.title()]) for kind, n in counts.items()]


@app.callback(Output("action", "children"), Input("subscribe", "n_clicks"), Input("stop", "n_clicks"), Input("subscribe-all", "n_clicks"),
              State("ids", "value"), State("instruments", "value"), State("product", "value"), State("instruments", "options"), prevent_initial_call=True)
def action(_subscribe, _stop, _all, text, selected, product, options):
    if ctx.triggered_id == "stop":
        stop()
        return "Stopped."
    try:
        values = list(selected or []) + re.split(r"[,;\s]+", (text or "").strip())
        if ctx.triggered_id == "subscribe-all":
            values = [option["value"] for option in options or []]
        items = list(dict.fromkeys(item_name(value) for value in values if value))
        if not items:
            raise ValueError("Choose saved contracts or enter IDs; subscribing to all requires the saved JSON")
        start(items, product or "")
        return f"Requested {len(items):,} IDs. Check acceptance and live-price counts below."
    except Exception as error:
        return f"{type(error).__name__}: {error}"


@app.callback(Output("result", "children"), Output("prices", "children"), Output("live-status", "children"),
              Output("counts", "children"), Output("coverage", "data"), Output("detail", "options"), Output("detail-generation", "data"),
              Input("tick", "n_intervals"), Input("detail", "value"), State("detail-generation", "data"))
def refresh(_, detail=None, version=None):
    with lock:
        items = list(state.get("items", []))
        counts, summary = coverage()
        selected = detail if detail in items else next(iter(items), None)
        rows = {selected: dict(state["rows"].get(selected, {}))} if selected else {}
        raw = json.dumps({"connection": state["connection"], "subscription": state["subscription"], "counts": counts,
                          "selected_item": selected, "received": rows.get(selected), "events": state["events"]}, indent=2)
        status = f"{state['connection']} | Subscription: {state['subscription']}"
        revision = generation
        options = [{"label": f"{catalogue.get(item, {}).get('qh_code') or catalogue.get(item, {}).get('name') or item} | {item}", "value": item}
                   for item in items] if version != revision else no_update
    panels = []
    for item in [selected] if selected else []:
        row = rows.get(item, {})
        values = [("Last message", ("Snapshot" if row["snapshot"] else "Live update") if row else "Waiting"),
                  ("Live updates", row.get("live_count", 0)), ("Snapshots", row.get("snapshot_count", 0)),
                  ("Received (UTC)", row.get("received_utc"))]
        values += [("LTP (tradePrice)" if field == "tradePrice" else field, row.get(field)) for field in DISPLAY]
        panels.append(html.Section([html.H4(item), html.Table([
            html.Thead(html.Tr([html.Th("Field"), html.Th("Received value")])),
            html.Tbody([html.Tr([html.Td(label), html.Td("—" if value is None else str(value))]) for label, value in values]),
        ])]))
    cards = [html.Div([html.Strong(f"{counts[key]:,}"), html.Span(label)]) for key, label in (
        ("requested", "Requested IDs"), ("accepted", "Accepted IDs"), ("received", "IDs with data"),
        ("priced", "IDs with prices"), ("live", "Fresh live prices"))]
    return raw, panels if items else "Enter an ID and click Subscribe to see received prices.", status, cards, summary, options, revision


atexit.register(stop)
if __name__ == "__main__":
    try:
        app.run(host="127.0.0.1", port=8059, debug=False)
    finally:
        stop()

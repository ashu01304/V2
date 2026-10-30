"""Saved contract selection and authenticated MDS subscriptions; no catalogue calls."""
import json
import re
import ssl
from pathlib import Path
from urllib.request import urlopen

from .config import DATA_DIR

CONFIG_PATH = Path(__file__).with_name("live_subscription_config.json")
SERVER = "https://ls.prod-live.hertshtengroup.com"
TOKEN_URL = "https://insight.corp.hertshtengroup.com/test/amrit/externalapi.php?request=gettmsaltokenwithuser"
FIELDS = "key command instrumentId exchangeTimeNs lsAdapterReceiveTimeNs lsAdapterSendTimeNs mdsSentTimeNs settlementTimeNs bidPrice bidQuantity askPrice askQuantity impliedBidPrice impliedBidQuantity impliedAskPrice impliedAskQuantity open close high low settlementPrice settlementType tradePrice tradeQuantity totalTradedQuantity state VWAP indicativeOpen cumLtq tradeSide".split()
ALIASES = {"BRN": "CO", "LCO": "CO", "LGO": "G", "+1XCL": "+1xCL", "BOXES": "+1xCL"}


def product_name(value):
    return ALIASES.get(value.upper(), value.upper())


def contract_expression(name, structure):
    """Use names because some catalogue codes repeat the wrong fly months."""
    months = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
    dates = [(2000 + int(year)) * 12 + months.index(month)
             for month, year in re.findall(r"\b(" + "|".join(months) + r")(\d{2})\b", name)]
    spacing = re.search(r"(\d+)mo Butterfly", name)
    if spacing and len(dates) == 1:
        dates = [dates[0] + i * int(spacing[1]) for i in range(3)]
    if structure == "boxes":
        if len(dates) != 4 or dates[0] != dates[1] or dates[2] != dates[3]:
            raise ValueError("Unsupported box legs: " + name)
        dates = [dates[0], dates[2]]
    weights = {"outrights": [1], "spreads": [1, -1], "flies": [1, -2, 1], "boxes": [1, -1]}[structure]
    if len(dates) != len(weights):
        raise ValueError("Cannot identify legs: " + name)
    codes = ["FGHJKMNQUVXZ"[d % 12] + f"{d // 12 % 100:02d}" for d in dates]
    return "".join(("+" if i and w > 0 else "-" if w < 0 else "") +
                   (f"{abs(w)}*" if abs(w) != 1 else "") + code
                   for i, (w, code) in enumerate(zip(weights, codes)))


def load_selection(config_path=CONFIG_PATH, products=None):
    config_path = Path(config_path)
    config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    products = {product_name(p) for p in (products if products is not None else config["products"])}
    allowed = {"CL", "CO", "G", "HO", "NG", "RB", "+1xCL"}
    if not products or products - allowed:
        raise ValueError("Select live products from: " + ", ".join(sorted(allowed)))
    if 'outrights' in config['structures']:
        raise ValueError("Outright subscriptions are disabled; select spreads, flies and boxes only")
    if not 1 <= int(config["batch_size"]) <= 250:
        raise ValueError("batch_size must be between 1 and 250")
    source = config_path.parent / config["catalogue_file"]
    groups = json.loads(source.read_text(encoding="utf-8-sig"))["contracts"]
    metadata = {}
    for product, structures in groups.items():
        route = "+1xCL" if product == "Boxes" else product_name(product)
        if route not in products:
            continue
        for structure, records in ({"boxes": structures} if isinstance(structures, list) else structures).items():
            if structure not in config["structures"]:
                continue
            for record in records:
                item = str(record["id"]) + "-2"
                metadata[item] = dict(name=record["name"], structure=structure, route=route,
                    product="CL-CO" if structure == "boxes" else route,
                    contract=contract_expression(record["name"], structure))
    if not metadata:
        raise ValueError("No saved contracts match the subscription settings")
    return metadata, int(config["batch_size"])


def credentials():
    with urlopen(TOKEN_URL, timeout=30, context=ssl.create_default_context()) as response:
        records = json.load(response)
    if not isinstance(records, list):
        raise ValueError("Token service did not return a product/user list")
    result = {}
    for record in records:
        if record.get("email") and record.get("token"):
            for product in record.get("product", "").split(","):
                if product.strip():
                    result[product_name(product.strip())] = (record["email"].split("@")[0], record["token"])
    return result


def connect(store, metadata, batch_size):
    if any(m.get('structure') == 'outrights' for m in metadata.values()):
        raise ValueError('Outright subscriptions are disabled')
    from lightstreamer.client import LightstreamerClient, Subscription, ClientListener, SubscriptionListener
    auth, groups, clients = credentials(), {}, []
    missing = sorted({m["route"] for m in metadata.values()} - auth.keys())
    if missing:
        raise ValueError("Token service has no user mapping for: " + ", ".join(missing))
    for item, meta in metadata.items():
        groups.setdefault(auth[meta["route"]], []).append(item)
    tokens = {token for _, token in groups}

    def safe(message):
        for token in tokens:
            message = str(message).replace(token, "[redacted]")
        return message

    def make_client(user, token, items):
        class Connection(ClientListener):
            def onStatusChange(self, status):
                with store.lock:
                    store.connections[user] = status
                    store.status = "CONNECTED:MDS" if any(s.startswith("CONNECTED:") for s in store.connections.values()) else status

            def onServerError(self, code, message):
                with store.lock:
                    store.error = f"{code}: {safe(message)}"

        class Prices(SubscriptionListener):
            def onSubscriptionError(self, code, message):
                with store.lock:
                    store.error = f"{code}: {safe(message)}"

            def onItemUpdate(self, update):
                fields = {field: update.getValue(field) for field in FIELDS}
                store.update(update.getItemName(), fields, record=not update.isSnapshot())

        client = LightstreamerClient(SERVER, "MARKET_DATA")
        client.connectionDetails.setUser(user)
        client.connectionDetails.setPassword(token)
        client.connectionOptions.setReconnectTimeout(2500)
        client.addListener(Connection())
        clients.append(client)
        for offset in range(0, len(items), batch_size):
            entries = items[offset:offset + batch_size]
            for item in entries:
                store.item_users[item] = user
            subscription = Subscription("MERGE", entries, FIELDS)
            subscription.setDataAdapter("MDS")
            subscription.setRequestedSnapshot("yes")
            subscription.setRequestedMaxFrequency("1")
            subscription.addListener(Prices())
            client.subscribe(subscription)
        client.connect()

    try:
        for (user, token), items in groups.items():
            make_client(user, token, items)
    except Exception:
        for client in clients:
            client.disconnect()
        raise
    return clients

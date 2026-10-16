"""Live Lightstreamer bridge. Run: python market_data/live_service.py."""
import argparse
import json
import logging
import math
import re
import sys
from pathlib import Path
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from market_data.live_history import LiveMinuteRecorder

CONTRACT_URL = "https://insight-x.corp.hertshtengroup.com/api/v1/insight/getPDSInstruments"
ENDPOINT = "https://ls-md.corp.hertshtengroup.com"
FIELDS = [
    "key", "command", "AdminPrice", "AskPrice", "AskSize", "BidPrice",
    "BidSize", "Custom", "LastPrice", "LastSize", "MP", "TotalTradedVolume",
    "WAP", "PreviousSettlement", "CurrentSettlement", "Timestamp",
    "InvokeTimestamp", "FormulaType",
]
COLUMN_FIELDS = {
    "admin": "AdminPrice", "ask": "AskPrice", "ask_size": "AskSize",
    "bid": "BidPrice", "bid_size": "BidSize", "last": "LastPrice",
    "last_size": "LastSize", "volume": "TotalTradedVolume", "wap": "WAP",
    "current_settlement": "CurrentSettlement",
    "previous_settlement": "PreviousSettlement",
}
TERM = re.compile(r"([+-]?)(?:(\d+(?:\.\d+)?)\*)?([FGHJKMNQUVXZ]\d{2})")


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) and abs(result) < 1e300 else None
    except (ValueError, TypeError):
        return None


def product_name(product):
    return {"BRN": "CO", "LCO": "CO"}.get(product.upper(), product.upper())


def parse_legs(expression):
    expression = expression.replace(" ", "").upper()
    position, legs = 0, []
    for match in TERM.finditer(expression):
        if match.start() != position:
            raise ValueError("Invalid contract expression")
        sign, size, contract = match.groups()
        legs.append(((-1 if sign == "-" else 1) * float(size or 1), contract))
        position = match.end()
    if not legs or position != len(expression):
        raise ValueError("Use an expression such as V26-X26 or V26-2*X26+Z26")
    return legs


def get_instruments(products):
    request = Request(CONTRACT_URL, data=json.dumps({
        "products": ["BRN" if p == "CO" else p for p in products],
        "strategies": ["OUT"],
    }).encode(), headers={"Content-Type": "application/json"}, method="POST")
    with urlopen(request, timeout=30) as response:
        rows = json.load(response)["data"]
    instruments = {}
    for row in rows:
        product = product_name(row["product"])
        contract = row["qh_code"][-3:].upper()
        if product not in products or not re.fullmatch(r"[FGHJKMNQUVXZ]\d{2}", contract):
            continue
        instruments["TT-" + str(row["instid"])] = (product, contract)
    if not instruments:
        raise ValueError("The instrument API returned no matching outright contracts")
    return instruments


class PriceStore:
    def __init__(self, instruments, max_age=120, recorder=None):
        self.instruments = instruments
        self.max_age = max_age
        self.recorder = recorder
        self.rows = {}
        self.lock = threading.RLock()
        self.status = "CONNECTING"
        self.error = None

    def update(self, item, fields, record=True):
        if item not in self.instruments:
            return
        product, contract = self.instruments[item]
        now = datetime.now(timezone.utc)
        row = dict(product=product, contract=contract, instrument=item,
                   received_at=now.isoformat(), timestamp=fields.get("Timestamp"),
                   raw=fields, _received=now.timestamp())
        row.update({column: number(fields.get(field))
                    for column, field in COLUMN_FIELDS.items()})
        row["mid"] = ((row["bid"] + row["ask"]) / 2
                      if row["bid"] is not None and row["ask"] is not None else None)
        row["value"] = next((row[k] for k in ("admin", "last", "mid")
                             if row[k] is not None), None)
        with self.lock:
            first = not self.rows
            self.rows[(product, contract)] = row
        if self.recorder is not None and record:
            self.recorder.record(product, contract, row["value"], now)
        if first:
            logging.info("First price update received: %s %s", product, contract)

    def health(self):
        with self.lock:
            newest = max((r["_received"] for r in self.rows.values()), default=None)
            age = datetime.now(timezone.utc).timestamp() - newest if newest else None
            return dict(status=self.status, error=self.error,
                        subscribed_instruments=len(self.instruments),
                        received_instruments=len(self.rows), latest_update_age_seconds=age,
                        minute_recording=self.recorder.health() if self.recorder else None)

    def snapshot(self, products, raw=False):
        with self.lock:
            return [{k: v for k, v in row.items()
                     if not k.startswith("_") and (raw or k != "raw")}
                    for row in self.rows.values()
                    if not products or row["product"] in products]

    def expression(self, product, expression):
        legs = parse_legs(expression)
        with self.lock:
            if not self.status.startswith("CONNECTED:"):
                raise LookupError("Lightstreamer is not connected: " + self.status)
            rows = []
            now = datetime.now(timezone.utc).timestamp()
            for coefficient, contract in legs:
                row = self.rows.get((product_name(product), contract))
                if row is None or row["value"] is None:
                    raise LookupError("No live price for " + product + " " + contract)
                if now - row["_received"] > self.max_age:
                    raise LookupError("Price update is older than "
                                      + str(self.max_age) + " seconds: " + contract)
                rows.append((coefficient, row))
            return dict(product=product_name(product), expression=expression,
                        value=sum(c * r["value"] for c, r in rows),
                        timestamp=min(r["received_at"] for _, r in rows),
                        timestamp_basis="oldest_leg_received_at_utc")


def make_handler(store):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            try:
                if parsed.path == "/health":
                    result = store.health()
                elif parsed.path == "/snapshot":
                    result = store.snapshot(
                        [product_name(p) for p in query.get("product", [])],
                        query.get("raw") == ["1"])
                elif parsed.path in ("/latest", "/expression/latest"):
                    product = query["product"][0]
                    expression = query["contract" if parsed.path == "/latest"
                                       else "expression"][0]
                    result = store.expression(product, expression)
                else:
                    self.send_json(404, {"error": "Endpoint not implemented"})
                    return
                self.send_json(200, result)
            except (KeyError, ValueError) as error:
                self.send_json(400, {"error": str(error)})
            except LookupError as error:
                self.send_json(503, {"error": str(error)})

        def send_json(self, status, data):
            body = json.dumps(data, allow_nan=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            logging.debug(format, *args)
    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--products", nargs="+", default=["CL", "NG", "CO"])
    parser.add_argument("--port", type=int, default=8060)
    parser.add_argument("--max-age", type=float, default=120,
                        help="Reject expression prices with older received updates")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    try:
        from lightstreamer.client import (
            LightstreamerClient, Subscription, SubscriptionListener, ClientListener)
    except ImportError:
        parser.exit(1, "Install dependency: python -m pip install lightstreamer-client-lib\n")
    if args.max_age <= 0:
        parser.error("--max-age must be positive")
    logging.info("Loading corporate instrument IDs")
    instruments = get_instruments(list(dict.fromkeys(product_name(p) for p in args.products)))
    recorder = LiveMinuteRecorder()
    store = PriceStore(instruments, args.max_age, recorder)

    class ConnectionListener(ClientListener):
        def onStatusChange(self, status):
            with store.lock:
                store.status = status
            logging.info("Lightstreamer: %s", status)

        def onServerError(self, code, message):
            with store.lock:
                store.error = str(code) + ": " + message
            logging.error("Lightstreamer server: %s %s", code, message)

    class PriceListener(SubscriptionListener):
        def onItemUpdate(self, update):
            try:
                store.update(update.getItemName(),
                             {field: update.getValue(field) for field in FIELDS},
                             record=not update.isSnapshot())
            except Exception:
                logging.exception("Could not process live price update")

        def onSubscriptionError(self, code, message):
            with store.lock:
                store.error = str(code) + ": " + message
            logging.error("Subscription: %s %s", code, message)

    client = LightstreamerClient(ENDPOINT, "AdminPriceAdapter")
    client.connectionDetails.setUser("Test")
    client.addListener(ConnectionListener())
    subscription = Subscription("MERGE", list(instruments), FIELDS)
    subscription.setDataAdapter("AdminPrice_DataAdapter")
    subscription.setRequestedSnapshot("yes")
    subscription.setRequestedMaxFrequency("1")
    subscription.addListener(PriceListener())
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(store))
    try:
        recorder.start()
        client.subscribe(subscription)
        client.connect()
        logging.info("Serving %d instruments at http://127.0.0.1:%s; Ctrl+C to stop",
                     len(instruments), args.port)
        server.serve_forever()
    except KeyboardInterrupt:
        logging.info("Stopping live service")
    finally:
        server.server_close()
        client.disconnect()
        recorder.close()


if __name__ == "__main__":
    main()



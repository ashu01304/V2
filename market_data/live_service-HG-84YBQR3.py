"""Live Lightstreamer bridge. Run: python market_data/live_service.py."""
import argparse
import json
import logging
import math
import re
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from market_data.cache import LivePriceCache
from market_data.config import (
    CACHE_HOURS, SEAC_UPDATE_HOURS, SPREAD_UPDATE_MINUTES)
from market_data.queries import MarketData
from market_data.sync import update as update_database

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
    def __init__(self, instruments, max_age=120):
        self.instruments = instruments
        self.max_age = max_age
        self.rows = {}
        self.lock = threading.RLock()
        self.status = "CONNECTING"
        self.error = None

    def update(self, item, fields):
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
        if first:
            logging.info("First price update received: %s %s", product, contract)

    def health(self):
        with self.lock:
            newest = max((r["_received"] for r in self.rows.values()), default=None)
            age = datetime.now(timezone.utc).timestamp() - newest if newest else None
            return dict(status=self.status, error=self.error,
                        subscribed_instruments=len(self.instruments),
                        received_instruments=len(self.rows), latest_update_age_seconds=age)

    def snapshot(self, products, raw=False):
        with self.lock:
            return [{k: v for k, v in row.items()
                     if not k.startswith("_") and (raw or k != "raw")}
                    for row in self.rows.values()
                    if not products or row["product"] in products]

    def fresh_snapshot(self):
        with self.lock:
            now = datetime.now(timezone.utc).timestamp()
            return [{k: v for k, v in row.items()
                     if not k.startswith("_") and k != "raw"}
                    for row in self.rows.values()
                    if row["value"] is not None
                    and now - row["_received"] <= self.max_age]

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


class BackgroundWork:
    def __init__(self, store):
        self.store = store
        self.cache = LivePriceCache()
        self.stop = threading.Event()
        self.database_lock = threading.Lock()
        self.last_spread_update = None
        self.last_seac_update = None
        self.sync_error = None
        self.threads = []

    def start(self):
        self.threads = [
            threading.Thread(target=self.sample_loop, daemon=True),
            threading.Thread(target=self.sync_loop, daemon=True),
        ]
        for thread in self.threads:
            thread.start()

    def sample_loop(self):
        while not self.stop.is_set():
            try:
                count = self.cache.save_snapshot(self.store.fresh_snapshot())
                if count:
                    logging.info("Cached %d live prices for the current minute", count)
            except Exception:
                logging.exception("Could not save live minute cache")
            now = datetime.now(timezone.utc)
            self.stop.wait(max(1, 60 - now.second - now.microsecond / 1_000_000))

    def sync_loop(self):
        next_spread = next_seac = 0.0
        while not self.stop.is_set():
            now = time.monotonic()
            if now >= next_spread:
                include_seac = now >= next_seac
                try:
                    with self.database_lock:
                        update_database(include_settlements=include_seac)
                    stamp = datetime.now(timezone.utc).isoformat()
                    self.last_spread_update = stamp
                    if include_seac:
                        self.last_seac_update = stamp
                        next_seac = time.monotonic() + SEAC_UPDATE_HOURS * 3600
                    next_spread = time.monotonic() + SPREAD_UPDATE_MINUTES * 60
                    self.sync_error = None
                except Exception as error:
                    self.sync_error = str(error)
                    logging.exception("Historical update failed; retrying in one minute")
                    next_spread = time.monotonic() + 60
            self.stop.wait(5)

    def health(self):
        return dict(cache_hours=CACHE_HOURS,
                    spread_update_minutes=SPREAD_UPDATE_MINUTES,
                    seac_update_hours=SEAC_UPDATE_HOURS,
                    last_spread_update=self.last_spread_update,
                    last_seac_update=self.last_seac_update,
                    sync_error=self.sync_error)

    def close(self):
        self.stop.set()
        for thread in self.threads:
            thread.join(timeout=10)
        self.cache.close()


def records(frame):
    result = []
    for timestamp, price in frame[["timestamp", "price"]].itertuples(index=False):
        result.append({"timestamp": timestamp.isoformat(), "price": float(price)})
    return result


def settlement_records(frame):
    return [{"trading_date": str(trading_date), "price": float(price)}
            for trading_date, price in
            frame[["trading_date", "price"]].itertuples(index=False)
            if price is not None and math.isfinite(float(price))]


def make_handler(store, background):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            try:
                if parsed.path == "/health":
                    result = {**store.health(), **background.health()}
                elif parsed.path == "/snapshot":
                    result = store.snapshot(
                        [product_name(p) for p in query.get("product", [])],
                        query.get("raw") == ["1"])
                elif parsed.path in ("/latest", "/expression/latest"):
                    product = query["product"][0]
                    expression = query["contract" if parsed.path == "/latest"
                                       else "expression"][0]
                    result = store.expression(product, expression)
                elif parsed.path == "/history":
                    frame = background.cache.history(
                        product_name(query["product"][0]), query["contract"][0],
                        query.get("start", [None])[0], query.get("end", [None])[0])
                    result = records(frame)
                elif parsed.path == "/expression/history":
                    product = product_name(query["product"][0])
                    expression = query["expression"][0]
                    start = query.get("start", [None])[0]
                    end = query.get("end", [None])[0]
                    with background.database_lock:
                        data = MarketData()
                        try:
                            frame = data.synthetic(
                                "LCO" if product == "CO" else product,
                                expression, start, end)
                        finally:
                            data.close()
                    result = records(frame)
                elif parsed.path == "/settlement/history":
                    product = product_name(query["product"][0])
                    with background.database_lock:
                        data = MarketData()
                        try:
                            frame = data.settlement(product, query["contract"][0])
                        finally:
                            data.close()
                    result = settlement_records(frame)
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
    store = PriceStore(instruments, args.max_age)

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
                             {field: update.getValue(field) for field in FIELDS})
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
    background = BackgroundWork(store)
    server = ThreadingHTTPServer(
        ("127.0.0.1", args.port), make_handler(store, background))
    try:
        client.subscribe(subscription)
        client.connect()
        background.start()
        logging.info("Serving %d instruments at http://127.0.0.1:%s; Ctrl+C to stop",
                     len(instruments), args.port)
        server.serve_forever()
    except KeyboardInterrupt:
        logging.info("Stopping live service")
    finally:
        server.server_close()
        background.close()
        client.disconnect()


if __name__ == "__main__":
    main()



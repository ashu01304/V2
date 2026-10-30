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
from market_data.config import LIVE_PRODUCTS
from market_data.mds_feed import CONFIG_PATH, load_selection, connect as connect_mds
from market_data.quote_resolver import StructureResolver

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
    return {"BRN": "CO", "LCO": "CO", "LGO": "G", "+1XCL": "CL-CO", "BOXES": "CL-CO"}.get(product.upper(), product.upper())


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
    """Compatibility entry point: saved quoted structures only; no catalogue fetch."""
    metadata, _ = load_selection(products=products)
    return {item: (m["product"], m["contract"]) for item, m in metadata.items()}


class PriceStore:
    def __init__(self, instruments, max_age=120, recorder=None, metadata=None):
        self.instruments = instruments
        self.max_age = max_age
        self.recorder = recorder
        self.rows = {}
        self.lock = threading.RLock()
        self.status = "CONNECTING"
        self.error = None
        self.metadata = metadata or {}
        self.connections, self.item_users = {}, {}
        self.resolver = StructureResolver(self)

    @staticmethod
    def recipe(expression):
        weights = {}
        for weight, contract in parse_legs(expression):
            weights[contract] = weights.get(contract, 0) + weight
        weights = sorted((c, w) for c, w in weights.items() if w)
        if not weights:
            raise ValueError("Expression has no net legs")
        scale = weights[0][1]
        return tuple((c, w / scale) for c, w in weights), scale

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
        mds_fields = {"ask": "askPrice", "ask_size": "askQuantity", "bid": "bidPrice", "bid_size": "bidQuantity",
                      "last": "tradePrice", "last_size": "tradeQuantity", "volume": "totalTradedQuantity",
                      "wap": "VWAP", "current_settlement": "settlementPrice"}
        if item in self.metadata:
            row.update({column: number(fields.get(field)) for column, field in mds_fields.items()})
            row.update(structure=self.metadata[item]["structure"], name=self.metadata[item]["name"],
                       timestamp=fields.get("exchangeTimeNs"))
        row["mid"] = ((row["bid"] + row["ask"]) / 2
                      if row["bid"] is not None and row["ask"] is not None else None)
        row["value"] = next((row[k] for k in ("admin", "last", "mid")
                             if row[k] is not None), None)
        with self.lock:
            first = not self.rows
            self.rows[(product, contract)] = row
        if self.recorder is not None and record and (item not in self.metadata or self.metadata[item]["structure"] == "outrights"):
            self.recorder.record(product, contract, row["value"], now)
        if first:
            logging.info("First price update received: %s %s", product, contract)

    def health(self):
        with self.lock:
            newest = max((r["_received"] for r in self.rows.values()), default=None)
            newest_price = max((r["received_at"] for r in self.rows.values() if r['value'] is not None
                and self.connections.get(self.item_users.get(r['instrument']), self.status).startswith('CONNECTED:')), default=None)
            age = datetime.now(timezone.utc).timestamp() - newest if newest else None
            return dict(status=self.status, error=self.error,
                        subscribed_instruments=len(self.instruments),
                        received_instruments=len(self.rows), latest_update_age_seconds=age,
                        connections=dict(self.connections),
                        latest_price_received_at=newest_price,
                        minute_recording=self.recorder.health() if self.recorder else None)

    def snapshot(self, products, raw=False):
        with self.lock:
            return [dict({k: v for k, v in row.items()
                     if not k.startswith("_") and (raw or k != "raw")},
                     connected=self.connections.get(self.item_users.get(row["instrument"]), self.status).startswith("CONNECTED:"))
                    for row in self.rows.values()
                    if not products or row["product"] in products]

    def expression(self, product, expression, best_sides=True):
        return self.resolver.resolve(product_name(product), expression, best_sides=best_sides)

    def quotes(self, product, expressions, best_sides=False):
        if not isinstance(expressions, list) or len(expressions) > 128 or not all(isinstance(e, str) for e in expressions):
            raise ValueError("Provide a list of at most 128 expression strings")
        quotes = []
        for expression in expressions:
            try:
                quotes.append(self.expression(product, expression, best_sides=best_sides))
            except (LookupError, ValueError) as error:
                quotes.append(dict(product=product, expression=expression, value=None, error=str(error)))
        return dict(quotes=quotes)


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
                elif parsed.path == "/curve":
                    result = store.resolver.curve(product_name(query["product"][0]), query["expression"][0])
                elif parsed.path in ("/latest", "/expression/latest"):
                    product = query["product"][0]
                    expression = query["contract" if parsed.path == "/latest"
                                       else "expression"][0]
                    result = store.expression(product, expression, best_sides=query.get('best_sides', ['1']) != ['0'])
                else:
                    self.send_json(404, {"error": "Endpoint not implemented"})
                    return
                self.send_json(200, result)
            except (KeyError, ValueError) as error:
                self.send_json(400, {"error": str(error)})
            except LookupError as error:
                self.send_json(503, {"error": str(error)})

        def do_POST(self):
            if urlparse(self.path).path != "/quotes":
                self.send_json(404, {"error": "Endpoint not implemented"})
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
                if not 0 < length <= 65536:
                    raise ValueError("Request body must be between 1 and 65536 bytes")
                body = json.loads(self.rfile.read(length))
                self.send_json(200, store.quotes(product_name(body["product"]), body["expressions"], best_sides=body.get('best_sides') is True))
            except (KeyError, ValueError, TypeError) as error:
                self.send_json(400, {"error": str(error)})

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
    parser.add_argument("--config", type=Path, default=CONFIG_PATH,
                        help="JSON file with products, structures and saved catalogue path")
    parser.add_argument("--products", nargs="+", default=None,
                        help="Override configured products, e.g. CL CO +1xCL")
    parser.add_argument("--port", type=int, default=8060)
    parser.add_argument("--no-record", action="store_true",
                        help="Test live subscriptions and HTTP endpoints without recording minute data")
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
    metadata, batch_size = load_selection(args.config, args.products)
    instruments = {item: (meta["product"], meta["contract"]) for item, meta in metadata.items()}
    logging.info("Loaded %d saved contracts from %s", len(instruments), args.config)
    recorder = None if args.no_record else LiveMinuteRecorder()
    store = PriceStore(instruments, args.max_age, recorder, metadata)
    clients = []
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(store))
    try:
        if recorder:
            recorder.start()
        clients = connect_mds(store, metadata, batch_size)
        logging.info("Serving %d instruments at http://127.0.0.1:%s; Ctrl+C to stop",
                     len(instruments), args.port)
        server.serve_forever()
    except KeyboardInterrupt:
        logging.info("Stopping live service")
    finally:
        server.server_close()
        for client in clients:
            client.disconnect()
        if recorder:
            recorder.close()


if __name__ == "__main__":
    main()



"""Discover the complete configured universe and monitor direct quoted prices."""

from collections import Counter
from datetime import datetime, timezone
import re
import threading

import requests

from market_data.config import LIVE_PRODUCTS
from market_data.live_probe import StreamProbe, catalogue_item, catalogue_rows
from market_data.legacy_live_service import CONTRACT_URL, ENDPOINT, FIELDS, number, product_name

PRICE_FIELDS = ("LastPrice", "BidPrice", "AskPrice", "BidSize", "AskSize", "LastSize", "AdminPrice",
                "TotalTradedVolume", "WAP", "PreviousSettlement", "CurrentSettlement")
KINDS = {"OUT": "Outrights", "MS": "Spreads", "MF": "Flies", "BOX": "Box spreads"}


def discovery_requests():
    return [
        {"products": ["BRN" if product == "CO" else product for product in LIVE_PRODUCTS],
         "strategies": ["OUT", "MS", "MF"]},
        {"products": ["+1xCL"], "strategies": [""]},
    ]


def monitor_product_name(product):
    product = (product or "").strip()
    return "+1xCL" if product.upper() == "+1XCL" else product_name(product)


def kind(row):
    return "BOX" if monitor_product_name(row.get("product")) == "+1xCL" else row.get("strategy", "")


def matching_instruments(catalogue, products=None, kinds=None):
    products = {monitor_product_name(product) for product in products or []}
    return {item: row for item, row in catalogue.items()
            if (not products or monitor_product_name(row.get("product", "")) in products)
            and (not kinds or kind(row) in kinds)}


def manual_items(text):
    items = []
    for value in re.split(r"[,;\s]+", (text or "").strip()):
        if not value:
            continue
        match = re.fullmatch(r"(?:TT-)?([0-9]+)", value, flags=re.IGNORECASE)
        if not match:
            raise ValueError(f"Invalid instrument ID: {value}. Use a numeric ID or TT-<ID>.")
        item = "TT-" + match[1]  # Preserve 64-bit IDs as strings throughout.
        if item not in items:
            items.append(item)
    if not items:
        raise ValueError("Enter at least one instrument ID")
    return items


def active_contracts(state, max_age=120, now=None):
    """A snapshot alone is not evidence that an instrument is updating live."""
    if not state.get("connection_status", state["status"]).startswith("CONNECTED:"):
        return []
    now = now or datetime.now(timezone.utc)
    metadata = (state["settings"] or {}).get("instruments", {})
    rows = []
    for quote in state["rows"]:
        if not quote.get("live_updates") or not quote.get("last_live_received_at_utc"):
            continue
        age = (now - datetime.fromisoformat(quote["last_live_received_at_utc"])).total_seconds()
        if age > max_age or not any(number(quote.get(field)) is not None for field in ("LastPrice", "BidPrice", "AskPrice", "AdminPrice")):
            continue
        instrument = metadata.get(quote["item"], {})
        rows.append({"id": quote["item"], "contract": instrument.get("qh_code") or instrument.get("name") or quote["item"],
            "product": monitor_product_name(instrument.get("product", "")), "kind": kind(instrument),
            "name": instrument.get("name", ""), **{field: number(quote.get(field)) for field in PRICE_FIELDS},
            "live_updates": quote["live_updates"], "age_seconds": round(max(0, age), 1),
            "received_at_utc": quote["last_live_received_at_utc"], "feed_timestamp": quote.get("Timestamp"), "item": quote["item"]})
    return sorted(rows, key=lambda row: (row["product"], row["kind"], row["contract"]))


class LiveContractMonitor:
    def __init__(self, probe=None):
        self.probe = probe or StreamProbe()
        self.lock = threading.RLock()
        self.thread = None
        self.closing = False
        self.loading = False
        self.errors = []
        self.loaded_at = None
        self.catalogue = {}

    def ensure_started(self):
        with self.lock:
            if self.thread is not None or self.closing:
                return
            self.loading = True
            self.thread = threading.Thread(target=self._load, name="live-contract-monitor", daemon=True)
            self.thread.start()

    def _load(self):
        instruments, errors = {}, []
        for payload in discovery_requests():
            with self.lock:
                if self.closing:
                    return
            try:
                with requests.Session() as session:
                    response = session.post(CONTRACT_URL, json=payload, timeout=30, allow_redirects=False)
                response.raise_for_status()
                rows = catalogue_rows(response.json())
                for row in rows:
                    item = catalogue_item(row)
                    if item:
                        instruments.setdefault(item, row)
            except (ValueError, TypeError, requests.RequestException) as error:
                errors.append(f"Discovery for {', '.join(payload['products'])}: {error}")
        with self.lock:
            self.catalogue = instruments
            self.errors = errors
            self.loaded_at = datetime.now(timezone.utc).isoformat()
            if self.closing:
                self.loading = False
                return
            if instruments:
                try:
                    self._subscribe(instruments)
                except Exception as error:
                    self.errors.append(f"Subscription startup: {error}")
            else:
                self.errors.append("No instrument IDs were discovered. Check corporate connectivity and restart the monitor.")
            self.loading = False

    def _subscribe(self, instruments):
        settings = dict(server=ENDPOINT, adapter_set="AdminPriceAdapter", data_adapter="AdminPrice_DataAdapter",
            user="Test", mode="MERGE", snapshot="yes", frequency="1", items=list(instruments),
            fields=FIELDS, instruments=instruments, batch_size=250)
        self.probe.start(settings)

    def options(self, products=None, kinds=None):
        with self.lock:
            matches = matching_instruments(self.catalogue, products, kinds)
            return [{"value": item, "label": f"{row.get('qh_code') or row.get('name') or item} | "
                     f"{monitor_product_name(row.get('product', ''))} | {kind(row)} | {item}"}
                    for item, row in sorted(matches.items(), key=lambda pair: (
                        pair[1].get("product", ""), kind(pair[1]), pair[1].get("qh_code") or pair[1].get("name") or pair[0]))]

    def apply_selection(self, products=None, kinds=None, items=None):
        with self.lock:
            if self.loading or not self.catalogue:
                raise ValueError("Wait for catalogue discovery to finish")
            if self.closing:
                raise ValueError("The monitor is stopping")
            matches = matching_instruments(self.catalogue, products, kinds)
            if items:
                if any(item not in matches for item in items):
                    raise ValueError("A selected contract is outside the current filters; select it again")
                matches = {item: matches[item] for item in dict.fromkeys(items)}
            if not matches:
                raise ValueError("No contracts match these filters; the subscription was not changed")
            existing = (self.probe.snapshot()["settings"] or {}).get("items", [])
            if set(existing) == set(matches):
                return f"Already subscribed to these {len(matches):,} contracts."
            self._subscribe(matches)
            noun = "contract" if len(matches) == 1 else "contracts"
            return f"Subscribed to {len(matches):,} {noun}. Waiting for fresh updates."

    def apply_ids(self, text):
        items = manual_items(text)
        with self.lock:
            if self.loading:
                raise ValueError("Wait for initial catalogue discovery to finish")
            if self.closing:
                raise ValueError("The monitor is stopping")
            existing = (self.probe.snapshot()["settings"] or {}).get("items", [])
            if set(existing) == set(items):
                return "Already subscribed to these IDs."
            instruments = {item: self.catalogue.get(item, {"product": "Manual", "qh_code": "", "name": item, "strategy": "ID"})
                           for item in items}
            self._subscribe(instruments)
            return f"Subscribed directly to {len(items)} ID(s). Product/type filters were bypassed; fresh prices appear below."

    def snapshot(self):
        with self.lock:
            result = {"loading": self.loading, "errors": list(self.errors), "loaded_at": self.loaded_at,
                      "discovered": dict(Counter(kind(row) for row in self.catalogue.values()))}
        result["stream"] = self.probe.snapshot()
        return result

    def stop(self):
        with self.lock:
            self.closing = True
        self.probe.stop()

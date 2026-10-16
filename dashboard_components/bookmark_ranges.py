"""Daily relative ranges fetched only for visible bookmarked dashboard cells."""

import json
from math import isfinite
from threading import Lock

import duckdb
import pandas as pd
from flask import jsonify, request
from requests import RequestException

from market_data.intraday import SESSION_TIMEZONE, session_window
from market_data import MarketData
from market_data.queries import database_signature
from market_data.live_client import fetch_live_quote


class BookmarkRanges:
    def __init__(self):
        self.lock = Lock()
        self.signature = None
        self.session = None
        self.history = {}
        self.observed = {}

    def positions(self, selections, now=None):
        start, end = session_window(1, now)
        local = end.tz_convert(SESSION_TIMEZONE)
        # The session is closed from 16:00 to 17:00 and on weekend trading dates.
        day = (local.tz_localize(None) - pd.Timedelta(hours=17)).normalize() + pd.Timedelta(days=1)
        if local.hour == 16 or day.dayofweek >= 5:
            return {}
        with self.lock:
            if self.session != start:
                self.session, self.history, self.observed = start, {}, {}
            signature = database_signature()
            if signature is None:
                return {}
            if self.signature != signature:
                self.signature, self.history = signature, {}
            missing = [item for item in selections if item[0] not in self.history]
            if missing:
                self.history.update(self.load_ranges(missing, start, end))
            result = {}
            for key, product, expression in selections:
                quote = fetch_live_quote(product, expression)
                if not quote:
                    continue
                try:
                    timestamp = pd.to_datetime(quote["timestamp"], utc=True)
                    price = float(quote["value"])
                    current = end if now is not None else pd.Timestamp.now(tz="UTC")
                    if not isfinite(price) or not start <= timestamp <= current:
                        continue
                except (KeyError, ValueError, TypeError):
                    continue
                observed = self.observed.get(key, (price, price))
                self.observed[key] = (min(observed[0], price), max(observed[1], price))
                lows, highs = [price], [price]
                for bounds in (self.history.get(key), self.observed[key]):
                    if bounds:
                        lows.append(bounds[0])
                        highs.append(bounds[1])
                low, high = min(lows), max(highs)
                # A flat session has no relative direction; show its midpoint.
                result[key] = (price - low) / (high - low) if high > low else 0.5
            return result

    @staticmethod
    def load_ranges(selections, start, end):
        db = MarketData()
        try:
            return db.expression_ranges(selections, start.to_pydatetime(), end.to_pydatetime())
        finally:
            db.close()


def register_bookmark_ranges(app, universe, format_contract_label):
    service = BookmarkRanges()
    contracts = {format_contract_label(contract): expressions
                 for contract, expressions in universe["contracts"].items()}

    @app.server.post("/_bookmark-ranges")
    def bookmark_ranges():
        body = request.get_json(silent=True) or {}
        keys = body.get("keys", []) if isinstance(body, dict) else []
        if not isinstance(keys, list) or len(keys) > 500:
            return jsonify({}), 400
        selections = []
        for key in dict.fromkeys(key for key in keys if isinstance(key, str)):
            try:
                product, contract, column = json.loads(key)
                if product not in {"CL", "CO", "CL-CO", "NG"}:
                    continue
                expression = contracts[contract][column]
                selections.append((key, product, expression))
            except (ValueError, TypeError, KeyError):
                continue
        if not selections:
            return jsonify({})
        try:
            return jsonify(service.positions(selections))
        except (OSError, RuntimeError, ValueError, TypeError, duckdb.Error, RequestException):
            return jsonify({})

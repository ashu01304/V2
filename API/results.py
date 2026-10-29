"""Views based on the endpoint samples returned by the manual probe."""

from dataclasses import dataclass
import re
from typing import Any


@dataclass
class APIResult:
    raw: Any

    @property
    def source_records(self):
        """Rows with server field names, available alongside normalized records."""
        value = self.raw
        if isinstance(value, dict) and "data" in value:
            value = value["data"]
        if isinstance(value, list):
            return [row if isinstance(row, dict) else {"value": row} for row in value]
        if isinstance(value, dict):
            return [value]
        return [] if value is None else [{"value": value}]

    @property
    def records(self):
        """Common names; timestamp is Unix milliseconds, as observed in probes."""
        aliases = {
            "Contract": "instrument", "product": "instrument", "Product": "instrument",
            "Timestamp": "timestamp", "time": "timestamp", "Date": "timestamp",
            "Price": "price", "Open": "open", "High": "high", "Low": "low",
            "Close": "close", "Volume": "volume", "BuyVolume": "buy_volume",
            "SellVolume": "sell_volume", "buyvolume": "buy_volume",
            "sellvolume": "sell_volume", "qty": "quantity",
            "buyVolume": "buy_volume", "sellVolume": "sell_volume",
        }
        normalized = []
        for row in self.source_records:
            result = {}
            for key, value in row.items():
                target = aliases.get(key, key)
                if target in result:
                    raise ValueError(f"Response fields conflict when normalized to {target}; inspect raw")
                result[target] = value
            normalized.append(result)
        return normalized

    @property
    def metadata(self):
        """Preserve pagination, status and other non-row fields."""
        if not isinstance(self.raw, dict):
            return {}
        return {key: value for key, value in self.raw.items() if key != "data"}

    @property
    def has_more(self):
        """TAS page completeness, without issuing hidden pagination requests."""
        info = self.metadata
        return bool(info.get("next")) or (info.get("page") is not None and
                info.get("total_pages") is not None and int(info["page"]) < int(info["total_pages"]))

    @property
    def levels(self):
        """VAP: one row per price level; volume-side meaning is unconfirmed."""
        rows = []
        for row in self.records:
            encoded = row.get("vap")
            if encoded is None or encoded == "":
                continue
            if not isinstance(encoded, str):
                raise ValueError("Expected a VAP string; inspect raw response")
            for level in encoded.split(";"):
                if not level.strip():
                    continue
                match = re.fullmatch(r"\s*([+-]?\d+(?:\.\d+)?):(\d+)-(\d+)\s*", level)
                if not match:
                    raise ValueError("Unrecognized VAP price/volume encoding; inspect raw response")
                price, first, second = match.groups()
                rows.append({**{key: value for key, value in row.items() if key != "vap"},
                             "price": float(price), "volume_1": int(first), "volume_2": int(second)})
        return rows

    def to_frame(self):
        """One normalized row per quote/candle/trade, preserving API row order."""
        return self._frame(self.records)

    def levels_frame(self):
        return self._frame(self.levels)

    @staticmethod
    def _frame(records):
        import pandas as pd
        frame = pd.json_normalize(records)
        if "timestamp" in frame:
            frame["timestamp"] = pd.to_datetime(frame["timestamp"], unit="ms", utc=True, errors="raise")
        return frame

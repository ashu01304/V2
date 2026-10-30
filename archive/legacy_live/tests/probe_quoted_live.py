"""Opt-in corporate live check. Does not change production subscriptions or databases.

Run manually: python -m tests.probe_quoted_live --seconds 45
"""

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import time

import requests

from market_data.live_probe import StreamProbe, catalogue_item, catalogue_rows
from market_data.live_service import CONTRACT_URL, ENDPOINT, FIELDS


def rank(row):
    codes = re.findall(r"[FGHJKMNQUVXZ]\d{2}", row.get("qh_code") or "")
    months = [int(code[1:]) * 12 + "FGHJKMNQUVXZ".index(code[0]) for code in codes]
    if months:
        return (min(months), max(months) - min(months), str(row.get("name")))
    date = str(row.get("lasttradedate") or "99999999")
    # Prefer nearby box legs, retaining exact names and instrument IDs.
    named = re.findall(r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)(\d{2})", row.get("name") or "")
    positions = [int(year)*12 + "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split().index(month) for month, year in named]
    return (int(date[:6]) if date[:6].isdigit() else 999999, max(positions)-min(positions) if positions else 0, str(row.get("name")))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=int, default=45)
    args = parser.parse_args()
    report = {"started_at_utc": datetime.now(timezone.utc).isoformat(), "catalogues": [], "selected": []}
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    for label, payload in (
        ("CL", {"products": ["CL"], "strategies": ["OUT", "MS", "MF"]}),
        ("boxes", {"products": ["+1xCL"], "strategies": [""]}),
    ):
        response = requests.post(CONTRACT_URL, json=payload, timeout=30, allow_redirects=False)
        response.raise_for_status()
        rows = catalogue_rows(response.json())
        counts = dict(Counter(str(row.get("strategy")) for row in rows))
        report["catalogues"].append({"label": label, "request": payload, "http": response.status_code,
                                     "count": len(rows), "strategy_counts": counts, "rows": rows})
        print(json.dumps({"catalogue": label, "count": len(rows), "strategies": counts}), flush=True)
        for strategy in (["OUT", "MS", "MF"] if label == "CL" else [""]):
            candidates = [row for row in rows if row.get("strategy") == strategy and catalogue_item(row)
                          and str(row.get("lasttradedate") or "99999999") >= today]
            for row in sorted(candidates, key=rank)[:(2 if strategy == "OUT" else 4)]:
                report["selected"].append({"group": strategy or "BOX", "item": catalogue_item(row), "catalogue": row})
    settings = dict(server=ENDPOINT, adapter_set="AdminPriceAdapter", data_adapter="AdminPrice_DataAdapter",
                    user="Test", mode="MERGE", snapshot="yes", frequency="1",
                    items=list(dict.fromkeys(row["item"] for row in report["selected"])), fields=FIELDS)
    report["settings"] = settings
    print(json.dumps({"subscribe": [{"group": row["group"], "item": row["item"],
          "code": row["catalogue"].get("qh_code"), "name": row["catalogue"].get("name")}
          for row in report["selected"]]}), flush=True)
    probe = StreamProbe()
    try:
        probe.start(settings)
        deadline = time.monotonic() + max(1, min(120, args.seconds))
        while time.monotonic() < deadline:
            time.sleep(1)
        state = probe.snapshot()
        report["stream"] = state
        by_item = {row["item"]: row for row in state["rows"]}
        for entry in report["selected"]:
            received = by_item.get(entry["item"], {})
            entry["received"] = received
            print(json.dumps({"group": entry["group"], "code": entry["catalogue"].get("qh_code"),
                  "name": entry["catalogue"].get("name"), "item": entry["item"],
                  "updates": received.get("updates", 0), "snapshot": received.get("snapshot"),
                  "prices": {key: received.get(key) for key in ("BidPrice", "AskPrice", "AdminPrice", "LastPrice", "Timestamp")}}), flush=True)
        print(json.dumps({"status": state["status"], "events": state["events"]}), flush=True)
    finally:
        probe.stop()
        report["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        destination = Path("data") / ("quoted_live_probe_" + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S") + ".json")
        destination.parent.mkdir(exist_ok=True)
        destination.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print("Report: " + str(destination.resolve()), flush=True)


if __name__ == "__main__":
    main()

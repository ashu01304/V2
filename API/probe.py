"""Manual response inspection. Run from V2: python -m API.probe --login"""

import argparse
import json
from pathlib import Path

from .auth import automatic_browser_token, browser_token
from .connection import QHClient, QHAPIError
from .endpoints import fairvalue, fairvalue_historical, ohlc, tas, vap

ENDPOINTS = ("fairvalue", "fairvalue_historical", "ohlc", "vap", "tas")


def safe_preview(value, depth=0):
    """Bound samples and remove authentication-shaped fields from printed JSON."""
    if depth > 6:
        return "<nested value omitted>"
    if isinstance(value, dict):
        return {key: ("<redacted>" if any(word in str(key).lower() for word in
                     ("access", "token", "authorization", "password", "secret", "cookie"))
                     else safe_preview(item, depth + 1))
                for key, item in list(value.items())[:25]}
    if isinstance(value, list):
        return [safe_preview(item, depth + 1) for item in value[:3]]
    if isinstance(value, str):
        if value.startswith("eyJ") or value.lower().startswith("bearer "):
            return "<redacted>"
        return value[:500]
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    login = parser.add_mutually_exclusive_group()
    login.add_argument("--login", action="store_true", help="Open Microsoft auth in your browser; prompt for token")
    login.add_argument("--auto-login", action="store_true", help="Read token automatically in Edge and save it to .env")
    parser.add_argument("--endpoint", choices=ENDPOINTS + ("all",), default="fairvalue")
    parser.add_argument("--instruments", default="CLZ26-F27-G27", help="Comma-separated QH codes")
    parser.add_argument("--count", type=int, default=3)
    parser.add_argument("--no-cache", action="store_true", help="Bypass cached responses for fresh API probes")
    parser.add_argument("--tas-payload", type=Path, help="JSON file containing products")
    args = parser.parse_args()
    if args.count < 1:
        parser.error("--count must be positive")
    selected = ENDPOINTS if args.endpoint == "all" else (args.endpoint,)
    payloads = {}
    # Read and validate input before authentication or any network requests.
    for name, filename in (("tas", args.tas_payload),):
        if name not in selected:
            continue
        if filename is None:
            if args.endpoint != "all":
                parser.error(f"--{name.split('_')[0]}-payload is required for {name}")
            continue
        try:
            value = json.loads(filename.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            parser.error(f"Cannot read valid JSON for {name}")
        required = "products"
        if not isinstance(value, dict) or not isinstance(value.get(required), list) or not value[required]:
            parser.error(f"{name} payload needs a nonempty {required} array")
        payloads[name] = value
    try:
        token = automatic_browser_token() if args.auto_login else browser_token() if args.login else None
        failures = 0
        with QHClient(token, cache_ttl=0 if args.no_cache else 60) as client:
            for name in selected:
                if name == "tas" and name not in payloads:
                    print(f"\n{name}: SKIPPED (supply its payload file to include this endpoint)")
                    continue
                print(f"\n=== {name} ===", flush=True)
                try:
                    if name == "fairvalue":
                        result = fairvalue.fetch(client, args.instruments)
                    elif name == "fairvalue_historical":
                        result = fairvalue_historical.fetch(client, args.instruments, count=args.count)
                    elif name == "ohlc":
                        result = ohlc.fetch(client, args.instruments, count=args.count)
                    elif name == "vap":
                        result = vap.fetch(client, args.instruments, count=args.count)
                    else:
                        result = tas.fetch(client, payloads[name]["products"])
                    print(json.dumps(client.last_response, indent=2))
                    print("Top-level type:", type(result.raw).__name__)
                    if isinstance(result.raw, dict):
                        print("Top-level keys:", list(result.raw))
                    print("Record-view count:", len(result.records))
                    print("Raw JSON preview (at most 3 items per array):")
                    print(json.dumps(safe_preview(result.raw), indent=2, ensure_ascii=True)[:8000])
                    print("Normalized records preview (timestamp in Unix milliseconds):")
                    print(json.dumps(safe_preview(result.records), indent=2, ensure_ascii=True)[:4000])
                    if name == "tas":
                        print("Pagination:", json.dumps(safe_preview(result.metadata)))
                        print("More pages available:", result.has_more)
                    if name == "vap":
                        print("Parsed price levels (volume_1/volume_2 meanings unconfirmed):")
                        print(json.dumps(safe_preview(result.levels), indent=2)[:4000])
                except (QHAPIError, ValueError) as error:
                    failures += 1
                    print("ERROR:", error)
        return 1 if failures else 0
    except (QHAPIError, ValueError) as error:
        print("ERROR:", error)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

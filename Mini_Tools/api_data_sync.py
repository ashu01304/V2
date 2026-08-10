import argparse
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api import OHLC
from api.config import MAX_BARS_PER_CALL, MAX_CONTRACTS_PER_CALL
from database.api_manager import APIDatabaseManager


def sync(instruments, interval="1D", count=None, start=None, end=None):
    api = OHLC()
    database = APIDatabaseManager()
    try:
        missing = [item for item in instruments if not database.has_coverage(
            item, interval, count, start, end)]
        if not missing:
            print("Requested data is already available in api_market_data.db")
            return 0
        seconds = {"1M": 60, "5M": 300, "1H": 3600, "1D": 86400}[interval.upper()]
        bars_each = count or ((end - start) // seconds + 1 if start is not None and end is not None else MAX_BARS_PER_CALL)
        batch_size = min(MAX_CONTRACTS_PER_CALL, MAX_BARS_PER_CALL // bars_each)
        if batch_size < 1:
            raise ValueError(f"COUNT cannot exceed {MAX_BARS_PER_CALL:,}")
        saved = 0
        for index in range(0, len(missing), batch_size):
            batch = missing[index:index + batch_size]
            saved += database.save_ohlc(
                api.get(batch, interval, count, start, end), interval)
        print(f"Saved {saved} candles; skipped {len(instruments) - len(missing)} cached instruments.")
        return saved
    finally:
        database.close()


def parse_args():
    parser = argparse.ArgumentParser(description="Fetch and store QH API OHLC data")
    parser.add_argument("instruments", nargs="+", help="API instrument codes")
    parser.add_argument("--interval", default="1D", choices=("1M", "5M", "1H", "1D"))
    parser.add_argument("--count", type=int)
    parser.add_argument("--start", type=int, help="Unix timestamp")
    parser.add_argument("--end", type=int, help="Unix timestamp")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    sync(args.instruments, args.interval, args.count, args.start, args.end)

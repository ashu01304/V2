import logging
import msvcrt
import sys
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from market_data.config import DATA_DIR
from market_data.sync import update


UPDATE_INTERVAL_SECONDS = 60 * 60
LOG_PATH = DATA_DIR / "market_data_sync.log"
LOCK_PATH = DATA_DIR / "market_data_sync.lock"


def configure_logging():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=LOG_PATH,
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )


def acquire_lock():
    try:
        lock = Path(LOCK_PATH).open("a+")
        lock.seek(0)
        if not lock.read(1):
            lock.write("1")
            lock.flush()
        lock.seek(0)
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        if "lock" in locals():
            lock.close()
        return None
    return lock


def main():
    configure_logging()
    lock = acquire_lock()
    if lock is None:
        message = "Hourly updater is already running; new process stopped."
        print(message, flush=True)
        logging.warning(message)
        return

    logging.info("Hourly market-data updater started")
    include_settlements = True
    try:
        while True:
            started = time.monotonic()
            try:
                result = update(include_settlements=include_settlements)
                message = (
                    "Market-data update completed successfully | "
                    f"new spread points: {result['spread_added']:,} | "
                    f"new settlement points: {result['settlement_added']:,}"
                )
                logging.info(message)
                include_settlements = False
            except Exception:
                logging.exception("Market-data update failed; it will retry next hour")

            elapsed = time.monotonic() - started
            time.sleep(max(0, UPDATE_INTERVAL_SECONDS - elapsed))
    finally:
        lock.close()


if __name__ == "__main__":
    main()

import argparse
import faulthandler
import logging
import msvcrt
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from market_data.config import DATABASE_PATH, DATA_DIR
from market_data.sync import update


LOG_PATH = DATA_DIR / "market_data_sync.log"
LOCK_PATH = DATA_DIR / "market_data_sync.lock"


def configure_logging():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(LOG_PATH, encoding="utf-8"),
        ],
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        force=True,
    )


def acquire_lock():
    lock = Path(LOCK_PATH).open("a+")
    try:
        lock.seek(0)
        if not lock.read(1):
            lock.write("1")
            lock.flush()
        lock.seek(0)
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        lock.close()
        raise
    return lock


def main():
    parser = argparse.ArgumentParser(description="Create or refresh the market database.")
    parser.add_argument("--once", action="store_true", help="Compatibility option: every run updates once and exits.")
    parser.parse_args()
    configure_logging()
    faulthandler.enable()
    logging.info("Market-data updater started | database: %s", DATABASE_PATH)
    logging.info("Progress and errors are also saved to %s", LOG_PATH)
    try:
        lock = acquire_lock()
    except OSError:
        logging.exception(
            "Cannot acquire updater lock at %s. Check for another watcher "
            "or a file-access problem.", LOCK_PATH
        )
        return 1

    try:
        result = update(include_settlements=True)
        logging.info(
            "Update completed successfully | new spread points: %s | "
            "new settlement points: %s | database: %s",
            f"{result['spread_added']:,}", f"{result['settlement_added']:,}",
            DATABASE_PATH,
        )
        return 0
    except Exception:
        logging.exception("Market-data update failed; database was not published.")
        return 1
    except KeyboardInterrupt:
        logging.warning("Updater stopped by user. An unfinished import is not published.")
        return 130
    finally:
        lock.close()


if __name__ == "__main__":
    sys.exit(main())

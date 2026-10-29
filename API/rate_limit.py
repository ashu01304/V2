"""Local cross-process endpoint locks and persistent rolling-window budgets."""

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time

STATE_DIR = Path(__file__).resolve().parents[1] / "data" / "qh_api"


@contextmanager
def endpoint_state(token, path, directory=STATE_DIR):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256((token + "\n" + path).encode()).hexdigest()
    lock_path = directory / (key + ".lock")
    state_path = directory / (key + ".json")
    with lock_path.open("a+b") as lock:
        if lock.seek(0, 2) == 0:
            lock.write(b"0")
            lock.flush()
        locked = False
        try:
            while not locked:
                lock.seek(0)
                try:
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                except (BlockingIOError, OSError):
                    time.sleep(0.1)
            # Fail closed if state is corrupt, rather than silently reset budgets.
            state = json.loads(state_path.read_text()) if state_path.exists() else {}

            def save():
                descriptor, temporary = tempfile.mkstemp(dir=directory, suffix=".tmp")
                try:
                    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                        json.dump(state, output)
                    os.replace(temporary, state_path)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)

            yield state, save
        finally:
            if locked:
                lock.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(lock, fcntl.LOCK_UN)


def reserve(state, save, limits):
    """Record attempts before sending; rejected/failed requests count too."""
    while True:
        now = time.time()
        history = [stamp for stamp in state.get("attempts", []) if stamp > now - 86400]
        wait_until = max(state.get("next_allowed", 0),
                         (history[-1] + 60 / limits[0]) if history else 0)
        for window, limit in zip((60, 3600, 86400), limits):
            recent = [stamp for stamp in history if stamp > now - window]
            if len(recent) >= limit:
                wait_until = max(wait_until, recent[-limit] + window + 0.05)
        if wait_until <= now:
            state["attempts"] = history + [now]
            save()
            return
        time.sleep(min(wait_until - now, 30))

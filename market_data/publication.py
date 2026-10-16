"""Serialize master writers and publish closed database copies atomically."""

from contextlib import contextmanager
import os
from pathlib import Path
import time


@contextmanager
def writer_lock(database_path, timeout=60):
    lock_path = Path(str(Path(database_path).resolve()) + ".write.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    try:
        # Inspect size without reading a byte another process may have locked.
        if os.fstat(handle.fileno()).st_size == 0:
            handle.write(b"0")
            handle.flush()
        deadline = time.monotonic() + timeout
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as error:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"Another writer holds {lock_path}") from error
                time.sleep(0.25)
        yield
    finally:
        handle.close()


def atomic_publish(temporary_path, database_path, timeout=30):
    deadline = time.monotonic() + timeout
    while True:
        try:
            os.replace(temporary_path, database_path)
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.25)

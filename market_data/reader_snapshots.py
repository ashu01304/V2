"""Reusable reader copies with locked ownership and automatic cleanup."""

import atexit
import os
from pathlib import Path
import re
import shutil
import tempfile
import threading
import time
from uuid import uuid4


_DIRECTORY_PREFIX = "market_data_readers_v2_"
_OWNER_CONTENT = b"market-data-reader-snapshot-v2\n"
_IDLE_SECONDS = 120
_CLEANUP_SECONDS = 60
_directory = None
_owner = None
_lock = threading.Lock()
_current = {}
_entries = {}
_stop_cleanup = threading.Event()


def _lock_owner(handle):
    """The OS releases this lock even if a dashboard is forcibly stopped."""
    handle.seek(0)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _clean_abandoned():
    """Delete only marked snapshot folders whose owner lock is available."""
    root = Path(tempfile.gettempdir()).resolve()
    for directory in root.glob(_DIRECTORY_PREFIX + "*"):
        if directory.is_symlink() or not directory.is_dir():
            continue
        if directory.resolve().parent != root:
            continue
        marker = directory / ".owner"
        if marker.is_symlink():
            continue
        try:
            with marker.open("r+b") as handle:
                if handle.read() != _OWNER_CONTENT:
                    continue
                _lock_owner(handle)
                files = list(directory.iterdir())
                # Leave any unexpected contents untouched.
                if any(file.is_symlink() or not file.is_file()
                       or (file.name != ".owner"
                           and not re.fullmatch(r"[a-f0-9]{32}\.duckdb", file.name))
                       for file in files):
                    continue
                for file in files:
                    if file.name != ".owner":
                        file.unlink(missing_ok=True)
            marker.unlink(missing_ok=True)
            directory.rmdir()
        except OSError:
            # Active owners, concurrent cleaners, and locked files are skipped.
            continue


def _signature(path):
    stat = path.stat()
    return (stat.st_mtime_ns, stat.st_size, stat.st_ino)


def _discard(key):
    entry = _entries[key]
    if entry["readers"] == 0 and key not in _current.values():
        try:
            entry["path"].unlink(missing_ok=True)
        except OSError:
            return
        del _entries[key]


def _housekeeping():
    while not _stop_cleanup.wait(_CLEANUP_SECONDS):
        with _lock:
            now = time.monotonic()
            for key, entry in list(_entries.items()):
                if entry["readers"] == 0 and now - entry["idle_since"] >= _IDLE_SECONDS:
                    source = key[0]
                    if _current.get(source) == key:
                        del _current[source]
                    _discard(key)
        _clean_abandoned()


def _shutdown():
    _stop_cleanup.set()
    if _owner is not None:
        _owner.close()
    if _directory is not None:
        _directory.cleanup()


def _ensure_directory():
    global _directory, _owner
    if _directory is not None:
        return
    _clean_abandoned()
    directory = tempfile.TemporaryDirectory(
        prefix=f"{_DIRECTORY_PREFIX}{os.getpid()}_", ignore_cleanup_errors=True
    )
    try:
        owner = (Path(directory.name) / ".owner").open("w+b")
        try:
            owner.write(_OWNER_CONTENT)
            owner.flush()
            _lock_owner(owner)
        except BaseException:
            owner.close()
            raise
    except BaseException:
        directory.cleanup()
        raise
    _directory, _owner = directory, owner
    atexit.register(_shutdown)
    threading.Thread(target=_housekeeping, name="reader-snapshot-cleanup",
                     daemon=True).start()


def acquire_snapshot(path):
    """Reuse a snapshot while protecting it from other processes' cleanup."""
    source = Path(path).resolve()
    with _lock:
        signature = _signature(source)
        key = (str(source), signature)
        if key not in _entries:
            _ensure_directory()
            # Remove obsolete idle generations before allocating another copy.
            old_key = _current.get(str(source))
            if old_key is not None and old_key != key and _entries[old_key]["readers"] == 0:
                del _current[str(source)]
                _discard(old_key)
            snapshot = Path(_directory.name) / f"{uuid4().hex}.duckdb"
            try:
                for attempt in range(3):
                    before = _signature(source)
                    # An earlier failed consistency check may have left a copy.
                    snapshot.unlink(missing_ok=True)
                    required = before[1] + 64 * 1024 * 1024
                    if shutil.disk_usage(_directory.name).free < required:
                        raise OSError(
                            28, f"Not enough disk space for a market-data snapshot: "
                            f"need {required / 1024**3:.2f} GiB free in "
                            f"{tempfile.gettempdir()}. Close unused dashboards "
                            f"or free space on that drive."
                        )
                    shutil.copyfile(source, snapshot)
                    after = _signature(source)
                    if before == after:
                        key = (str(source), after)
                        break
                else:
                    raise RuntimeError(
                        "Market database changed repeatedly while making a reader snapshot"
                    )
            except BaseException:
                snapshot.unlink(missing_ok=True)
                raise
            if key in _entries:
                snapshot.unlink(missing_ok=True)
            else:
                _entries[key] = {"path": snapshot, "readers": 0,
                                 "idle_since": time.monotonic()}
        old_key = _current.get(str(source))
        _current[str(source)] = key
        if old_key is not None and old_key != key:
            _discard(old_key)
        _entries[key]["readers"] += 1
        return key, _entries[key]["path"]


def release_snapshot(key):
    with _lock:
        entry = _entries[key]
        entry["readers"] -= 1
        if entry["readers"] == 0:
            entry["idle_since"] = time.monotonic()
        _discard(key)

"""Back up, validate and publish the selected-product master. Run only on request."""

from datetime import datetime, timezone
import argparse
import hashlib
import json
from pathlib import Path
import shutil
from uuid import uuid4

from .config import DATABASE_PATH
from .database import MasterDatabase, build_filtered_database
from .publication import writer_lock, atomic_publish


def digest(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(8 * 1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def cleanup(resume_backup=None):
    master = DATABASE_PATH.resolve()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S") + "_" + uuid4().hex[:8]
    backup_dir = master.parent / "backups"
    backup_dir.mkdir(exist_ok=True)
    backup = backup_dir / f"master_database_before_cleanup_{stamp}.duckdb"
    filtered = master.parent / f".master_database_cleanup_{stamp}.duckdb"
    report_path = backup_dir / f"cleanup_{stamp}.json"
    with writer_lock(master):
        if not master.is_file():
            raise FileNotFoundError(master)
        original_bytes = master.stat().st_size
        if shutil.disk_usage(master.parent).free < 4 * original_bytes:
            raise RuntimeError("Insufficient free space for backup, rebuilt database and temporary work")
        if resume_backup:
            candidate = Path(resume_backup).resolve()
            if candidate.parent != backup_dir.resolve() or not candidate.name.startswith("master_database_before_cleanup_"):
                raise ValueError("Resume backup must be a cleanup backup in data/backups")
            if digest(candidate) == digest(master):
                backup = candidate
                print(f"Reusing verified backup {backup}", flush=True)
            else:
                print("Master has advanced since the previous attempt; taking a fresh backup.", flush=True)
        if not backup.exists():
            print(f"Backing up master to {backup}", flush=True)
            shutil.copy2(master, backup)
        backup_hash = digest(backup)
        if backup_hash != digest(master):
            raise RuntimeError("Backup checksum mismatch; master was not changed")
        print("Backup checksum verified.", flush=True)
        report = build_filtered_database(backup, filtered)
        reopened = MasterDatabase(filtered, read_only=True)
        try:
            if reopened.row_counts() != (report["spread_prices"]["kept"], report["seac_settlements"]["kept"]):
                raise RuntimeError("Reopened database verification failed; master was not changed")
        finally:
            reopened.close()
        report.update(backup=str(backup), backup_sha256=backup_hash, master=str(master),
                      original_bytes=original_bytes, cleaned_bytes=filtered.stat().st_size, published=False)
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        # Detect writers outside the shared writer lock before replacement.
        if digest(master) != backup_hash:
            raise RuntimeError("Master changed during cleanup; backup and filtered copy retained without publication")
        print("Publishing verified filtered database...", flush=True)
        atomic_publish(filtered, master)
        report["published"] = True
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps({**report, "report_path": str(report_path)}, indent=2), flush=True)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resume-backup", help="Reuse an earlier cleanup backup only if it still matches the master")
    cleanup(parser.parse_args().resume_backup)

"""Replace isolated minute spikes with the next neighboring price; keep an audit CSV."""

import argparse
import logging
from pathlib import Path
import re
import shutil
import sys
from uuid import uuid4

import pandas as pd

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from market_data.config import DATABASE_PATH
from market_data.database import MasterDatabase
from market_data.force_refresh_minutes import refresh_window
from market_data.publication import atomic_publish, writer_lock
from market_data.minute_quality import spike_replacements


logger = logging.getLogger(__name__)
COLUMNS = ['timestamp', 'instrument', 'original', 'replacement']


def find_spikes(instrument, frame):
    """Conservative heuristic: a large one-point jump with an immediate reversal."""
    product = re.match(r'([^0-9]+?)[FGHJKMNQUVXZ]\d', instrument).group(1)
    tick = {'HO': .0001, 'RB': .0001, 'NG': .001, 'LGO': .25}.get(product, .01)
    result = spike_replacements(frame, tick)
    result['instrument'] = instrument
    return result[COLUMNS]


def clean(database_path=DATABASE_PATH, *, apply=False, now=None):
    target = Path(database_path).resolve()
    start, end = refresh_window(now)
    token = uuid4().hex
    report = target.parent / f'minute_spike_corrections_{token}.csv'
    backup = target.with_name(f'{target.stem}.before_spike_cleanup_{token}.duckdb')
    working = target.parent / f'.spike_cleanup_{token}.duckdb'
    # Hold the writer lock for the scan so the audit matches the database version.
    with writer_lock(target):
        reader = MasterDatabase(target, read_only=True)
        frames = []
        try:
            for index, (instrument, frame) in enumerate(reader.minute_cleanup_frames(
                    start.to_pydatetime(), end.to_pydatetime()), 1):
                corrections = find_spikes(instrument, frame)
                if not corrections.empty:
                    frames.append(corrections)
                if index % 100 == 0:
                    logger.info('Scanned %s instruments; flagged %s isolated points',
                                index, sum(len(value) for value in frames))
        finally:
            reader.close()
        corrections = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=COLUMNS)
        corrections.to_csv(report, index=False)
        logger.info('Flagged %s isolated points; audit: %s', len(corrections), report)
        if apply and not corrections.empty:
            shutil.copy2(target, backup)
            logger.info('Backup: %s', backup)
            try:
                shutil.copy2(backup, working)
                database = MasterDatabase(working)
                try:
                    updated = database.replace_minute_spikes(corrections)
                    if updated != len(corrections):
                        raise RuntimeError('Correction count mismatch; master was not changed')
                    database.finish()
                finally:
                    database.close()
                atomic_publish(working, target)
            finally:
                working.unlink(missing_ok=True)
            logger.info('Published %s corrections', updated)
    return {'count': len(corrections), 'report': report, 'backup': backup if apply and frames else None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='Apply corrections; otherwise only write a preview CSV.')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
    clean(apply=args.apply)


if __name__ == '__main__':
    main()

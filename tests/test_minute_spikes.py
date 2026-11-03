import unittest
from unittest.mock import patch

import pandas as pd

from market_data.clean_minute_spikes import find_spikes
from market_data.database import MasterDatabase
from market_data.data_client import DataClient
from tests.helpers import memory_reader


class MinuteSpikeTests(unittest.TestCase):
    def frame(self, prices):
        return pd.DataFrame({'timestamp': pd.date_range('2026-10-01', periods=len(prices), freq='min', tz='UTC'),
                             'price': prices})

    def test_isolated_spike_uses_next_neighbor(self):
        frame = self.frame([1.] * 15 + [99.] + [1.01] * 15)
        result = find_spikes('CLZ26-F27', frame)
        self.assertEqual(result.original.tolist(), [99.])
        self.assertEqual(result.replacement.tolist(), [1.01])

    def test_sustained_move_and_session_gap_are_preserved(self):
        self.assertTrue(find_spikes('CLZ26-F27', self.frame([1.] * 15 + [2.] * 16)).empty)
        frame = self.frame([1.] * 15 + [99.] + [1.] * 15)
        frame.loc[16:, 'timestamp'] += pd.Timedelta(days=1)
        self.assertTrue(find_spikes('CLZ26-F27', frame).empty)

    def test_database_update_preserves_source_marker(self):
        database = MasterDatabase(':memory:')
        try:
            database._connection.execute("INSERT INTO spread_prices VALUES ('2026-10-01 00:15:00Z', 'CLZ26-F27', 99)")
            database._connection.execute("INSERT INTO live_spread_keys VALUES ('2026-10-01 00:15:00Z', 'CLZ26-F27')")
            result = find_spikes('CLZ26-F27', self.frame([1.] * 15 + [99.] + [1.01] * 15))
            self.assertEqual(database.replace_minute_spikes(result), 1)
            self.assertEqual(database._connection.execute('SELECT price FROM spread_prices').fetchone()[0], 1.01)
            self.assertEqual(database._connection.execute('SELECT COUNT(*) FROM live_spread_keys').fetchone()[0], 1)
        finally:
            database.close()

    def test_extractor_drops_combined_spikes_before_ohlc_without_writing(self):
        database = MasterDatabase(':memory:')
        try:
            database.register_spreads(['CLZ26-F27', 'LCOZ6-F7'])
            cl = self.frame([11.] * 15 + [109.] + [11.01] * 15)
            cl['instrument'] = 'CLZ26-F27'
            co = self.frame([10.] * 31)
            co['instrument'] = 'LCOZ6-F7'
            database.save_spreads(pd.concat([cl, co], ignore_index=True))
            with DataClient(database=memory_reader(database._connection)) as client:
                args = dict(start='2026-10-01', end='2026-10-02')
                clean = client.minute('CL-CO Z26 1MS', interval=None, **args)
                candles = client.minute('CL-CO Z26 1MS', interval='1d', **args)
                raw = client.minute('CL-CO Z26 1MS', interval=None, normalize_spikes=False, **args)
            self.assertEqual(clean['diagnostics']['normalized_spikes'], 1)
            self.assertEqual(clean['diagnostics']['dropped_spikes'], 1)
            self.assertEqual(len(clean['data']), 30)
            self.assertNotIn(pd.Timestamp('2026-10-01T00:15Z'), clean['data'].timestamp.tolist())
            self.assertAlmostEqual(candles['data'].high.iloc[0], 1.01)
            self.assertAlmostEqual(raw['data'].price.iloc[15], 99)
            self.assertEqual(database._connection.execute("SELECT MAX(price) FROM spread_prices").fetchone()[0], 109)
        finally:
            database.close()

    def test_8057_uses_same_minute_source_options_as_8060(self):
        from dashboard_components.seac_experiment import daily_minute_bars
        from candle_dashboard import plot_candles
        database = MasterDatabase(':memory:')
        try:
            database.register_spreads(['CLZ26-F27'])
            frame = self.frame(list(range(15)) + [99.] + list(range(15, 30)))
            frame['instrument'] = 'CLZ26-F27'
            database.save_spreads(frame)
            database._connection.execute("INSERT INTO live_spread_keys VALUES ('2026-10-01 00:16:00Z','CLZ26-F27')")
            reader = memory_reader(database._connection)
            daily_minute_bars.cache_clear()
            with patch('dashboard_components.seac_experiment.DataClient',
                       side_effect=lambda: DataClient(database=reader)):
                bars, message = daily_minute_bars('CL', 'Z26-F27', '2026-10-01', '2026-12-01', ())
            with DataClient(database=reader) as client:
                figure, _, _ = plot_candles('CL', 'Z26', 'MS', 1, '',
                                           '2026-10-01', '2026-10-01', '1d', client=client)
            for column in ['open', 'high', 'low', 'close']:
                self.assertEqual(bars[column].tolist(), list(getattr(figure.data[0], column)))
            self.assertEqual(bars.low.tolist(), [3])
            self.assertEqual(bars.high.tolist(), [27])
            self.assertIn('spikes dropped: 0', message)
            self.assertIn('P10–P90 per day', message)
        finally:
            daily_minute_bars.cache_clear()
            database.close()

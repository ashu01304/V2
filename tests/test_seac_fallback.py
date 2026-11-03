import unittest

import pandas as pd
import plotly.graph_objects as go

from dashboard_components.seac_experiment import add_seac_overlay


class SeacFallbackTests(unittest.TestCase):
    def test_fallback_preserves_prepared_values_dates_and_expiry_axis(self):
        frame = pd.DataFrame({'value': [0., .1],
                              'date': pd.to_datetime(['2026-10-29', '2026-10-30'])}, index=[-150, -149])
        original = frame.copy(deep=True)
        figure = go.Figure()
        self.assertTrue(add_seac_overlay(figure, frame, 2027))
        self.assertEqual(list(figure.data[0].x), [-150, -149])
        self.assertEqual(list(figure.data[0].y), [0., .1])
        self.assertEqual(list(figure.data[0].customdata), ['2026-10-29', '2026-10-30'])
        self.assertEqual(figure.data[0].name, '2027 SEAC')
        self.assertEqual(figure.data[0].visible, 'legendonly')
        self.assertTrue(figure.data[0].showlegend)
        pd.testing.assert_frame_equal(frame, original)

    def test_excluded_or_missing_settlement_series_is_not_added(self):
        figure = go.Figure()
        self.assertFalse(add_seac_overlay(figure, None, 2027))
        self.assertFalse(add_seac_overlay(figure, pd.DataFrame(), 2027))
        self.assertFalse(figure.data)

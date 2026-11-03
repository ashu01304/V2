import unittest

import pandas as pd
import plotly.graph_objects as go

from dashboard_components.seac_experiment import add_statistics, rolling_statistics


class CurrentYearStatisticsTests(unittest.TestCase):
    def test_current_day_excluded_with_full_previous_calendar_window(self):
        source = pd.DataFrame({2027: [2., 4., 1000.]}, index=[-2, -1, 0])
        mean, sd = rolling_statistics(source, [0], 2, include_current=False)
        self.assertEqual(mean.iloc[0], 3)
        self.assertAlmostEqual(sd.iloc[0], 2 ** .5)
        # Today's price is excluded even after a live refresh changes it.
        source.loc[0, 2027] = -1000
        new_mean, new_sd = rolling_statistics(source, [0], 2, include_current=False)
        pd.testing.assert_series_equal(mean, new_mean)
        pd.testing.assert_series_equal(sd, new_sd)

    def test_initial_current_year_band_uses_prior_days(self):
        source = pd.DataFrame({2026: [10., 12., 14.], 2027: [2., 4., 1000.]}, index=[-2, -1, 0])
        figure = go.Figure()
        stats = add_statistics(figure, source, ['current'], 2, 2, source,
                               current_year=2027, current_index=[0], current_multiplier=2)
        upper, lower = [figure.data[index].y[0] for index in stats['currentTraces']]
        self.assertAlmostEqual(upper, 3 + 2 * 2 ** .5)
        self.assertAlmostEqual(lower, 3 - 2 * 2 ** .5)

    def test_other_year_windows_still_include_current_point(self):
        source = pd.DataFrame({2026: [2., 4., 8.]}, index=[-2, -1, 0])
        mean, _ = rolling_statistics(source, [0], 2)
        self.assertEqual(mean.iloc[0], 6)

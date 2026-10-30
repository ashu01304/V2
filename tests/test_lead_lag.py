"""Neighbour lag, exact history grids and chronological forecast validation."""

import unittest

import duckdb
import numpy as np
import pandas as pd

from analysis.lead_lag import neighbour_indices, neighbour_movements, neighbour_summary, fit_neighbour_forecasts, neighbour_figure
from market_data.workflow_client import load_snapshot_history
from tests.helpers import memory_reader


def predictable_history(n=500, reverse_at=None):
    rng = np.random.default_rng(32)
    neighbour_moves = rng.choice([-.04, -.02, 0, .02, .04], size=n)
    target_moves = np.zeros(n)
    for index in range(1, n):
        sign = -1 if reverse_at is not None and index >= reverse_at else 1
        target_moves[index] = sign * (neighbour_moves[index-1] - target_moves[index-1])
    values = np.column_stack([np.cumsum(target_moves), *[np.cumsum(neighbour_moves) for _ in range(4)]])
    return pd.DataFrame(values, index=pd.date_range("2026-10-01", periods=n, freq="15min", tz="UTC"),
                        columns=["Target", "N1", "N2", "N3", "N4"])


class NeighbourLagTests(unittest.TestCase):
    def test_readable_months_leave_model_identifiers_unchanged(self):
        from analysis.expression import format_contract_label

        self.assertEqual(format_contract_label("Z26-F27"), "Dec 26-Jan 27")
        self.assertEqual(format_contract_label("Target"), "Target")
        labels = ["Z26-F27", "F27-G27", "G27-H27"]
        frame = neighbour_summary(labels, {0: [.1]*3, 15: [0]*3, 30: [0]*3})
        self.assertEqual(neighbour_figure(frame).data[0].x[0], "Dec 26-Jan 27")
        self.assertEqual(frame.Spread.tolist(), labels)

    def setUp(self):
        self.labels = ["Target", "N1", "N2", "N3", "N4"]
        self.snapshots = {0: [.01, .04, .04, .04, .04], 15: [0]*5, 30: [-.01, -.04, -.04, -.04, -.04]}

    def test_known_gap_and_30_minute_agreement(self):
        frame = neighbour_summary(self.labels, self.snapshots)
        row = frame.iloc[0]
        self.assertAlmostEqual(row.NeighbourMove15, .04)
        self.assertAlmostEqual(row.OwnMove15, .01)
        self.assertAlmostEqual(row.Gap15, .03)
        self.assertAlmostEqual(row.Gap30, .06)
        self.assertEqual(row.Agreement30, "Agrees")
        self.assertEqual(row.Neighbours15, 4)
        self.assertEqual(len(neighbour_figure(frame).data), 1)

    def test_neighbours_exclude_self_and_never_replace_missing_nearby_maturities(self):
        indices = neighbour_indices(7)
        self.assertEqual(indices[3], [2, 4, 1, 5])
        self.assertNotIn(3, indices[3])
        changes = np.array([[1., np.nan, np.nan, .1, np.nan, .2, 2.]])
        moves, counts = neighbour_movements(changes)
        self.assertEqual(counts[0, 3], 1)
        self.assertTrue(np.isnan(moves[0, 3]))
        first = neighbour_summary(self.labels, self.snapshots)
        self.snapshots[0][0] += 1
        second = neighbour_summary(self.labels, self.snapshots)
        self.assertEqual(first.iloc[0].NeighbourMove15, second.iloc[0].NeighbourMove15)

    def test_missing_prices_and_unsupported_forecasts_remain_blank(self):
        trained = {"labels": self.labels, "models": [{"Spread": "Target", "Accepted": True,
            "Intercept": .001, "Slope": .5, "GapMin": -.1, "GapMax": .1, "Status": "accepted"}]}
        frame = neighbour_summary(self.labels, self.snapshots, trained)
        self.assertAlmostEqual(frame.iloc[0].Forecast15, .016)
        self.assertEqual(len(neighbour_figure(frame).data), 2)
        trained["models"][0]["GapMax"] = .02
        self.assertTrue(neighbour_summary(self.labels, self.snapshots, trained).Forecast15.isna().all())
        trained["labels"] = ["different"]
        self.assertIn("Retrain", neighbour_summary(self.labels, self.snapshots, trained).iloc[0].ForecastStatus)
        self.snapshots[15][0] = None
        row = neighbour_summary(self.labels, self.snapshots).iloc[0]
        self.assertTrue(np.isnan(row.Gap15))
        self.assertEqual(row.Agreement30, "Missing data")

    def test_regression_learns_known_predictable_movement_with_purged_holdout(self):
        models = fit_neighbour_forecasts(predictable_history())
        target = models[0]
        self.assertTrue(target["Accepted"])
        self.assertAlmostEqual(target["Intercept"], 0, places=10)
        self.assertAlmostEqual(target["Slope"], 1, places=10)
        self.assertLess(target["TestMAE"], target["NoChangeMAE"])
        self.assertLess(pd.Timestamp(target["TrainLastLabel"]), pd.Timestamp(target["TestFirstDecision"]))
        self.assertEqual(target["Samples"], 498)
        self.assertGreaterEqual(target["TestSamples"], 20)

    def test_failed_holdout_constant_gaps_and_short_history_do_not_produce_forecasts(self):
        reverse = fit_neighbour_forecasts(predictable_history(reverse_at=375))[0]
        self.assertFalse(reverse["Accepted"])
        self.assertGreaterEqual(reverse["TestMAE"], reverse["NoChangeMAE"])
        flat = predictable_history()*0
        self.assertTrue(all(not model["Accepted"] for model in fit_neighbour_forecasts(flat)))
        self.assertTrue(all(not model["Accepted"] for model in fit_neighbour_forecasts(flat.iloc[:40])))
        gap = predictable_history().drop(predictable_history().index[100])
        self.assertEqual(fit_neighbour_forecasts(gap)[0]["Samples"], 495)

    def test_reader_queries_exact_grid_without_filling_missing_minutes(self):
        connection = duckdb.connect(":memory:")
        reader = memory_reader(connection)
        try:
            connection.execute("CREATE TABLE spread_prices(timestamp TIMESTAMPTZ, instrument VARCHAR, price DOUBLE)")
            connection.executemany("INSERT INTO spread_prices VALUES (?, ?, ?)", [
                ("2026-10-19T12:00:00Z", "CLZ26-F27", .5), ("2026-10-19T12:00:00Z", "LCOZ6-F7", .2),
                ("2026-10-19T12:15:00Z", "CLZ26-F27", .6), ("2026-10-19T12:16:00Z", "LCOZ6-F7", .2),
                ("2026-10-19T12:30:00Z", "CLZ26-F27", .7), ("2026-10-19T12:30:00Z", "LCOZ6-F7", .3)])
            frame = load_snapshot_history([("spread", "CL-CO", "Z26-F27")],
                "2026-10-19T12:00:00Z", "2026-10-19T12:30:00Z", database=reader)
            self.assertEqual(len(frame), 3)
            self.assertAlmostEqual(frame.iloc[0, 0], .3)
            self.assertTrue(np.isnan(frame.iloc[1, 0]))
            self.assertAlmostEqual(frame.iloc[2, 0], .4)
        finally:
            reader.close()


if __name__ == "__main__":
    unittest.main()

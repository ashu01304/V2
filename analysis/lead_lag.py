"""Neighbour-movement lag and a small, time-validated 15-minute regression."""

import numpy as np
import pandas as pd
import plotly.graph_objects as go

from .expression import format_contract_label


NEIGHBOURS = 4
MIN_NEIGHBOURS = 2


def neighbour_indices(count):
    """Fixed nearest maturities, excluding the target and never replacing gaps."""
    return [sorted((other for other in range(count) if other != target),
                   key=lambda other: (abs(other-target), other))[:NEIGHBOURS]
            for target in range(count)]


def neighbour_movements(changes):
    changes = np.asarray(changes, dtype=float)
    if changes.ndim != 2:
        raise ValueError("Changes must be a time-by-contract matrix")
    moves = np.full(changes.shape, np.nan)
    counts = np.zeros(changes.shape, dtype=int)
    for target, neighbours in enumerate(neighbour_indices(changes.shape[1])):
        if not neighbours: continue
        values = np.ma.masked_invalid(changes[:, neighbours])
        counts[:, target] = np.isfinite(changes[:, neighbours]).sum(axis=1)
        medians = np.ma.median(values, axis=1).filled(np.nan)
        moves[:, target] = np.where(counts[:, target] >= MIN_NEIGHBOURS, medians, np.nan)
    return moves, counts


def neighbour_summary(labels, snapshots, trained=None):
    live = np.array(snapshots.get(0, [None]*len(labels)), dtype=float)
    frame = pd.DataFrame({"Spread": labels})
    for age in (15, 30):
        past = np.array(snapshots.get(age, [None]*len(labels)), dtype=float)
        own = live - past
        neighbours, counts = neighbour_movements(own[None, :])
        frame[f"OwnMove{age}"] = own
        frame[f"NeighbourMove{age}"] = neighbours[0]
        frame[f"Neighbours{age}"] = counts[0]
        frame[f"Gap{age}"] = neighbours[0] - own
    def agreement(row):
        a, b = row["Gap15"], row["Gap30"]
        if not np.isfinite(a) or not np.isfinite(b): return "Missing data"
        if abs(a) <= 1e-10 or abs(b) <= 1e-10: return "Neutral"
        return "Agrees" if a*b > 0 else "Opposite"
    frame["Agreement30"] = frame.apply(agreement, axis=1)
    estimates, statuses = [], []
    same_range = trained and trained.get("labels") == list(labels)
    models = {row["Spread"]: row for row in trained.get("models", [])} if same_range else {}
    for row in frame.to_dict("records"):
        gap, model = row["Gap15"], models.get(row["Spread"])
        forecast = None
        if not np.isfinite(gap): status = "Missing prices"
        elif trained and not same_range: status = "Retrain for this month range"
        elif model is None: status = "Not trained"
        elif not model["Accepted"]: status = model["Status"]
        elif not model["GapMin"] - 1e-10 <= gap <= model["GapMax"] + 1e-10:
            status = "Outside historical lag range"
        else:
            forecast = model["Intercept"] + model["Slope"]*gap
            status = "Beats no-change on holdout"
        estimates.append(forecast)
        statuses.append(status)
    frame["Forecast15"] = estimates
    frame["ForecastStatus"] = statuses
    return frame


def fit_neighbour_forecasts(history, min_samples=80):
    """Per-spread next-15m change = intercept + slope * lag, with a purged time split."""
    if not isinstance(history.index, pd.DatetimeIndex) or history.index.tz is None:
        raise ValueError("History must have a timezone-aware timestamp index")
    if history.index.has_duplicates:
        raise ValueError("History timestamps must be unique")
    if any(timestamp != timestamp.floor("15min") for timestamp in history.index):
        raise ValueError("History must use exact 15-minute snapshots")
    if history.empty:
        return [{"Spread": column, "Samples": 0, "TrainSamples": 0, "TestSamples": 0,
                 "Accepted": False, "Status": "Insufficient history"} for column in history.columns]
    # Match the two-decimal observed prices on the dashboard. Never bridge missing grid points.
    prices = history.sort_index().asfreq("15min").astype(float).round(2)
    prices = prices.where(np.isfinite(prices))
    changes = prices.diff()
    neighbour_move, _counts = neighbour_movements(changes.to_numpy())
    gaps = neighbour_move - changes.to_numpy()
    future_changes = prices.shift(-1) - prices
    models = []
    for target, spread in enumerate(prices.columns):
        samples = pd.DataFrame({"Gap": gaps[:, target], "Outcome": future_changes.iloc[:, target]}, index=prices.index).dropna()
        row = {"Spread": spread, "Samples": len(samples), "TrainSamples": 0, "TestSamples": 0,
               "Accepted": False, "Status": "Insufficient history", "Intercept": None, "Slope": None,
               "TestMAE": None, "NoChangeMAE": None, "GapMin": None, "GapMax": None}
        models.append(row)
        if len(samples) < min_samples: continue
        split = int(len(samples)*.75)
        test = samples.iloc[split:]
        # Remove training rows whose future label reaches the first test decision time.
        train = samples.iloc[:split]
        train = train[train.index + pd.Timedelta(minutes=15) < test.index[0]]
        row.update(TrainSamples=len(train), TestSamples=len(test))
        if len(train) < 50 or len(test) < 20: continue
        design = np.column_stack([np.ones(len(train)), train.Gap])
        coefficients, _, rank, _ = np.linalg.lstsq(design, train.Outcome.to_numpy(), rcond=None)
        if rank < 2:
            row["Status"] = "Insufficient lag variation"
            continue
        predicted = coefficients[0] + coefficients[1]*test.Gap.to_numpy()
        mae = float(np.mean(np.abs(predicted-test.Outcome.to_numpy())))
        baseline = float(np.mean(np.abs(test.Outcome.to_numpy())))
        accepted = mae + 1e-12 < baseline
        final_design = np.column_stack([np.ones(len(samples)), samples.Gap])
        final, _, rank, _ = np.linalg.lstsq(final_design, samples.Outcome.to_numpy(), rcond=None)
        if rank < 2: continue
        row.update(Intercept=float(final[0]), Slope=float(final[1]), TestMAE=mae, NoChangeMAE=baseline,
                   GapMin=float(samples.Gap.min()), GapMax=float(samples.Gap.max()), Accepted=accepted,
                   Status="Beats no-change on holdout" if accepted else "Did not beat no-change",
                   TrainLastLabel=(train.index[-1]+pd.Timedelta(minutes=15)).isoformat(),
                   TestFirstDecision=test.index[0].isoformat())
    return models


def neighbour_figure(frame):
    frame = frame.copy()
    frame["Spread"] = frame["Spread"].map(format_contract_label)
    figure = go.Figure()
    details = np.column_stack([frame.OwnMove15, frame.NeighbourMove15, frame.Neighbours15,
                               frame.Gap30, frame.Agreement30])
    figure.add_trace(go.Bar(x=frame.Spread, y=frame.Gap15, name="15m neighbour lag",
        marker_color=["#4ade80" if value > 1e-10 else "#f87171" if value < -1e-10 else "#94a3b8" for value in frame.Gap15],
        customdata=details, hovertemplate="%{x}<br>Lag gap: %{y:.4f}<br>Own move: %{customdata[0]:.4f}"
        "<br>Neighbour move: %{customdata[1]:.4f}<br>Neighbours available: %{customdata[2]}"
        "<br>30m gap: %{customdata[3]:.4f}<br>30m check: %{customdata[4]}<extra></extra>"))
    if pd.to_numeric(frame.Forecast15).notna().any():
        figure.add_trace(go.Scatter(x=frame.Spread, y=frame.Forecast15, mode="lines+markers", connectgaps=False,
            name="Estimated next 15m change", line={"color": "#fbbf24", "dash": "dot"},
            hovertemplate="%{x}<br>Forecast change: %{y:.4f}<extra></extra>"))
    figure.update_layout(title="15-minute neighbour lag", template="plotly_dark", paper_bgcolor="#111827", plot_bgcolor="#111827",
        height=440, margin={"l": 65, "r": 25, "t": 75, "b": 95}, dragmode="zoom",
        legend={"orientation": "h", "y": 1.03}, uirevision="neighbours:" + ":".join(frame.Spread),
        xaxis={"title": "Calendar spread", "type": "category", "categoryorder": "array", "categoryarray": frame.Spread.tolist(), "tickangle": -45},
        yaxis={"title": "Price change", "zerolinecolor": "#94a3b8", "tickformat": ".4f"})
    if not np.isfinite(frame.Gap15).any():
        figure.add_annotation(text="Waiting for matching live and historical neighbour prices", x=.5, y=.5,
                              xref="paper", yref="paper", showarrow=False)
    return figure

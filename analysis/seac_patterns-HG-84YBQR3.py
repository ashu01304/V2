import os
from concurrent.futures import ProcessPoolExecutor, as_completed

import numpy as np
import pandas as pd
import umap
from lightgbm import LGBMClassifier, LGBMRegressor
from sklearn.cluster import HDBSCAN
from sklearn.ensemble import ExtraTreesClassifier, ExtraTreesRegressor
from sklearn.linear_model import LogisticRegression, Ridge
from sklearn.metrics import brier_score_loss
from sklearn.base import clone

from analysis.seasonality import Seasonality
from analysis.contract_universe import load_universe as load_generated_universe
from analysis.expression import parse_expression, shift_contract_year
from analysis.rollover import StrategyRollover
from market_data import MarketData


def load_universe(path=None):
    return load_generated_universe(path)


def available_products():
    database = MarketData()
    try:
        return [row[0] for row in database.connection.execute(
            "SELECT DISTINCT symbol FROM seac_settlements ORDER BY symbol"
        ).fetchall()]
    finally:
        database.close()


def scan_trade_suggestions(product="CL", years=12, horizon=10, clusters=6,
                           universe=None, progress=None, workers=None,
                           stop_before_expiry=42, max_rollover_weight=0.30):
    """Scan every CL expression and rank its current hidden-state forecast."""
    universe = universe or load_universe()
    suggestions = []
    contracts = list(universe["contracts"].items())
    worker_count = workers or min(12, os.cpu_count() or 1)
    with ProcessPoolExecutor(max_workers=worker_count) as executor:
            futures = {
                executor.submit(_scan_contract, product, contract, expressions, int(years),
                                int(horizon), int(clusters),
                                int(stop_before_expiry),
                                float(max_rollover_weight)): contract
                for contract, expressions in contracts
            }
            completed = 0
            for future in as_completed(futures):
                completed += 1
                contract = futures[future]
                try:
                    suggestions.extend(future.result())
                except Exception:
                    pass
                if progress:
                    progress(completed, len(contracts), contract,
                             _sort_suggestions(suggestions))
    if not suggestions:
        return pd.DataFrame()
    return _sort_suggestions(suggestions)


def _sort_suggestions(suggestions):
    if not suggestions:
        return pd.DataFrame()
    return pd.DataFrame(suggestions).sort_values(
        ["Quality", "Score", "Similar_Cases"],
        ascending=[True, False, False],
    ).reset_index(drop=True)


def _ensemble_prediction(features, outcome, current, pattern_labels,
                         current_pattern, pattern_probability_up, sample_years,
                         sample_days):
    """Weight models using predictions made on chronologically unseen years."""
    classifiers = {
        "LightGBM": LGBMClassifier(
            n_estimators=150, learning_rate=0.04, num_leaves=15,
            min_child_samples=30, subsample=0.8, colsample_bytree=0.8,
            reg_lambda=1.0, random_state=42, n_jobs=1, verbosity=-1,
        ),
        "Extra Trees": ExtraTreesClassifier(
            n_estimators=150, min_samples_leaf=10, max_features=0.7,
            class_weight="balanced", random_state=42, n_jobs=1,
        ),
        "Logistic": LogisticRegression(
            C=0.3, class_weight="balanced", max_iter=1000, random_state=42,
        ),
    }
    regressors = {
        "LightGBM": LGBMRegressor(
            n_estimators=150, learning_rate=0.04, num_leaves=15,
            min_child_samples=30, subsample=0.8, colsample_bytree=0.8,
            reg_lambda=1.0, random_state=42, n_jobs=1, verbosity=-1,
        ),
        "Extra Trees": ExtraTreesRegressor(
            n_estimators=150, min_samples_leaf=10, max_features=0.7,
            random_state=42, n_jobs=1,
        ),
        "Logistic": Ridge(alpha=10.0),
    }

    probabilities, expected_moves, skills = {}, {}, {}
    backtest_probabilities = {name: [] for name in [*classifiers, "HDBSCAN"]}
    backtest_actual = []
    backtest_moves, backtest_years, backtest_days = [], [], []
    years = pd.Series(sample_years, index=features.index).astype(int)
    unique_years = sorted(years.unique())
    test_years = unique_years[-min(3, max(0, len(unique_years) - 3)):]
    for test_year in test_years:
        train_mask, test_mask = years < test_year, years == test_year
        fold_x, fold_test_x = features.loc[train_mask], features.loc[test_mask]
        fold_y = (outcome.loc[train_mask] > 0).astype(int)
        fold_actual = (outcome.loc[test_mask] > 0).astype(int)
        if len(fold_x) < 50 or len(fold_test_x) < 5 or fold_y.nunique() < 2:
            continue
        fold_outputs = {}
        for name, classifier in classifiers.items():
            model = clone(classifier)
            model.fit(fold_x, fold_y)
            fold_outputs[name] = model.predict_proba(fold_test_x)[:, 1]
        pattern_rates = fold_y.groupby(pattern_labels.loc[train_mask]).mean()
        fold_outputs["HDBSCAN"] = pattern_labels.loc[test_mask].map(
            pattern_rates
        ).fillna(0.5).to_numpy()
        backtest_actual.extend(fold_actual.tolist())
        backtest_moves.extend(outcome.loc[test_mask].astype(float).tolist())
        backtest_years.extend([int(test_year)] * len(fold_actual))
        backtest_days.extend(sample_days.loc[test_mask].astype(int).tolist())
        for name, values in fold_outputs.items():
            backtest_probabilities[name].extend(values.tolist())

    if not backtest_actual:
        raise ValueError("Not enough unseen years for model backtesting")
    actual = np.asarray(backtest_actual, dtype=int)
    accuracies = {}
    for name, values in backtest_probabilities.items():
        values = np.asarray(values, dtype=float)
        skills[name] = max(0.02, 0.25 - brier_score_loss(actual, values))
        accuracies[name] = float(((values >= 0.5) == actual).mean() * 100)

    full_direction = (outcome > 0).astype(int)
    for name, classifier in classifiers.items():
        classifier.fit(features, full_direction)
        probabilities[name] = float(classifier.predict_proba(current)[0, 1] * 100)

        regressor = regressors[name]
        regressor.fit(features, outcome)
        expected_moves[name] = float(regressor.predict(current)[0])

    probabilities["HDBSCAN"] = pattern_probability_up
    current_pattern_moves = outcome[pattern_labels == current_pattern]
    expected_moves["HDBSCAN"] = float(current_pattern_moves.median())

    total_skill = sum(skills.values())
    weights = {name: skill / total_skill for name, skill in skills.items()}
    probability_up = sum(weights[name] * probabilities[name] for name in weights)
    expected_move = sum(weights[name] * expected_moves[name] for name in weights)
    ensemble_backtest = sum(
        weights[name] * np.asarray(backtest_probabilities[name], dtype=float)
        for name in weights
    )
    accuracies["Ensemble"] = float(((ensemble_backtest >= 0.5) == actual).mean() * 100)
    backtest_details = []
    for index in range(len(actual)):
        row = {
            "Test_Year": backtest_years[index],
            "Day_To_Expiry": backtest_days[index],
            "Actual_Move": backtest_moves[index],
            "Actual_Direction": "UP" if actual[index] else "DOWN",
        }
        for name, values in backtest_probabilities.items():
            probability = float(values[index] * 100)
            row[f"{name}_Probability_Up"] = probability
            row[f"{name}_Prediction"] = "UP" if probability >= 50 else "DOWN"
            row[f"{name}_Correct"] = int((probability >= 50) == bool(actual[index]))
        ensemble_probability = float(ensemble_backtest[index] * 100)
        row["Ensemble_Probability_Up"] = ensemble_probability
        row["Ensemble_Prediction"] = "UP" if ensemble_probability >= 50 else "DOWN"
        row["Ensemble_Correct"] = int(
            (ensemble_probability >= 50) == bool(actual[index])
        )
        backtest_details.append(row)
    final_long = probability_up >= 50
    agreement_count = sum((probabilities[name] >= 50) == final_long for name in weights)
    return {
        "probability_up": float(probability_up),
        "expected_move": float(expected_move),
        "agreement_count": int(agreement_count),
        "probabilities": probabilities,
        "weights_text": ", ".join(
            f"{name} {weight * 100:.0f}%" for name, weight in weights.items()
        ),
        "accuracies": accuracies,
        "backtest_predictions": len(actual),
        "backtest_details": backtest_details,
    }


def _rollover_signal(database, product, expression, horizon, maximum_weight):
    """Measure how the same structure behaved at the current rollover phase."""
    try:
        result = StrategyRollover(database).calculate(product, expression, periods=8)
        series = result.get("series", {})
        active = next((item for item in series.values() if item.get("active")), None)
        if active is None or active["data"].empty:
            return None
        latest_date = pd.Timestamp(database.cursor.execute(
            "SELECT MAX(trading_date) FROM seac_settlements WHERE symbol=?", [product]
        ).fetchone()[0]).normalize()
        active_rows = active["data"][active["data"]["date"] <= latest_date]
        if active_rows.empty:
            return None
        current_day = int(active_rows.index[-1])
        moves = []
        for item in series.values():
            if item.get("active") or item["data"].empty:
                continue
            values = item["data"]["value"].groupby(level=0).last().sort_index()
            full_index = range(int(values.index.min()), int(values.index.max()) + 1)
            values = values.reindex(full_index).interpolate(limit_area="inside")
            future_day = current_day + horizon
            if current_day in values.index and future_day in values.index:
                start, end = values.loc[current_day], values.loc[future_day]
                if pd.notna(start) and pd.notna(end):
                    moves.append(float(end - start))
        if len(moves) < 3:
            return None
        moves = pd.Series(moves)
        probability_up = float((moves > 0).mean() * 100)
        confidence = max(probability_up, 100 - probability_up)
        consistency = abs(probability_up - 50) / 50
        sample_strength = min(1.0, len(moves) / 6)
        weight = min(maximum_weight, maximum_weight * consistency * sample_strength)
        return {
            "probability_up": probability_up,
            "confidence": confidence,
            "expected_move": float(moves.median()),
            "cases": len(moves),
            "weight": float(weight),
        }
    except Exception:
        return None


def _scan_contract(product, contract, expressions, years, horizon, clusters,
                   stop_before_expiry, max_rollover_weight):
    database = MarketData()
    try:
        seasonality = _ProductSeasonality(_CachedProductData(database, product))
        return _analyse_contract(product, contract, expressions, years, horizon, clusters,
                                 stop_before_expiry, max_rollover_weight, seasonality)
    finally:
        database.close()


def _analyse_contract(product, contract, expressions, years, horizon, clusters,
                      stop_before_expiry, max_rollover_weight, seasonality):
    suggestions = []
    contract_year = 2000 + int(contract[1:])
    matrices = {}
    for strategy, expression in expressions.items():
        if not _has_complete_history(
                seasonality.db, expression, contract_year - years + 1, contract_year):
            continue
        result = seasonality.working_day_expression_seasonality(
                product, expression, contract_year - years + 1,
                contract_year, window_days=400,
        )
        if not result.get("combined", pd.DataFrame()).empty:
            matrices[strategy] = result["combined"]
    if len(matrices) < 3:
        return []
    states, feature_columns = _joint_states(matrices, window=20)
    if len(states) < clusters * 5 or not feature_columns:
        return []
    values = states[feature_columns].replace([np.inf, -np.inf], np.nan)
    values = values.fillna(values.median())
    standardized = (values - values.mean()) / values.std().replace(0, 1)
    reducer = umap.UMAP(
        n_components=min(8, len(feature_columns)),
        n_neighbors=min(30, len(states) - 1),
        min_dist=0.05,
        metric="euclidean",
        random_state=42,
        n_jobs=1,
    )
    embedding = reducer.fit_transform(standardized.to_numpy(dtype=float))
    clusterer = HDBSCAN(
        min_cluster_size=max(20, min(100, len(states) // 30)),
        min_samples=10,
        allow_single_cluster=False,
        copy=True,
    )
    states["Pattern"] = clusterer.fit_predict(embedding) + 1
    latest = states.sort_values(["Year", "Day"]).iloc[-1]
    current_pattern = int(latest["Pattern"])
    if current_pattern == 0 or int(latest["Day"]) >= -abs(stop_before_expiry):
        return []
    for strategy, matrix in matrices.items():
        outcomes = []
        for index, state in states.iterrows():
            year, day = int(state["Year"]), int(state["Day"])
            future_day = day + horizon
            if (year in matrix and day in matrix.index and future_day in matrix.index
                        and pd.notna(matrix.at[day, year])
                        and pd.notna(matrix.at[future_day, year])):
                outcomes.append((index, float(
                        matrix.at[future_day, year] - matrix.at[day, year]
                )))
        if not outcomes:
            continue
        outcome = pd.Series(dict(outcomes))
        matches = outcome[states.loc[outcome.index, "Pattern"] == current_pattern]
        match_years = states.loc[matches.index, "Year"].nunique()
        if len(matches) < 40 or match_years < 4:
            continue
        train_x = standardized.loc[outcome.index]
        train_direction = (outcome > 0).astype(int)
        if train_direction.nunique() < 2:
            continue
        current_x = standardized.loc[[latest.name]]
        pattern_probability_up = float((matches > 0).mean() * 100)
        ensemble = _ensemble_prediction(
            train_x, outcome, current_x,
            states.loc[outcome.index, "Pattern"], current_pattern,
            pattern_probability_up, states.loc[outcome.index, "Year"],
            states.loc[outcome.index, "Day"],
        )
        rollover = _rollover_signal(
            seasonality.db, product, expressions[strategy], horizon,
            max_rollover_weight,
        )
        rollover_weight = rollover["weight"] if rollover else 0.0
        probability_up = ensemble["probability_up"]
        predicted_move = ensemble["expected_move"]
        if rollover:
            probability_up = ((1 - rollover_weight) * probability_up
                              + rollover_weight * rollover["probability_up"])
            predicted_move = ((1 - rollover_weight) * predicted_move
                              + rollover_weight * rollover["expected_move"])
        confidence = max(probability_up, 100 - probability_up)
        side = "LONG" if probability_up >= 50 else "SHORT"
        pattern_confidence = max(pattern_probability_up, 100 - pattern_probability_up)
        direction_sign = 1 if side == "LONG" else -1
        model_agrees = (direction_sign * predicted_move > 0
                        and direction_sign * float(matches.median()) > 0)
        votes = list(ensemble["probabilities"].values())
        if rollover:
            votes.append(rollover["probability_up"])
        agreement_count = sum((probability >= 50) == (side == "LONG")
                              for probability in votes)
        required_votes = 4
        latest_year, latest_day = int(latest["Year"]), int(latest["Day"])
        current_value = (matrix.at[latest_day, latest_year]
                         if latest_year in matrix and latest_day in matrix.index
                         else np.nan)
        if pd.isna(current_value):
            continue
        suggestions.append({
                "Quality": ("TRADE" if confidence >= 70
                            and agreement_count >= required_votes
                            and pattern_confidence >= 60
                            and ensemble["accuracies"]["Ensemble"] >= 55
                            and match_years >= 5 and model_agrees else "WATCH"),
                "Side": side,
                "Contract": contract, "Structure": strategy,
                "Expression": expressions[strategy],
                "Current_Value": current_value,
                "Expected_Move": float(matches.median()),
                "ML_Expected_Move": predicted_move,
                "Probability": confidence,
                "Pattern_Probability": pattern_confidence,
                "Models_Agree": model_agrees,
                "Model_Votes": f"{agreement_count}/{len(votes)}",
                "LightGBM_Probability": ensemble["probabilities"]["LightGBM"],
                "ExtraTrees_Probability": ensemble["probabilities"]["Extra Trees"],
                "Logistic_Probability": ensemble["probabilities"]["Logistic"],
                "Validation_Weights": ensemble["weights_text"],
                "LightGBM_Accuracy": ensemble["accuracies"]["LightGBM"],
                "ExtraTrees_Accuracy": ensemble["accuracies"]["Extra Trees"],
                "Logistic_Accuracy": ensemble["accuracies"]["Logistic"],
                "HDBSCAN_Accuracy": ensemble["accuracies"]["HDBSCAN"],
                "Ensemble_Accuracy": ensemble["accuracies"]["Ensemble"],
                "Backtest_Predictions": ensemble["backtest_predictions"],
                "_Backtest_Details": ensemble["backtest_details"],
                "Rollover_Probability": (rollover["confidence"] if rollover else np.nan),
                "Rollover_Expected_Move": (rollover["expected_move"] if rollover else np.nan),
                "Rollover_Cases": (rollover["cases"] if rollover else 0),
                "Rollover_Weight": rollover_weight * 100,
                "Rollover_Used": rollover is not None,
                "Days_To_Expiry": abs(latest_day),
                "Pattern": current_pattern,
                "Similar_Cases": len(matches), "Years_Seen": int(match_years),
                "Score": (confidence - 50) * np.sqrt(match_years),
                "Model": "Validation-weighted ensemble + adaptive rollover",
                "Trade_Filter": ("Confidence >=70%; pattern >=60%; accuracy >=55%; "
                                 "at least 4 votes; >=5 years; moves agree"),
        })
    return suggestions


class _CachedProductData:
    """Use one settlement query instead of repeating thousands of DuckDB queries."""
    def __init__(self, database, product):
        self.cursor = database.cursor
        self.product = product
        frame = database.connection.execute("""
            SELECT contract_code, trading_date AS Date, price AS Close
            FROM seac_settlements WHERE symbol=? ORDER BY trading_date
        """, [product]).fetchdf()
        frame["Date"] = pd.to_datetime(frame["Date"])
        self.histories = {
            code: group.set_index("Date")[["Close"]]
            for code, group in frame.groupby("contract_code")
        }

    def get_contract_history(self, symbol, codes):
        if symbol != self.product:
            return {}
        return {code: self.histories[code] for code in codes if code in self.histories}


class _ProductSeasonality(Seasonality):
    def _contract_years(self, symbol, letter, start_year, end_year=None):
        years = sorted({2000 + int(code[1:]) for code in self.db.histories
                        if code.startswith(letter)})
        return [year for year in years
                if year >= start_year and (end_year is None or year <= end_year)]


def _has_complete_history(database, expression, start_year, end_year):
    legs = parse_expression(expression)
    if not legs:
        return False
    reference_year = int(legs[0][1][1:])
    for year in range(start_year, end_year + 1):
        required = {
            shift_contract_year(contract, year % 100 - reference_year)
            for _, contract in legs
        }
        if any(code not in database.histories for code in required):
            return False
    return True


def _joint_states(matrices, window):
    keys = sorted({(int(year), int(day))
                   for matrix in matrices.values()
                   for year in matrix.columns for day in matrix.index
                   if pd.notna(matrix.at[day, year])})
    rows = []
    for year, day in keys:
        record = {
            "Year": year, "Day": day,
            "DaysToExpiry_ctx": abs(day),
            "RolloverWindow_ctx": float(-63 <= day <= 0),
        }
        for strategy, matrix in matrices.items():
            if year not in matrix or day not in matrix.index:
                continue
            series = matrix[year].loc[:day].dropna().tail(window)
            if len(series) < window:
                continue
            std = series.std()
            record[f"{strategy}_z"] = 0.0 if not std else float(
                (series.iloc[-1] - series.mean()) / std
            )
            record[f"{strategy}_chg5"] = float(series.iloc[-1] - series.iloc[-6])
            record[f"{strategy}_vol"] = float(series.diff().std())
        rows.append(record)
    states = pd.DataFrame(rows)
    candidates = [column for column in states if column.endswith(
        ("_z", "_chg5", "_vol", "_ctx")
    )]
    features = [column for column in candidates if states[column].notna().mean() >= 0.60]
    if features:
        states = states[states[features].notna().sum(axis=1)
                        >= max(3, int(len(features) * 0.60))].copy()
    return states, features


def discover_patterns(contract, target_strategy, years=12, horizon=10,
                      clusters=6, window=20, universe=None):
    """Cluster joint states of every structure belonging to one anchor contract."""
    universe = universe or load_universe()
    expressions = universe["contracts"][contract]
    if target_strategy not in expressions:
        raise ValueError(f"Unknown strategy {target_strategy!r} for {contract}")

    anchor_year = 2000 + int(contract[1:])
    start_year = anchor_year - int(years) + 1
    database = MarketData()
    try:
        seasonality = Seasonality(database)
        matrices = {}
        warnings = []
        for strategy, expression in expressions.items():
            result = seasonality.working_day_expression_seasonality(
                "CL", expression, start_year, anchor_year, window_days=400
            )
            matrix = result.get("combined", pd.DataFrame())
            if not matrix.empty:
                matrices[strategy] = matrix
            warnings.extend(result.get("warnings", []))
    finally:
        database.close()

    if target_strategy not in matrices:
        raise ValueError("No usable SEAC history for the selected target")

    target = matrices[target_strategy]
    rows = []
    for year in target.columns:
        year = int(year)
        target_series = target[year].dropna().sort_index()
        for day in target_series.index:
            record = {"Year": year, "Day": int(day),
                      "Target_Value": float(target_series.loc[day])}
            for strategy, matrix in matrices.items():
                if year not in matrix or day not in matrix.index:
                    continue
                series = matrix[year].loc[:day].dropna().tail(window)
                if len(series) < window:
                    continue
                std = series.std()
                record[f"{strategy}_z"] = 0.0 if not std else float(
                    (series.iloc[-1] - series.mean()) / std
                )
                record[f"{strategy}_chg5"] = float(
                    series.iloc[-1] - series.iloc[max(0, len(series) - 6)]
                )
                record[f"{strategy}_vol"] = float(series.diff().std())
            future_day = day + horizon
            if (future_day in target.index
                    and pd.notna(target.at[future_day, year])):
                record["Forward_Move"] = float(
                    target.at[future_day, year] - target.at[day, year]
                )
                rows.append(record)

    samples = pd.DataFrame(rows)
    if len(samples) < clusters * 5:
        raise ValueError(f"Only {len(samples)} complete samples; reduce clusters or years")

    candidate_columns = [column for column in samples if column.endswith(
        ("_z", "_chg5", "_vol")
    )]
    feature_columns = [
        column for column in candidate_columns
        if samples[column].notna().mean() >= 0.60
    ]
    if not feature_columns:
        raise ValueError("No relationship features have sufficient historical coverage")
    minimum = max(3, int(len(feature_columns) * 0.60))
    samples = samples[samples[feature_columns].notna().sum(axis=1) >= minimum].copy()
    if len(samples) < clusters * 5:
        raise ValueError(f"Only {len(samples)} complete samples; reduce clusters or years")
    values = samples[feature_columns].replace([np.inf, -np.inf], np.nan)
    values = values.fillna(values.median())
    scale = values.std().replace(0, 1)
    standardized = (values - values.mean()) / scale
    components, explained = _pca(standardized.to_numpy(dtype=float), dimensions=8)
    labels = _kmeans(components, int(clusters))

    samples["Pattern"] = labels + 1
    samples["PC1"] = components[:, 0]
    samples["PC2"] = components[:, 1] if components.shape[1] > 1 else 0.0
    summary = samples.groupby("Pattern", as_index=False).agg(
        Occurrences=("Forward_Move", "size"),
        Years=("Year", "nunique"),
        Average_Move=("Forward_Move", "mean"),
        Median_Move=("Forward_Move", "median"),
        Probability_Up=("Forward_Move", lambda values: (values > 0).mean() * 100),
        Best_Move=("Forward_Move", "max"),
        Worst_Move=("Forward_Move", "min"),
    )
    summary["Strength"] = (
        (summary["Probability_Up"] - 50).abs()
        * np.sqrt(summary["Occurrences"])
    )
    summary = summary.sort_values("Strength", ascending=False)
    latest = samples.sort_values(["Year", "Day"]).iloc[-1]
    return {
        "samples": samples,
        "summary": summary,
        "feature_columns": feature_columns,
        "latest_pattern": int(latest["Pattern"]),
        "explained_variance": explained,
        "warnings": warnings,
        "expressions_used": list(matrices),
    }


def _pca(values, dimensions):
    _, singular, right = np.linalg.svd(values, full_matrices=False)
    count = max(1, min(dimensions, right.shape[0]))
    transformed = values @ right[:count].T
    variance = singular ** 2
    explained = variance[:count] / variance.sum() if variance.sum() else variance[:count]
    return transformed, explained


def _kmeans(values, clusters, iterations=100, seed=42):
    rng = np.random.default_rng(seed)
    centers = values[rng.choice(len(values), clusters, replace=False)].copy()
    labels = np.zeros(len(values), dtype=int)
    for _ in range(iterations):
        distances = ((values[:, None, :] - centers[None, :, :]) ** 2).sum(axis=2)
        updated_labels = distances.argmin(axis=1)
        if np.array_equal(labels, updated_labels):
            break
        labels = updated_labels
        for cluster in range(clusters):
            members = values[labels == cluster]
            centers[cluster] = members.mean(axis=0) if len(members) else values[
                rng.integers(len(values))
            ]
    return labels

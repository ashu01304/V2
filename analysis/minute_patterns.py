import json
import os
import re
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import pandas as pd
import umap
from scipy.signal import find_peaks
from sklearn.cluster import HDBSCAN

from analysis.seac_patterns import _ensemble_prediction
from market_data import MarketData


UNIVERSE_PATH = Path(__file__).resolve().parents[1] / "data" / "contracts_list.json"
PRODUCT_PATTERN = re.compile(r"^([A-Z]+)[FGHJKMNQUVXZ]\d{1,2}-")


def available_minute_products():
    database = MarketData()
    try:
        products = {
            match.group(1)
            for instrument in database.spreads("%")
            for match in [PRODUCT_PATTERN.match(instrument)]
            if match
        }
        return sorted(products)
    finally:
        database.close()


def scan_minute_suggestions(product="CL", interval="30min", history_days=150,
                            horizon_bars=6, workers=None, progress=None):
    with UNIVERSE_PATH.open(encoding="utf-8") as file:
        universe = json.load(file)
    contracts = list(universe["contracts"].items())
    worker_count = workers or min(8, os.cpu_count() or 1)
    suggestions = []
    with ProcessPoolExecutor(max_workers=worker_count) as executor:
        futures = {
            executor.submit(
                _scan_family, product, contract, expressions, interval,
                int(history_days), int(horizon_bars),
            ): contract
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
            partial = _sort_suggestions(suggestions)
            if progress:
                progress(completed, len(contracts), contract, partial)
    return _sort_suggestions(suggestions)


def _scan_family(product, contract, expressions, interval, history_days,
                 horizon_bars):
    database = MarketData()
    try:
        start = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=history_days)
        candles = {}
        for strategy, expression in expressions.items():
            try:
                minute = database.synthetic(product, expression, start=start)
            except Exception:
                continue
            if minute.empty:
                continue
            frame = (minute.set_index("timestamp")["price"].sort_index()
                     .resample(interval).ohlc().dropna())
            if len(frame) >= 100:
                candles[strategy] = frame
        if len(candles) < 3:
            return []
        states, features = _candle_states(candles)
        if len(states) < 100 or not features:
            return []
        values = states[features].replace([np.inf, -np.inf], np.nan)
        values = values.fillna(values.median())
        standardized = (values - values.mean()) / values.std().replace(0, 1)
        reducer = umap.UMAP(
            n_components=min(8, len(features)), n_neighbors=30,
            min_dist=0.05, random_state=42, n_jobs=1,
        )
        embedding = reducer.fit_transform(standardized.to_numpy(dtype=float))
        clusterer = HDBSCAN(
            min_cluster_size=max(20, min(100, len(states) // 30)),
            min_samples=10, allow_single_cluster=False, copy=True,
        )
        states["Pattern"] = clusterer.fit_predict(embedding) + 1
        latest = states.iloc[-1]
        current_pattern = int(latest["Pattern"])
        if current_pattern == 0:
            return []

        output = []
        for strategy, frame in candles.items():
            close = frame["close"].reindex(states.index)
            outcome = close.shift(-horizon_bars) - close
            valid = outcome.dropna().index
            if len(valid) < 80:
                continue
            target = outcome.loc[valid]
            pattern_labels = states.loc[valid, "Pattern"]
            matches = target[pattern_labels == current_pattern]
            match_index = states.loc[matches.index].index
            month_index = match_index.tz_localize(None) if match_index.tz is not None else match_index
            periods = month_index.to_period("M").nunique()
            if len(matches) < 30 or periods < 3:
                continue
            pattern_probability_up = float((matches > 0).mean() * 100)
            period_codes = pd.Series(
                states.loc[valid].index.year * 100 + states.loc[valid].index.month,
                index=valid,
            )
            ensemble = _ensemble_prediction(
                standardized.loc[valid], target,
                standardized.loc[[states.index[-1]]], pattern_labels,
                current_pattern, pattern_probability_up, period_codes,
                pd.Series(np.arange(len(valid)), index=valid),
            )
            probability_up = ensemble["probability_up"]
            confidence = max(probability_up, 100 - probability_up)
            side = "LONG" if probability_up >= 50 else "SHORT"
            direction = 1 if side == "LONG" else -1
            pattern_confidence = max(pattern_probability_up, 100 - pattern_probability_up)
            agreement = ensemble["agreement_count"]
            moves_agree = (direction * ensemble["expected_move"] > 0
                           and direction * float(matches.median()) > 0)
            levels = _support_resistance(frame)
            expected_size = max(abs(ensemble["expected_move"]),
                                abs(float(matches.median())))
            available_room = (levels["resistance"] - levels["current"]
                              if side == "LONG" else
                              levels["current"] - levels["support"])
            level_allows_trade = (pd.notna(available_room) and available_room > 0
                                  and available_room >= expected_size * 0.75)
            output.append({
                "Quality": ("TRADE" if confidence >= 65 and pattern_confidence >= 55
                            and ensemble["accuracies"]["Ensemble"] >= 52
                            and agreement >= 3 and moves_agree
                            and level_allows_trade else "WATCH"),
                "Side": side, "Contract": contract, "Structure": strategy,
                "Expression": expressions[strategy],
                "Current_Value": float(close.iloc[-1]),
                "ML_Expected_Move": ensemble["expected_move"],
                "Historical_Move": float(matches.median()),
                "Probability": confidence,
                "Pattern_Probability": pattern_confidence,
                "Model_Votes": f"{agreement}/4",
                "Ensemble_Accuracy": ensemble["accuracies"]["Ensemble"],
                "LightGBM_Accuracy": ensemble["accuracies"]["LightGBM"],
                "ExtraTrees_Accuracy": ensemble["accuracies"]["Extra Trees"],
                "Logistic_Accuracy": ensemble["accuracies"]["Logistic"],
                "HDBSCAN_Accuracy": ensemble["accuracies"]["HDBSCAN"],
                "Similar_Cases": len(matches), "Months_Seen": periods,
                "Support": levels["support"],
                "Resistance": levels["resistance"],
                "Available_Room": available_room,
                "Level_Allows_Trade": level_allows_trade,
                "Pattern": current_pattern,
                "Score": (confidence - 50) * np.sqrt(periods),
                "Interval": interval, "Horizon_Bars": horizon_bars,
            })
        return output
    finally:
        database.close()


def _candle_states(candles, window=20):
    combined = pd.concat({name: frame for name, frame in candles.items()}, axis=1)
    feature_data = {}
    for name in candles:
        frame = combined[name]
        close = frame["close"]
        average = close.rolling(window).mean()
        std = close.rolling(window).std()
        feature_data[f"{name}_z"] = (close - average) / std
        feature_data[f"{name}_chg3"] = close.diff(3)
        feature_data[f"{name}_vol"] = close.diff().rolling(window).std()
        feature_data[f"{name}_range"] = frame["high"] - frame["low"]
        feature_data[f"{name}_body"] = frame["close"] - frame["open"]
        candle_range = (frame["high"] - frame["low"]).replace(0, np.nan)
        feature_data[f"{name}_upper_wick"] = (
            frame["high"] - frame[["open", "close"]].max(axis=1)
        ) / candle_range
        feature_data[f"{name}_lower_wick"] = (
            frame[["open", "close"]].min(axis=1) - frame["low"]
        ) / candle_range
        body_ratio = abs(frame["close"] - frame["open"]) / candle_range
        feature_data[f"{name}_body_ratio"] = body_ratio
        feature_data[f"{name}_doji"] = (body_ratio <= 0.10).astype(float)
        previous_high, previous_low = frame["high"].shift(1), frame["low"].shift(1)
        feature_data[f"{name}_inside_bar"] = (
            (frame["high"] < previous_high) & (frame["low"] > previous_low)
        ).astype(float)
        previous_open, previous_close = frame["open"].shift(1), frame["close"].shift(1)
        feature_data[f"{name}_bull_engulf"] = (
            (frame["close"] > frame["open"]) & (previous_close < previous_open)
            & (frame["close"] >= previous_open) & (frame["open"] <= previous_close)
        ).astype(float)
        feature_data[f"{name}_bear_engulf"] = (
            (frame["close"] < frame["open"]) & (previous_close > previous_open)
            & (frame["open"] >= previous_close) & (frame["close"] <= previous_open)
        ).astype(float)
        for level_window in (20, 60):
            resistance = frame["high"].rolling(level_window).max()
            support = frame["low"].rolling(level_window).min()
            feature_data[f"{name}_resistance_{level_window}"] = resistance - close
            feature_data[f"{name}_support_{level_window}"] = close - support
            feature_data[f"{name}_range_position_{level_window}"] = (
                (close - support) / (resistance - support).replace(0, np.nan)
            )
    feature_data["Hour_ctx"] = pd.Series(
        combined.index.hour + combined.index.minute / 60, index=combined.index
    )
    feature_data["DayOfWeek_ctx"] = pd.Series(
        combined.index.dayofweek, index=combined.index
    )
    output = pd.DataFrame(feature_data, index=combined.index)
    features = list(output.columns)
    output = output.dropna(thresh=max(3, int(len(features) * 0.60)))
    features = [column for column in features if output[column].notna().mean() >= 0.60]
    return output, features


def _support_resistance(frame, lookback=120):
    recent = frame.tail(lookback).dropna()
    current = float(recent["close"].iloc[-1])
    scale = float(recent["close"].diff().std())
    prominence = max(scale * 1.5, 1e-10)
    peak_indexes, _ = find_peaks(
        recent["high"].to_numpy(dtype=float), prominence=prominence, distance=3
    )
    trough_indexes, _ = find_peaks(
        -recent["low"].to_numpy(dtype=float), prominence=prominence, distance=3
    )
    resistances = recent["high"].iloc[peak_indexes]
    supports = recent["low"].iloc[trough_indexes]
    resistances = resistances[resistances > current]
    supports = supports[supports < current]
    resistance = (float(resistances.min()) if not resistances.empty
                  else float(recent["high"].max()))
    support = (float(supports.max()) if not supports.empty
               else float(recent["low"].min()))
    return {"current": current, "support": support, "resistance": resistance}


def _sort_suggestions(suggestions):
    if not suggestions:
        return pd.DataFrame()
    result = pd.DataFrame(suggestions).sort_values(
        ["Quality", "Score", "Similar_Cases"], ascending=[True, False, False]
    ).reset_index(drop=True)
    trade_indexes = result.index[result["Quality"] == "TRADE"]
    if len(trade_indexes) > 5:
        result.loc[trade_indexes[5:], "Quality"] = "WATCH"
        result = result.sort_values(
            ["Quality", "Score", "Similar_Cases"], ascending=[True, False, False]
        ).reset_index(drop=True)
    return result

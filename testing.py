"""STUMPY experiment for discovering repeated intraday synthetic-price patterns."""

import argparse
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import stumpy

from market_data import MarketData


DEFAULT_PRODUCT = "CL"
DEFAULT_EXPRESSION = "X26-2*Z26+F27"
OUTPUT_DIRECTORY = Path("stumpy_outputs")


def load_candles(product, expression, interval, history_days, minimum_coverage):
    database = MarketData()
    try:
        start = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=history_days)
        minute = database.synthetic(product, expression, start=start)
    finally:
        database.close()
    if minute.empty:
        raise ValueError("No minute data is available for the requested synthetic expression")

    price = (minute.dropna(subset=["timestamp", "price"])
             .sort_values("timestamp").set_index("timestamp")["price"])
    candles = price.resample(interval).agg(
        open="first", high="max", low="min", close="last", observations="count"
    )
    expected = pd.Timedelta(interval) / pd.Timedelta(minutes=1)
    candles["coverage"] = candles["observations"] / expected
    candles = candles[candles["coverage"] >= minimum_coverage].dropna(
        subset=["open", "high", "low", "close"]
    )
    if candles.empty:
        raise ValueError("No candles passed the selected data-coverage requirement")
    return candles


def discover_motifs(candles, pattern_bars, motif_count, distance_multiplier):
    close = candles["close"].to_numpy(dtype=np.float64)
    if pattern_bars < 3 or pattern_bars >= len(close) // 2:
        raise ValueError("Pattern length must be at least 3 and below half the candle count")

    matrix_profile = stumpy.stump(close, m=pattern_bars)
    distances = matrix_profile[:, 0].astype(float)
    neighbours = matrix_profile[:, 1].astype(int)
    selected, motifs, occurrences = [], [], []
    for first in np.argsort(distances):
        second = neighbours[first]
        if second < 0 or not np.isfinite(distances[first]):
            continue
        if any(abs(first - used) < pattern_bars or abs(second - used) < pattern_bars
               for used in selected):
            continue
        motif_id = len(motifs) + 1
        base_distance = float(distances[first])
        threshold = max(base_distance * distance_multiplier, np.finfo(float).eps)
        query = close[first:first + pattern_bars]
        matches = stumpy.match(query, close, max_distance=threshold)
        valid_matches = []
        for distance, index in matches:
            index = int(index)
            if any(abs(index - existing[1]) < pattern_bars for existing in valid_matches):
                continue
            valid_matches.append((float(distance), index))
        if len(valid_matches) < 2:
            valid_matches = [(base_distance, int(first)), (base_distance, int(second))]
        motifs.append({
            "Motif": motif_id, "Matrix_Profile_Distance": base_distance,
            "Match_Threshold": threshold, "Occurrences": len(valid_matches),
            "Pattern_Bars": pattern_bars,
        })
        for distance, index in valid_matches:
            end_index = index + pattern_bars - 1
            occurrences.append({
                "Motif": motif_id, "Distance": distance,
                "Start_Index": index, "End_Index": end_index,
                "Start": candles.index[index], "End": candles.index[end_index],
            })
        selected.extend(index for _, index in valid_matches)
        if len(motifs) >= motif_count:
            break
    profile = pd.DataFrame({
        "Start": candles.index[:len(distances)], "Distance": distances,
        "Nearest_Index": neighbours,
    })
    return pd.DataFrame(motifs), pd.DataFrame(occurrences), profile


def make_figure(candles, occurrences, profile, title):
    figure = make_subplots(
        rows=3, cols=1, vertical_spacing=0.08,
        subplot_titles=("Candles and discovered occurrences",
                        "Normalized motif shapes", "Matrix profile"),
        row_heights=[0.52, 0.28, 0.20],
    )
    figure.add_trace(go.Candlestick(
        x=candles.index, open=candles["open"], high=candles["high"],
        low=candles["low"], close=candles["close"], name="OHLC",
        increasing_line_color="#16a34a", decreasing_line_color="#dc2626",
    ), row=1, col=1)
    colors = ["#2563eb", "#9333ea", "#ea580c", "#0891b2", "#ca8a04",
              "#db2777", "#4f46e5", "#059669"]
    for _, occurrence in occurrences.iterrows():
        motif = int(occurrence["Motif"])
        color = colors[(motif - 1) % len(colors)]
        figure.add_vrect(
            x0=occurrence["Start"], x1=occurrence["End"],
            fillcolor=color, opacity=0.16, line_width=1, line_color=color,
            row=1, col=1,
        )
        segment = candles["close"].iloc[
            int(occurrence["Start_Index"]):int(occurrence["End_Index"]) + 1
        ].to_numpy(dtype=float)
        std = segment.std()
        normalized = (segment - segment.mean()) / std if std else segment - segment.mean()
        figure.add_trace(go.Scatter(
            x=np.arange(len(normalized)), y=normalized, mode="lines",
            line={"color": color, "width": 1.5},
            name=f"Motif {motif}: {occurrence['Start']:%Y-%m-%d %H:%M}",
            legendgroup=f"motif-{motif}",
        ), row=2, col=1)
    figure.add_trace(go.Scatter(
        x=profile["Start"], y=profile["Distance"], mode="lines",
        name="Matrix-profile distance", line={"color": "#334155", "width": 1},
    ), row=3, col=1)
    figure.update_layout(
        title=title, template="plotly_white", height=1100,
        xaxis_rangeslider_visible=False, hovermode="x unified",
    )
    return figure


def save_outputs(candles, motifs, occurrences, profile, figure, parameters):
    OUTPUT_DIRECTORY.mkdir(exist_ok=True)
    safe_expression = parameters["expression"].replace("*", "x").replace("+", "p")
    stem = (f"stumpy_{parameters['product']}_{safe_expression}_"
            f"{parameters['interval']}_m{parameters['pattern_bars']}_"
            f"{datetime.now():%Y%m%d_%H%M%S}")
    html_path = OUTPUT_DIRECTORY / f"{stem}.html"
    excel_path = OUTPUT_DIRECTORY / f"{stem}.xlsx"
    figure.write_html(html_path, include_plotlyjs="cdn")
    with pd.ExcelWriter(excel_path, engine="openpyxl") as writer:
        pd.DataFrame({"Parameter": parameters.keys(),
                      "Value": parameters.values()}).to_excel(
            writer, sheet_name="Parameters", index=False
        )
        motifs.to_excel(writer, sheet_name="Motifs", index=False)
        occurrences.to_excel(writer, sheet_name="Occurrences", index=False)
        profile.to_excel(writer, sheet_name="Matrix_Profile", index=False)
        candles.reset_index().to_excel(writer, sheet_name="Candles", index=False)
    return html_path.resolve(), excel_path.resolve()


def main():
    parser = argparse.ArgumentParser(description="Discover repeated minute-data patterns")
    parser.add_argument("--product", default=DEFAULT_PRODUCT)
    parser.add_argument("--expression", default=DEFAULT_EXPRESSION)
    parser.add_argument("--interval", default="30min", choices=["15min", "30min", "60min"])
    parser.add_argument("--pattern-bars", type=int, default=12)
    parser.add_argument("--motifs", type=int, default=5)
    parser.add_argument("--history-days", type=int, default=150)
    parser.add_argument("--distance-multiplier", type=float, default=1.25)
    parser.add_argument("--minimum-coverage", type=float, default=0.60)
    args = parser.parse_args()
    parameters = vars(args)
    candles = load_candles(
        args.product, args.expression, args.interval,
        args.history_days, args.minimum_coverage,
    )
    motifs, occurrences, profile = discover_motifs(
        candles, args.pattern_bars, args.motifs, args.distance_multiplier,
    )
    title = (f"STUMPY motifs | {args.product} {args.expression} | "
             f"{args.interval} | {args.pattern_bars}-bar patterns")
    figure = make_figure(candles, occurrences, profile, title)
    html_path, excel_path = save_outputs(
        candles, motifs, occurrences, profile, figure, parameters,
    )
    print(f"Interactive chart: {html_path}")
    print(f"Excel details: {excel_path}")
    figure.show()


if __name__ == "__main__":
    main()

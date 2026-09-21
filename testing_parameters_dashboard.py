"""Simple symbol/bucket-level dashboard for the parameter sweep report."""

import argparse
import math
import os
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
from dash import Dash, Input, Output, ctx, dcc, html

from product_config import PRODUCT_CONFIG

PARAMETERS = ["lookback", "rank", "max_parallel", "max_consecutive",
              "maximum_trade_days", "no_trade_days", "swing_lookback", "swing_top_count",
              "minimum_swing_ticks"]
FILTER_PARAMETERS = [parameter for parameter in PARAMETERS
                     if parameter != "minimum_swing_ticks"]
RANKINGS = {"Robust win rate (recommended)": "wilson_lower_95", "Win rate": "win_rate",
            "Total PnL": "total_pnl_ticks", "Consistency": "profitable_year_rate"}
INPUT_STYLE = {"width": "100%", "color": "#111827"}


def load_results(filename):
    source = Path(filename)
    if not source.exists():
        raise FileNotFoundError(f"Report not found: {source.resolve()}")
    if source.is_dir():
        reports = sorted(source.glob("*.xlsx"))
        if not reports:
            raise FileNotFoundError(f"No parameter workbooks found in: {source.resolve()}")
        bucket_reports = [report for report in reports if "_" in report.stem]
        if bucket_reports:
            reports = bucket_reports
        cache = source / "combined_by_bucket.dashboard.pkl"
        if cache.exists() and cache.stat().st_mtime >= max(
                report.stat().st_mtime for report in reports):
            return pd.read_pickle(cache)
        print("Reading the symbol reports for the first time...")
        frames = []
        for report in reports:
            workbook = pd.ExcelFile(report)
            frames.extend(pd.read_excel(workbook, sheet_name=sheet)
                          for sheet in workbook.sheet_names
                          if sheet.startswith("Raw_Results"))
        frame = pd.concat(frames, ignore_index=True)
        frame.to_pickle(cache)
        return frame
    cache = source.with_suffix(".dashboard.pkl")
    if cache.exists() and cache.stat().st_mtime >= source.stat().st_mtime:
        return pd.read_pickle(cache)
    print("Reading the Excel report for the first time...")
    frame = pd.read_excel(source, sheet_name="Raw_Results")
    frame.to_pickle(cache)
    return frame


def choices(values):
    unique = pd.Series(values).dropna().unique()
    return [{"label": str(value), "value": value} for value in sorted(unique)]


def wilson_lower(wins, trades, z=1.96):
    if not trades:
        return 0.0
    rate, denominator = wins / trades, 1 + z * z / trades
    centre = rate + z * z / (2 * trades)
    spread = z * math.sqrt(rate * (1 - rate) / trades + z * z / (4 * trades * trades))
    return (centre - spread) / denominator


def aggregate_bucket(raw, bucket, symbol=None):
    selected = raw[raw["strategy"] == bucket]
    if symbol is not None and "symbol" in selected:
        selected = selected[selected["symbol"] == symbol]
    result = selected.groupby(PARAMETERS, as_index=False).agg(
        expressions=("expression", "nunique"), trades=("trades", "sum"),
        wins=("wins", "sum"), losses=("losses", "sum"),
        unresolved=("unresolved", "sum"), total_pnl_ticks=("total_pnl_ticks", "sum"),
        profitable_year_rate=("profitable_year_rate", "mean"),
        average_holding_days=("average_holding_days", "mean"))
    result["win_rate"] = result["wins"] / result["trades"].replace(0, np.nan)
    result["average_pnl_ticks"] = result["total_pnl_ticks"] / result["trades"].replace(0, np.nan)
    result["wilson_lower_95"] = [wilson_lower(w, t)
                                 for w, t in zip(result["wins"], result["trades"])]
    return result


def parameter_filter(column, frame):
    return html.Label([
        html.Span(column.replace("_", " ").title(),
                  style={"display": "block", "marginBottom": "4px"}),
        dcc.Dropdown(id=f"filter-{column}", options=choices(frame[column]), multi=True,
                     placeholder="All values", persistence=True, persistence_type="local",
                     style=INPUT_STYLE)])


def shortlist_thresholds(symbol, bucket):
    config = PRODUCT_CONFIG[symbol]["buckets"][bucket]
    return (list(config["minimum_trade_thresholds"]),
            list(config["minimum_swing_ticks"]))


def save_best_settings(frame, symbol, bucket, trade_thresholds,
                       swing_thresholds, count, output_dir):
    searches = []
    for trade_threshold in trade_thresholds:
        for swing_threshold in swing_thresholds:
            eligible = frame[
                (frame["trades"] >= trade_threshold) &
                (frame["minimum_swing_ticks"] == swing_threshold)
            ]
            for ranking_label, ranking_column in RANKINGS.items():
                selected = eligible.sort_values(
                    [ranking_column, "win_rate", "total_pnl_ticks", "trades"],
                    ascending=False,
                ).drop_duplicates(PARAMETERS).head(count).copy()
                selected["trade_threshold"] = trade_threshold
                selected["required_minimum_swing_ticks"] = swing_threshold
                selected["ranking_method"] = ranking_label
                searches.append(selected)
    selected = pd.concat(searches, ignore_index=True)
    selected.drop_duplicates(PARAMETERS, inplace=True)
    selected.reset_index(drop=True, inplace=True)
    if selected.empty:
        raise ValueError("No settings qualify for the selected thresholds")
    selected.insert(0, "symbol", symbol)
    selected.insert(1, "bucket", bucket)
    selected.insert(2, "shortlist_case", range(1, len(selected) + 1))
    selected["selected_win_rate"] = selected["win_rate"]
    selected["selected_robust_win_rate"] = selected["wilson_lower_95"]
    selected["selected_trades"] = selected["trades"]
    selected["selected_total_pnl_ticks"] = selected["total_pnl_ticks"]
    selected["status"] = "READY"
    selected["use_currently"] = 0
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    output = destination / f"{symbol}_{bucket}.xlsx"
    handle, temporary = tempfile.mkstemp(
        prefix=f".{output.stem}.", suffix=".xlsx", dir=destination)
    os.close(handle)
    try:
        with pd.ExcelWriter(temporary, engine="openpyxl") as writer:
            selected.to_excel(writer, sheet_name="All_Settings", index=False)
            selected.to_excel(writer, sheet_name=f"{symbol}_{bucket}", index=False)
        os.replace(temporary, output)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return output, len(selected)


def create_app(raw, best_output_dir):
    if "symbol" not in raw:
        raw["symbol"] = "CL"
    symbols = sorted(raw["symbol"].dropna().unique())
    buckets = [value for value in ("1MDF", "1MDDF") if value in set(raw["strategy"])]
    initial_trades, initial_swings = shortlist_thresholds(symbols[0], buckets[0])
    app = Dash(__name__)
    app.title = "Parameter Sweep"
    panel = {"background": "#111827", "border": "1px solid #334155",
             "borderRadius": "8px", "padding": "16px", "margin": "12px auto",
             "maxWidth": "1400px"}
    app.layout = html.Div([
        html.H2("Find the Best Settings", style={"textAlign": "center"}),
        html.P("Results are combined across every expression in the selected bucket. "
               "No individual expression is ranked.",
               style={"textAlign": "center", "color": "#94a3b8"}),
        html.Div([html.H3("1. Choose symbol and bucket"), dcc.RadioItems(
            id="symbol", options=choices(symbols), value=symbols[0], inline=True,
            labelStyle={"marginRight": "30px", "fontSize": "20px",
                        "color": "#e2e8f0"}), dcc.RadioItems(
            id="bucket", options=[{"label": value, "value": value} for value in buckets],
            value=buckets[0], inline=True, persistence=True, persistence_type="local",
            labelStyle={"marginRight": "30px", "fontSize": "20px", "color": "#e2e8f0"})],
            style=panel),
        html.Div([html.H3("2. Optionally narrow parameter values"), html.Div(
            [parameter_filter(column, raw) for column in FILTER_PARAMETERS],
            style={"display": "grid", "gridTemplateColumns":
                   "repeat(auto-fit, minmax(190px, 1fr))", "gap": "12px"})], style=panel),
        html.Div([html.H3("3. Choose what best means"), html.Div([
            html.Label(["Rank using", dcc.Dropdown(
                id="ranking", options=[{"label": k, "value": v} for k, v in RANKINGS.items()],
                value="wilson_lower_95", clearable=False, style=INPUT_STYLE)]),
            html.Label(["Minimum trade thresholds", dcc.Checklist(
                id="trade-thresholds",
                options=[{"label": str(value), "value": value}
                         for value in initial_trades],
                value=initial_trades, inline=True,
                persistence=True, persistence_type="local",
                labelStyle={"marginRight": "16px"})]),
            html.Label(["Minimum swing ticks", dcc.Checklist(
                id="swing-thresholds",
                options=[{"label": str(value), "value": value}
                         for value in initial_swings],
                value=initial_swings, inline=True,
                persistence=True, persistence_type="local",
                labelStyle={"marginRight": "16px"})]),
            html.Label(["Number of best settings", dcc.Input(
                id="best-count", type="number", value=3, min=1, step=1,
                debounce=True, persistence=True, persistence_type="local",
                style={"width": "100%", "height": "36px"})]),
            html.Div([
                html.Button("Save best settings", id="save-best-settings", n_clicks=0,
                            style={"padding": "10px 18px", "fontWeight": "bold"}),
                html.Div(id="save-best-status", style={"color": "#86efac",
                                                        "marginTop": "6px"}),
            ])],
            style={"display": "grid", "gridTemplateColumns":
                   "repeat(auto-fit, minmax(210px, 1fr))",
                   "gap": "16px"})], style=panel),
        html.Div(id="explanation", style={**panel, "color": "#cbd5e1"}),
        html.Div(id="kpis", style={"display": "flex", "justifyContent": "center",
                                   "flexWrap": "wrap", "gap": "12px"})],
        style={"background": "#020617", "color": "white", "minHeight": "100vh",
               "padding": "10px 20px 40px"})

    @app.callback(
        Output("trade-thresholds", "options"), Output("trade-thresholds", "value"),
        Output("swing-thresholds", "options"), Output("swing-thresholds", "value"),
        Input("symbol", "value"), Input("bucket", "value"),
    )
    def refresh_shortlist_thresholds(symbol, bucket):
        trades, swings = shortlist_thresholds(symbol, bucket)
        options = lambda values: [{"label": str(value), "value": value}
                                  for value in values]
        return options(trades), trades, options(swings), swings

    @app.callback(
        Output("explanation", "children"), Output("kpis", "children"),
        Output("save-best-status", "children"),
        Input("symbol", "value"), Input("bucket", "value"),
        *[Input(f"filter-{column}", "value") for column in FILTER_PARAMETERS],
        Input("ranking", "value"), Input("trade-thresholds", "value"),
        Input("swing-thresholds", "value"),
        Input("best-count", "value"), Input("save-best-settings", "n_clicks"))
    def update(symbol, bucket, *values):
        selected_values = values[:len(FILTER_PARAMETERS)]
        ranking, trade_thresholds, swing_thresholds, best_count, _clicks = values[
            len(FILTER_PARAMETERS):]
        configured_trades, configured_swings = shortlist_thresholds(symbol, bucket)
        trade_thresholds = trade_thresholds or configured_trades
        swing_thresholds = swing_thresholds or configured_swings
        frame = aggregate_bucket(raw, bucket, symbol)
        for column, selected in zip(FILTER_PARAMETERS, selected_values):
            if selected:
                frame = frame[frame[column].isin(selected)]
        frame = frame[
            (frame["trades"] >= min(trade_thresholds)) &
            frame["minimum_swing_ticks"].isin(swing_thresholds)
        ].copy()
        if frame.empty:
            return "No configurations pass the filters.", [], ""
        frame = frame.sort_values([ranking, "win_rate", "total_pnl_ticks"], ascending=False)
        best = frame.iloc[0]
        ranking_label = next(label for label, value in RANKINGS.items() if value == ranking)
        save_status = ""
        if ctx.triggered_id == "save-best-settings":
            try:
                count = max(1, int(best_count or 1))
                output, saved_count = save_best_settings(
                    frame, symbol, bucket, trade_thresholds,
                    swing_thresholds, count, best_output_dir,
                )
                save_status = f"Saved {saved_count} unique settings to {output}."
            except PermissionError:
                save_status = ("Could not save the file. Close it in Excel and wait for "
                               "OneDrive syncing to finish, then try again.")
            except Exception as error:
                save_status = f"Could not save best settings: {error}"
        recommendations = []
        for position, (_, recommendation) in enumerate(frame.head(10).iterrows(), 1):
            chips = [html.Span(
                f"{parameter.replace('_', ' ').title()}: {recommendation[parameter]:g}",
                style={"background": "#1e293b", "border": "1px solid #475569",
                       "borderRadius": "14px", "padding": "4px 8px", "margin": "3px"})
                     for parameter in PARAMETERS]
            recommendations.append(html.Div([
                html.Div([
                    html.B(f"#{position}", style={"color": "#a78bfa", "fontSize": "18px"}),
                    html.Span(f"Win {recommendation['win_rate']:.1%}"),
                    html.Span(f"Robust {recommendation['wilson_lower_95']:.1%}"),
                    html.Span(f"Trades {recommendation['trades']:,.0f}"),
                    html.Span(f"PnL {recommendation['total_pnl_ticks']:+,.0f} ticks"),
                    html.Span(f"Profitable years {recommendation['profitable_year_rate']:.1%}"),
                ], style={"display": "flex", "gap": "14px", "alignItems": "center",
                          "flexWrap": "wrap", "marginBottom": "7px"}),
                html.Div(chips, style={"display": "flex", "flexWrap": "wrap"}),
            ], style={"background": "#0f172a", "border": "1px solid #334155",
                      "borderRadius": "7px", "padding": "10px"}))
        explanation = [
            html.Div([html.B(f"Top 10 {symbol} {bucket} settings by {ranking_label}")],
                     style={"fontSize": "18px", "marginBottom": "8px", "color": "#f8fafc"}),
            html.Div(recommendations, style={"display": "grid",
                                             "gridTemplateColumns":
                                             "repeat(auto-fit, minmax(600px, 1fr))",
                                             "gap": "8px"}),
            html.Div(f"Compared {len(frame):,} settings across {int(best['expressions'])} "
                     f"expressions. Robust win rate penalizes weak sample sizes.",
                     style={"marginTop": "10px", "color": "#94a3b8"})]
        card = {"background": "#0f172a", "border": "1px solid #334155",
                "padding": "12px 18px", "borderRadius": "7px", "textAlign": "center"}
        kpis = [html.Div([html.Small(label), html.H3(value)], style=card)
                for label, value in (("Best win rate", f"{best['win_rate']:.1%}"),
                    ("Robust win rate", f"{best['wilson_lower_95']:.1%}"),
                    ("Total trades", f"{best['trades']:,.0f}"),
                    ("Total PnL", f"{best['total_pnl_ticks']:+,.0f} ticks"),
                    ("Profitable years", f"{best['profitable_year_rate']:.1%}"))]
        return explanation, kpis, save_status
    return app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="data/testing_parameters")
    parser.add_argument("--best-output-dir", default="data/best_parameters")
    parser.add_argument("--port", type=int, default=8052)
    arguments = parser.parse_args()
    create_app(load_results(arguments.input), arguments.best_output_dir).run(
        host="127.0.0.1", port=arguments.port, debug=False)


if __name__ == "__main__":
    main()

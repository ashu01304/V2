"""Expression-level dashboard for latest_trigger_levels.xlsx."""

import argparse
from pathlib import Path
from threading import Lock
from time import monotonic

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from dash import Dash, Input, Output, dash_table, dcc, html

from market_data import LiveMarketDataClient, MarketData
from product_config import TICK_SIZES


COLORS = {"paper_bgcolor": "#020617", "plot_bgcolor": "#020617", "font_color": "#e2e8f0"}
MINIMUM_OPPORTUNITY_SECONDS = 10 * 60


def load_levels(filename):
    source = Path(filename)
    if not source.exists():
        raise FileNotFoundError(f"Trigger workbook not found: {source.resolve()}")
    if source.is_dir():
        reports = sorted(source.glob("*.xlsx"))
        if not reports:
            raise FileNotFoundError(f"No trigger workbooks found in: {source.resolve()}")
        cache = source / "combined.dashboard.pkl"
        if cache.exists() and cache.stat().st_mtime >= max(
                report.stat().st_mtime for report in reports):
            return pd.read_pickle(cache)
        frame = pd.concat(
            [pd.read_excel(report, sheet_name="All_Triggers") for report in reports],
            ignore_index=True,
        )
        frame.to_pickle(cache)
        return frame
    cache = source.with_suffix(".dashboard.pkl")
    if cache.exists() and cache.stat().st_mtime >= source.stat().st_mtime:
        return pd.read_pickle(cache)
    frame = pd.read_excel(source, sheet_name="All_Triggers")
    if "symbol" not in frame:
        frame["symbol"] = "CL"
    frame.to_pickle(cache)
    return frame


def choices(values):
    return [{"label": str(value), "value": value}
            for value in sorted(pd.Series(values).dropna().unique())]


def hourly_figure(symbol, expression, plans, live_price):
    database = MarketData()
    try:
        minute = database.synthetic({"CO": "LCO"}.get(symbol, symbol), expression)
    except Exception as error:
        figure = go.Figure().add_annotation(
            text=f"Hourly data unavailable: {error}", showarrow=False)
        return figure.update_layout(**COLORS, height=675)
    finally:
        database.close()
    candles = (minute.dropna().set_index("timestamp")["price"].sort_index()
               .resample("1h").ohlc().dropna())
    if candles.empty:
        return go.Figure().add_annotation(
            text="No hourly candles are available", showarrow=False
        ).update_layout(**COLORS, height=675)
    all_hours = pd.date_range(candles.index.min(), candles.index.max(), freq="1h")
    missing_hours = all_hours.difference(candles.index)
    figure = go.Figure(go.Candlestick(
        x=candles.index, open=candles["open"], high=candles["high"],
        low=candles["low"], close=candles["close"], name="Hourly price",
        increasing_line_color="#22c55e", decreasing_line_color="#ef4444"))
    decimals = 2 if symbol in ("CL", "CO") else 4
    for direction, entry_column, tp_column, sl_column in (
            ("LONG", "long_entry", "long_tp", "long_sl"),
            ("SHORT", "short_entry", "short_tp", "short_sl")):
        source = pd.DataFrame({
            "entry": plans[entry_column], "tp": plans[tp_column], "sl": plans[sl_column],
            "case": plans["shortlist_case"], "trades": plans["selected_trades"],
            "wins": plans["selected_win_rate"] * plans["selected_trades"],
        }).dropna()
        grouped = source.groupby(["entry", "tp", "sl"], as_index=False).agg(
            supporting_cases=("case", "count"), trades=("trades", "sum"),
            wins=("wins", "sum"), cases=("case", lambda x: ", ".join(map(str, x))))
        grouped["accuracy"] = grouped["wins"] / grouped["trades"]
        best = grouped.sort_values(
            ["accuracy", "supporting_cases", "trades"], ascending=False
        ).iloc[0]
        dash = "solid" if direction == "LONG" else "dot"
        side = "left" if direction == "LONG" else "right"
        for label, column, color in (
                ("ENTRY", "entry", "#facc15"),
                ("TP", "tp", "#22c55e"),
                ("SL", "sl", "#ef4444")):
            details = (f" · {best.accuracy:.1%} · {int(best.supporting_cases)} settings"
                       if label == "ENTRY" else "")
            figure.add_hline(
                y=best[column], line_color=color, line_dash=dash, line_width=1.5,
                annotation_text=(f"{direction} {label} "
                                 f"{best[column]:.{decimals}f}{details}"),
                annotation_position=f"top {side}",
                annotation_font={"color": color, "size": 11})
    if live_price is not None:
        figure.add_trace(go.Scatter(
            x=[candles.index.max()], y=[live_price], mode="markers+text", name="Live price",
            text=[f"Live {live_price:.{decimals}f}"], textposition="top right",
            marker={"color": "#34d399", "size": 8, "line": {"color": "white", "width": 1}}))
    figure.update_layout(
        title=f"{symbol} {expression} | hourly price with strongest LONG and SHORT plans",
        xaxis_title="Time", yaxis_title="Expression price", height=675,
        xaxis_rangeslider_visible=False, uirevision=f"hourly-trigger:{symbol}:{expression}",
        legend={"orientation": "h", "y": 1.02, "x": 0},
        margin={"l": 65, "r": 35, "t": 90, "b": 55}, **COLORS)
    figure.update_xaxes(
        showgrid=False,
        rangebreaks=[{"values": missing_hours, "dvalue": 60 * 60 * 1000}],
    )
    figure.update_yaxes(showgrid=False)
    return figure


def create_app(data):
    app = Dash(__name__)
    app.title = "Trigger Levels"
    live = LiveMarketDataClient()
    opportunity_cache, opportunity_lock = {}, Lock()
    if "symbol" not in data:
        data["symbol"] = "CL"
    initial_symbol = sorted(data["symbol"].dropna().unique())[0]
    symbol_data = data[data["symbol"] == initial_symbol]
    initial_bucket = sorted(symbol_data["bucket"].dropna().unique())[0]
    initial_expressions = sorted(
        symbol_data.loc[symbol_data["bucket"] == initial_bucket,
                        "expression"].dropna().unique()
    )
    panel = {"background": "#111827", "border": "1px solid #334155",
             "borderRadius": "8px", "padding": "15px", "margin": "12px auto",
             "maxWidth": "1500px"}
    input_style = {"width": "100%", "color": "#111827"}
    app.layout = html.Div([
        html.H2("Latest Trigger-Level Analysis", style={"textAlign": "center"}),
        html.P("Select one expression to compare its unique shortlisted boundary calculations.",
               style={"textAlign": "center", "color": "#94a3b8"}),
        html.Div([
            html.Label(["Symbol", dcc.RadioItems(
                id="symbol", options=choices(data["symbol"]), value=initial_symbol,
                inline=True,
                labelStyle={"marginRight": "25px", "color": "white"})]),
            html.Label(["Bucket", dcc.RadioItems(
                id="bucket", options=choices(data["bucket"]),
                value=initial_bucket, inline=True,
                labelStyle={"marginRight": "25px", "color": "white"})]),
            html.Label(["Expression", dcc.Dropdown(
                id="expression", options=choices(initial_expressions),
                value=initial_expressions[0] if initial_expressions else None,
                clearable=False, style=input_style)]),
            html.Label(["Ranking methods", dcc.Dropdown(
                id="ranking-method", options=choices(data["ranking_method"]), multi=True,
                placeholder="All rankings", persistence=True, persistence_type="local",
                style=input_style)]),
            html.Label(["Trade thresholds", dcc.Dropdown(
                id="trade-threshold", options=choices(data["trade_threshold"]), multi=True,
                placeholder="All thresholds", persistence=True, persistence_type="local",
                style=input_style)]),
            html.Label(["Required minimum swing", dcc.Dropdown(
                id="minimum-swing", options=choices(data["required_minimum_swing_ticks"]),
                multi=True, placeholder="All values", persistence=True,
                persistence_type="local", style=input_style)]),
        ], style={**panel, "display": "grid",
                  "gridTemplateColumns": "repeat(auto-fit, minmax(220px, 1fr))",
                  "gap": "15px", "alignItems": "end"}),
        html.Div(id="status", style=panel),
        dcc.Graph(id="hourly-chart", config={"displaylogo": False, "responsive": True},
                  style={"width": "calc(100vw - 40px)", "height": "675px"}),
        html.H3("Consensus trigger levels", style={"textAlign": "center"}),
        dash_table.DataTable(
            id="consensus-table", page_size=15, sort_action="native",
            style_table={"width": "fit-content", "maxWidth": "calc(100vw - 40px)",
                         "margin": "0 auto 25px", "overflowX": "auto"},
            style_header={"backgroundColor": "#1e293b", "color": "white",
                          "fontWeight": "bold"},
            style_cell={"backgroundColor": "#0f172a", "color": "white",
                        "border": "1px solid #334155", "padding": "7px",
                        "minWidth": "115px", "textAlign": "center"}),
        html.H3("Best current trade options", style={"textAlign": "center"}),
        dash_table.DataTable(
            id="case-table", page_action="none", sort_action="native",
            style_table={"width": "fit-content", "maxWidth": "calc(100vw - 40px)",
                         "margin": "0 auto", "overflowX": "auto",
                         "maxHeight": "700px", "overflowY": "auto"},
            style_header={"backgroundColor": "#1e293b", "color": "white",
                          "fontWeight": "bold"},
            style_cell={"backgroundColor": "#0f172a", "color": "white",
                        "border": "1px solid #334155", "padding": "6px",
                        "minWidth": "105px", "textAlign": "center"}),
        dcc.Interval(id="live-refresh", interval=10_000, n_intervals=0),
    ], style={"background": "#020617", "color": "white", "minHeight": "100vh",
              "padding": "10px 20px 40px"})

    @app.callback(Output("expression", "options"), Output("expression", "value"),
                  Input("symbol", "value"), Input("bucket", "value"))
    def expressions_for_bucket(symbol, bucket):
        expressions = sorted(data.loc[
            (data["symbol"] == symbol) & (data["bucket"] == bucket),
            "expression"].dropna().unique())
        return choices(expressions), expressions[0] if expressions else None

    @app.callback(
        Output("status", "children"), Output("hourly-chart", "figure"),
        Output("consensus-table", "columns"), Output("consensus-table", "data"),
        Output("case-table", "columns"), Output("case-table", "data"),
        Input("expression", "value"),
        Input("ranking-method", "value"), Input("trade-threshold", "value"),
        Input("minimum-swing", "value"), Input("live-refresh", "n_intervals"),
        Input("bucket", "value"), Input("symbol", "value"))
    def update(expression, rankings, thresholds, minimum_swings, _, bucket, symbol):
        expression_rows = data[
            (data["symbol"] == symbol) & (data["bucket"] == bucket)
            & (data["expression"] == expression)].copy()
        option_rows = data[(data["symbol"] == symbol) &
                           (data["bucket"] == bucket)].copy()
        for column, selected in (("trade_threshold", thresholds),
                                 ("required_minimum_swing_ticks", minimum_swings)):
            if selected:
                option_rows = option_rows[option_rows[column].isin(selected)]
        option_ready = option_rows[
            option_rows["upper_bound"].notna() &
            option_rows["lower_bound"].notna() &
            option_rows["trigger_allowed"].fillna(False).astype(bool)
        ].copy()

        rows = expression_rows.copy()
        for column, selected in (("ranking_method", rankings),
                                 ("trade_threshold", thresholds),
                                 ("required_minimum_swing_ticks", minimum_swings)):
            if selected:
                rows = rows[rows[column].isin(selected)]
        ready = rows[
            rows["upper_bound"].notna() &
            rows["lower_bound"].notna() &
            rows["trigger_allowed"].fillna(False).astype(bool)
        ].copy()
        if ready.empty:
            empty = go.Figure().update_layout(**COLORS)
            return ("No currently eligible levels match these filters or their "
                    "configured trading-day windows.", empty, [], [], [], [])
        try:
            current = live.expression(symbol, expression)
            live_price = float(current["value"])
            live_text = f"Live price: {live_price:.4f} | Updated: {current['timestamp']}"
        except Exception as error:
            live_price = None
            live_text = f"Live price unavailable: {error}"
        status = html.Div([
            html.B(f"{expression}"), html.Span(
                f" | Level data: {ready['latest_date'].iloc[0]} | {live_text}")])

        def plan_figure(direction):
            long = direction == "LONG"
            source = pd.DataFrame({
                "stop": ready["long_sl" if long else "short_sl"],
                "entry": ready["lower_bound" if long else "upper_bound"],
                "target": ready["long_tp" if long else "short_tp"],
                "trades": ready["selected_trades"],
                "wins": ready["selected_win_rate"] * ready["selected_trades"],
                "robust": ready["selected_robust_win_rate"] * ready["selected_trades"],
                "case": ready["shortlist_case"],
            })
            plans = source.groupby(["stop", "entry", "target"], as_index=False).agg(
                supporting_cases=("case", "count"), trades=("trades", "sum"),
                wins=("wins", "sum"), robust=("robust", "sum"),
                cases=("case", lambda values: ", ".join(map(str, values))))
            plans["accuracy"] = plans["wins"] / plans["trades"]
            plans["robust_accuracy"] = plans["robust"] / plans["trades"]
            plans = plans.sort_values(
                ["accuracy", "supporting_cases"], ascending=False
            ).reset_index(drop=True)
            plans["row"] = [
                f"P{i + 1}  |  {row.accuracy:.1%}"
                for i, row in plans.iterrows()
            ]
            support = plans["supporting_cases"]
            spread = support.max() - support.min()
            marker_sizes = (10 + 22 * (support - support.min()) / spread
                            if spread else pd.Series(20, index=plans.index))
            figure = go.Figure()
            evidence = plans[["cases", "supporting_cases", "accuracy",
                              "robust_accuracy", "trades"]]
            for label, column, marker_symbol in (
                ("SL", "stop", "x"),
                ("Entry", "entry", "circle"),
                ("TP", "target", "diamond"),
            ):
                figure.add_trace(go.Scatter(
                    x=plans["row"], y=plans[column], mode="markers", name=label,
                    marker={"color": plans["accuracy"], "size": marker_sizes,
                            "symbol": marker_symbol,
                            "colorscale": "RdYlGn", "cmin": plans["accuracy"].min(),
                            "cmax": plans["accuracy"].max(), "showscale": label == "Entry",
                            "colorbar": {"title": "Historical<br>win rate",
                                         "tickformat": ".0%", "x": 1.01,
                                         "len": 0.78}},
                    customdata=evidence,
                    hovertemplate=(f"{direction} {label}<br>Level: %{{y:.4f}}"
                                   "<br>Cases: %{customdata[0]}"
                                   "<br>Supporting cases: %{customdata[1]}"
                                   "<br>Weighted win rate: %{customdata[2]:.1%}"
                                   "<br>Weighted robust rate: %{customdata[3]:.1%}"
                                   "<br>Total trades: %{customdata[4]:,.0f}<extra></extra>")))
            if live_price is not None:
                figure.add_hline(y=live_price, line_color="#22c55e", line_dash="dot",
                                 annotation_text=f"Live price {live_price:.4f}")
            figure.update_layout(title=(f"{direction} consensus plans | {len(plans)} unique "
                                        f"from {len(ready)} cases"),
                                 xaxis_title="Consensus plan",
                                 yaxis_title="Expression price level",
                                 uirevision=f"trigger-levels:{expression}:{direction}",
                                 autosize=True, height=675,
                                 legend={"orientation": "h", "yanchor": "bottom",
                                         "y": 1.02, "xanchor": "left", "x": 0},
                                 margin={"l": 65, "r": 78, "t": 95, "b": 60},
                                 **COLORS)
            figure.update_xaxes(categoryorder="array",
                                categoryarray=plans["row"].tolist(), showgrid=False)
            figure.update_yaxes(showgrid=False)
            return figure
        hourly_chart = hourly_figure(symbol, expression, ready, live_price)

        ready["weighted_wins"] = ready["selected_win_rate"] * ready["selected_trades"]
        consensus = ready.groupby(
            ["upper_bound", "lower_bound", "tp_ticks", "short_tp", "short_sl",
             "long_tp", "long_sl"], as_index=False).agg(
                 supporting_cases=("shortlist_case", "count"),
                 weighted_wins=("weighted_wins", "sum"),
                 accuracy_weight=("selected_trades", "sum"),
                 case_numbers=("shortlist_case", lambda values: ", ".join(map(str, values))))
        consensus["weighted_accuracy"] = (
            consensus["weighted_wins"] / consensus["accuracy_weight"]
        )
        consensus = consensus.sort_values("supporting_cases", ascending=False)
        consensus["weighted_accuracy"] = consensus["weighted_accuracy"].map(
            lambda value: f"{value:.1%}"
        )
        consensus_columns = ["supporting_cases", "weighted_accuracy", "upper_bound",
                             "lower_bound", "tp_ticks", "short_tp", "short_sl",
                             "long_tp", "long_sl", "case_numbers"]
        option_columns = ["expression", "direction", "entry_status",
                          "distance_ticks", "entry",
                          "live_price", "tp", "sl", "tp_sl_ticks",
                          "supporting_cases", "weighted_accuracy", "robust_accuracy",
                          "historical_trades", "case_numbers"]
        options = []
        for option_expression, eligible in option_ready.groupby("expression"):
            try:
                option_live = float(live.expression(symbol, option_expression)["value"])
            except Exception:
                continue
            for direction in ("LONG", "SHORT"):
                long = direction == "LONG"
                source = pd.DataFrame({
                    "entry": eligible["lower_bound" if long else "upper_bound"],
                    "tp": eligible["long_tp" if long else "short_tp"],
                    "sl": eligible["long_sl" if long else "short_sl"],
                    "tp_sl_ticks": eligible["tp_ticks"],
                    "trades": eligible["selected_trades"],
                    "wins": eligible["selected_win_rate"] * eligible["selected_trades"],
                    "robust": (eligible["selected_robust_win_rate"]
                               * eligible["selected_trades"]),
                    "case": eligible["shortlist_case"],
                })
                grouped = source.groupby(
                    ["entry", "tp", "sl", "tp_sl_ticks"], as_index=False
                ).agg(supporting_cases=("case", "count"),
                      historical_trades=("trades", "sum"),
                      weighted_wins=("wins", "sum"), weighted_robust=("robust", "sum"),
                      case_numbers=("case", lambda values: ", ".join(map(str, values))))
                grouped["weighted_accuracy_value"] = (
                    grouped["weighted_wins"] / grouped["historical_trades"])
                grouped["robust_accuracy_value"] = (
                    grouped["weighted_robust"] / grouped["historical_trades"])
                grouped["reached"] = (option_live <= grouped["entry"] if long
                                      else option_live >= grouped["entry"])
                raw_distance = ((option_live - grouped["entry"]) if long
                                else (grouped["entry"] - option_live)) / TICK_SIZES[symbol]
                grouped["distance_ticks"] = np.ceil(np.maximum(0, raw_distance)).astype(int)
                for row in grouped.itertuples():
                    entry_status = ("AT/BEYOND ENTRY" if row.reached else
                                    "ABOUT TO REACH" if row.distance_ticks <= 2
                                    else "WAITING")
                    options.append({
                        "expression": option_expression,
                        "direction": direction,
                        "entry_status": entry_status,
                        "distance_ticks": row.distance_ticks,
                        "entry": round(row.entry, 4),
                        "live_price": round(option_live, 4),
                        "tp": round(row.tp, 4), "sl": round(row.sl, 4),
                        "tp_sl_ticks": int(row.tp_sl_ticks),
                        "supporting_cases": int(row.supporting_cases),
                        "weighted_accuracy": f"{row.weighted_accuracy_value:.1%}",
                        "robust_accuracy": f"{row.robust_accuracy_value:.1%}",
                        "historical_trades": int(row.historical_trades),
                        "case_numbers": row.case_numbers,
                        "_is_reached": bool(row.reached),
                        "_accuracy": row.weighted_accuracy_value,
                    })
        selected_options = [row for row in options
                            if row["_is_reached"] or row["distance_ticks"] in (1, 2)]
        selected_options.sort(key=lambda row: (
            not row["_is_reached"], row["distance_ticks"], -row["_accuracy"],
            -row["supporting_cases"]))
        unique_options, seen_expressions = [], set()
        for row in selected_options:
            if row["expression"] not in seen_expressions:
                unique_options.append(row)
                seen_expressions.add(row["expression"])
        now = monotonic()
        with opportunity_lock:
            for key, (_, expires) in list(opportunity_cache.items()):
                if expires <= now:
                    del opportunity_cache[key]
            current_keys = set()
            for row in unique_options:
                key = (symbol, bucket, row["expression"])
                current_keys.add(key)
                if key not in opportunity_cache:
                    opportunity_cache[key] = (
                        row.copy(), now + MINIMUM_OPPORTUNITY_SECONDS
                    )
            retained = []
            for key, (row, _) in opportunity_cache.items():
                if key[:2] != (symbol, bucket):
                    continue
                output = row.copy()
                if key not in current_keys:
                    output["entry_status"] = "RECENT SIGNAL"
                retained.append(output)
        retained.sort(key=lambda row: (
            row["entry_status"] == "RECENT SIGNAL", not row["_is_reached"],
            row["distance_ticks"], -row["_accuracy"], -row["supporting_cases"]))
        options = [{key: value for key, value in row.items() if not key.startswith("_")}
                   for row in retained]
        return (status, hourly_chart,
                [{"name": c.replace("_", " ").title(), "id": c} for c in consensus_columns],
                consensus[consensus_columns].round(4).to_dict("records"),
                [{"name": c.replace("_", " ").title(), "id": c}
                 for c in option_columns], options)
    return app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="data/trigger_levels")
    parser.add_argument("--port", type=int, default=8053)
    arguments = parser.parse_args()
    create_app(load_levels(arguments.input)).run(
        host="127.0.0.1", port=arguments.port, debug=False)


if __name__ == "__main__":
    main()

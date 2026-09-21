"""Simple dashboard for hourly_shortlist_results.xlsx."""

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from dash import Dash, Input, Output, ctx, dash_table, dcc, html


THEME = {"paper_bgcolor": "#020617", "plot_bgcolor": "#020617",
         "font_color": "#e2e8f0"}
PANEL = {"background": "#111827", "border": "1px solid #334155",
         "borderRadius": "8px", "padding": "14px", "margin": "12px auto",
         "maxWidth": "1500px"}
INPUT = {"color": "#111827", "width": "100%"}


def load_results(path):
    source = Path(path)
    if not source.exists():
        raise FileNotFoundError(f"Hourly report not found: {source.resolve()}")
    workbook = pd.ExcelFile(source)
    trades = (pd.read_excel(workbook, sheet_name="Trades")
              if "Trades" in workbook.sheet_names else pd.DataFrame())
    return (pd.read_excel(workbook, sheet_name="Setting_Summary"),
            pd.read_excel(workbook, sheet_name="Expression_Results"), trades)


def choices(values):
    return [{"label": str(value), "value": value}
            for value in sorted(pd.Series(values).dropna().unique())]


def mark_current_workbook(source, symbol, bucket, cases):
    with pd.ExcelFile(source) as workbook:
        sheets = {sheet: pd.read_excel(workbook, sheet_name=sheet)
                  for sheet in workbook.sheet_names}
    for frame in sheets.values():
        frame["use_currently"] = 0
        required = {"symbol", "bucket", "shortlist_case"}
        if required.issubset(frame.columns):
            selected = ((frame["symbol"] == symbol) & (frame["bucket"] == bucket) &
                        frame["shortlist_case"].isin(cases))
            frame.loc[selected, "use_currently"] = 1
    handle, temporary = tempfile.mkstemp(
        prefix=f".{source.stem}.", suffix=".xlsx", dir=source.parent)
    os.close(handle)
    try:
        with pd.ExcelWriter(temporary, engine="openpyxl") as writer:
            for sheet, frame in sheets.items():
                frame.to_excel(writer, sheet_name=sheet, index=False)
        os.replace(temporary, source)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def mark_current_settings(path, symbol, bucket, cases):
    source = Path(path)
    workbooks = sorted(source.glob("*.xlsx")) if source.is_dir() else [source]
    for workbook in workbooks:
        mark_current_workbook(workbook, symbol, bucket, cases)


def calculate_latest_triggers(shortlist_path, output_path):
    command = [sys.executable, "-m", "parameter_testing.generate_trigger_levels",
               "--input", str(shortlist_path), "--output", str(output_path)]
    completed = subprocess.run(
        command, cwd=Path(__file__).parent, capture_output=True, text=True,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    if completed.returncode:
        message = completed.stderr.strip().splitlines()
        raise RuntimeError(message[-1] if message else "Trigger calculation failed")


def hourly_result_path(shortlist_path, output_dir):
    return Path(output_dir) / f"{Path(shortlist_path).stem}_hourly.xlsx"


def trigger_result_path(shortlist_path, output_dir):
    return Path(output_dir) / f"{Path(shortlist_path).stem}_triggers.xlsx"


def run_hourly_test(shortlist_path, output_path):
    command = [sys.executable, "-m", "parameter_testing.testing_parameters_hourly",
               "--input", str(shortlist_path), "--output", str(output_path)]
    completed = subprocess.run(
        command, cwd=Path(__file__).parent, capture_output=True, text=True,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    if completed.returncode:
        message = completed.stderr.strip().splitlines()
        raise RuntimeError(message[-1] if message else "Hourly test failed")


def empty_figure(message="No results match these filters"):
    figure = go.Figure().add_annotation(text=message, showarrow=False)
    figure.update_layout(**THEME, height=470)
    return figure


def create_app(summary, expressions, trades, best_dir, hourly_output_dir,
               trigger_output_dir):
    shortlist_files = sorted(Path(best_dir).glob("*.xlsx"))
    if not shortlist_files:
        raise FileNotFoundError(f"No best-parameter files found in {Path(best_dir).resolve()}")
    shortlist_index = pd.concat(
        [pd.read_excel(path, sheet_name="All_Settings", usecols=["symbol", "bucket"])
         for path in shortlist_files], ignore_index=True)
    symbols = sorted(shortlist_index["symbol"].dropna().unique())
    buckets = sorted(shortlist_index["bucket"].dropna().unique())
    app = Dash(__name__)
    app.title = "Hourly Tests"
    app.layout = html.Div([
        html.H2("Hourly Strategy Results", style={"textAlign": "center"}),
        html.P("Shortlisted settings tested on completed hourly candles; exact fills and exits use minute prices.",
               style={"textAlign": "center", "color": "#94a3b8"}),
        html.Div([
            html.Label(["Best-parameter file", dcc.Dropdown(
                id="shortlist-file",
                options=[{"label": path.name, "value": str(path)}
                         for path in shortlist_files],
                value=str(shortlist_files[0]), clearable=False, style=INPUT)]),
            html.Button("Run hourly test", id="run-hourly-test", n_clicks=0,
                        style={"height": "38px", "padding": "0 20px",
                               "fontWeight": "bold"}),
            html.Div(id="hourly-test-status", style={"color": "#86efac"}),
        ], style={**PANEL, "display": "grid",
                  "gridTemplateColumns": "minmax(280px, 1fr) auto minmax(280px, 1fr)",
                  "gap": "14px", "alignItems": "end"}),
        html.Div([
            html.Label(["Symbol", dcc.Dropdown(
                id="symbol", options=choices(symbols), value=symbols[0],
                clearable=False, style=INPUT)]),
            html.Label(["Bucket", dcc.Dropdown(
                id="bucket", options=choices(buckets), value=buckets[0],
                clearable=False, style=INPUT)]),
            html.Label(["Average tick profit greater than", dcc.Input(
                id="minimum-average-pnl", type="number", value=None, step=0.1,
                debounce=True, style={"width": "100%", "height": "36px"})]),
            html.Label(["Win rate greater than (%)", dcc.Input(
                id="minimum-win-rate", type="number", value=None,
                min=0, max=100, step=1,
                debounce=True, style={"width": "100%", "height": "36px"})]),
        ], style={**PANEL, "display": "grid",
                  "gridTemplateColumns": "repeat(auto-fit, minmax(190px, 1fr))",
                  "gap": "12px"}),
        html.Div([
            html.Button("Use filtered settings", id="use-filtered-settings",
                        n_clicks=0, style={"padding": "10px 18px",
                                           "fontWeight": "bold"}),
            html.Span(id="use-currently-status", style={"color": "#86efac"}),
            html.Button("Calculate latest trigger levels", id="calculate-triggers",
                        n_clicks=0, style={"padding": "10px 18px",
                                           "fontWeight": "bold"}),
            html.Span(id="trigger-status", style={"color": "#86efac"}),
        ], style={"display": "flex", "justifyContent": "center", "gap": "14px",
                  "alignItems": "center"}),
        html.Div(id="kpis", style={"display": "flex", "justifyContent": "center",
                                    "flexWrap": "wrap", "gap": "10px"}),
        html.Div([
            dcc.Loading(dcc.Graph(id="settings-chart", config={"displaylogo": False})),
            dcc.Loading(dcc.Graph(id="contracts-chart", config={"displaylogo": False})),
        ], style={"display": "grid", "gridTemplateColumns":
                  "repeat(auto-fit, minmax(600px, 1fr))", "maxWidth": "1800px",
                  "margin": "0 auto"}),
        dcc.Loading(dcc.Graph(id="expiry-frequency-chart",
                              config={"displaylogo": False})),
        dcc.Loading(dcc.Graph(id="expiry-hours-chart",
                              config={"displaylogo": False})),
        html.H3("Shortlisted settings", style={"textAlign": "center"}),
        dash_table.DataTable(
            id="settings-table", page_size=20, sort_action="native",
            filter_action="native", style_table={"maxWidth": "1500px", "margin": "auto",
                                                    "overflowX": "auto"},
            style_header={"backgroundColor": "#1e293b", "color": "white",
                          "fontWeight": "bold"},
            style_cell={"backgroundColor": "#0f172a", "color": "white",
                        "border": "1px solid #334155", "padding": "7px",
                        "minWidth": "95px", "textAlign": "center"}),
        html.H3("Available contract results", style={"textAlign": "center",
                                                       "marginTop": "28px"}),
        dash_table.DataTable(
            id="contracts-table", page_size=20, sort_action="native",
            filter_action="native", style_table={"maxWidth": "1250px", "margin": "auto",
                                                    "overflowX": "auto"},
            style_header={"backgroundColor": "#1e293b", "color": "white",
                          "fontWeight": "bold"},
            style_cell={"backgroundColor": "#0f172a", "color": "white",
                        "border": "1px solid #334155", "padding": "7px",
                        "minWidth": "100px", "textAlign": "center"}),
    ], style={"background": "#020617", "color": "white", "minHeight": "100vh",
              "padding": "10px 18px 40px"})

    @app.callback(
        Output("symbol", "options"), Output("symbol", "value"),
        Output("bucket", "options"), Output("bucket", "value"),
        Input("shortlist-file", "value"),
    )
    def select_shortlist(shortlist_file):
        frame = pd.read_excel(shortlist_file, sheet_name="All_Settings")
        selected_symbols = sorted(frame["symbol"].dropna().unique())
        selected_buckets = sorted(frame["bucket"].dropna().unique())
        return (choices(selected_symbols), selected_symbols[0],
                choices(selected_buckets), selected_buckets[0])

    @app.callback(
        Output("kpis", "children"), Output("settings-chart", "figure"),
        Output("contracts-chart", "figure"),
        Output("expiry-frequency-chart", "figure"),
        Output("expiry-hours-chart", "figure"),
        Output("settings-table", "columns"),
        Output("settings-table", "data"), Output("contracts-table", "columns"),
        Output("contracts-table", "data"), Output("use-currently-status", "children"),
        Output("trigger-status", "children"),
        Output("hourly-test-status", "children"),
        Input("symbol", "value"),
        Input("bucket", "value"), Input("minimum-average-pnl", "value"),
        Input("minimum-win-rate", "value"),
        Input("use-filtered-settings", "n_clicks"),
        Input("calculate-triggers", "n_clicks"),
        Input("shortlist-file", "value"), Input("run-hourly-test", "n_clicks"))
    def update(symbol, bucket, minimum_average_pnl, minimum_win_rate,
               _use_clicks, _trigger_clicks, shortlist_file, _run_clicks):
        nonlocal summary, expressions, trades
        hourly_status = ""
        selected_shortlist = Path(shortlist_file)
        selected_result = hourly_result_path(selected_shortlist, hourly_output_dir)
        if ctx.triggered_id == "run-hourly-test":
            try:
                run_hourly_test(selected_shortlist, selected_result)
                summary, expressions, trades = load_results(selected_result)
                hourly_status = f"Hourly test saved to {selected_result}."
            except Exception as error:
                hourly_status = f"Hourly test failed: {error}"
        elif ctx.triggered_id == "shortlist-file" and selected_result.exists():
            summary, expressions, trades = load_results(selected_result)
        settings = summary[(summary["symbol"] == symbol) &
                           (summary["bucket"] == bucket)].copy()
        detail = expressions[(expressions["symbol"] == symbol) &
                             (expressions["bucket"] == bucket)].copy()
        selected_trades = trades.copy()
        if not selected_trades.empty:
            selected_trades = selected_trades[
                (selected_trades["symbol"] == symbol) &
                (selected_trades["bucket"] == bucket)]
        if minimum_average_pnl is not None:
            settings = settings[
                settings["average_pnl_ticks"] > float(minimum_average_pnl)]
        if minimum_win_rate is not None:
            settings = settings[settings["win_rate"] > float(minimum_win_rate) / 100]
        valid_cases = set(settings["shortlist_case"])
        save_status = ""
        trigger_status = ""
        if ctx.triggered_id == "use-filtered-settings":
            try:
                mark_current_settings(selected_shortlist, symbol, bucket, valid_cases)
                save_status = f"Marked {len(valid_cases)} settings as currently in use."
            except PermissionError:
                save_status = ("Could not update the shortlist. Close it in Excel and "
                               "wait for OneDrive syncing to finish, then try again.")
        if ctx.triggered_id == "calculate-triggers":
            try:
                trigger_output = trigger_result_path(
                    selected_shortlist, trigger_output_dir)
                calculate_latest_triggers(selected_shortlist, trigger_output)
                trigger_status = f"Latest trigger levels saved to {trigger_output}."
            except Exception as error:
                trigger_status = f"Trigger calculation failed: {error}"
        detail = detail[detail["shortlist_case"].isin(valid_cases)]
        if not selected_trades.empty:
            selected_trades = selected_trades[
                selected_trades["shortlist_case"].isin(valid_cases)]
        if settings.empty:
            return ([], empty_figure(), empty_figure(), empty_figure(),
                    empty_figure(), [], [], [], [], save_status, trigger_status,
                    hourly_status)

        settings = settings.sort_values(["total_pnl_ticks", "win_rate"],
                                        ascending=False)
        shown = settings.head(15).copy()
        shown["setting"] = "S" + shown["shortlist_case"].astype(int).astype(str)
        bar = px.bar(shown.sort_values("total_pnl_ticks"),
                     x="total_pnl_ticks", y="setting", orientation="h",
                     color="win_rate", color_continuous_scale="RdYlGn",
                     title=f"Top {len(shown)} settings by Total PnL",
                     hover_data=["resolved", "wins", "losses", "total_pnl_ticks",
                                 "average_pnl_ticks", "maximum_drawdown_ticks"])

        contracts = detail.groupby(["contract", "expression"], as_index=False).agg(
            settings=("shortlist_case", "nunique"), resolved=("resolved", "sum"),
            wins=("wins", "sum"), losses=("losses", "sum"),
            unresolved=("unresolved", "sum"), total_pnl_ticks=("total_pnl_ticks", "sum"),
            total_holding_hours=("total_holding_hours", "sum"))
        contracts["win_rate"] = contracts["wins"] / contracts["resolved"].replace(0, np.nan)
        contracts["average_pnl_ticks"] = (contracts["total_pnl_ticks"] /
                                           contracts["resolved"].replace(0, np.nan))
        contracts["average_holding_hours"] = (contracts["total_holding_hours"] /
                                               contracts["resolved"].replace(0, np.nan))
        contracts.sort_values("total_pnl_ticks", ascending=False, inplace=True)
        contract_chart = px.bar(
            contracts, x="contract", y="total_pnl_ticks", color="win_rate",
            color_continuous_scale="RdYlGn", title="Performance by available contract",
            hover_data=["expression", "settings", "resolved", "wins", "losses",
                        "average_pnl_ticks", "average_holding_hours"])
        for figure in (bar, contract_chart):
            figure.update_layout(**THEME, height=500, margin={"l": 65, "r": 25,
                                                               "t": 65, "b": 55})

        if selected_trades.empty or "days_before_expiry" not in selected_trades:
            expiry_chart = empty_figure(
                "Regenerate the hourly results to populate expiry frequencies")
        else:
            frequency = selected_trades.groupby(
                ["days_before_expiry", "outcome"], as_index=False
            ).size().rename(columns={"size": "trades"})
            expiry_chart = go.Figure()
            for outcome, label, color in (
                ("TP", "Winning trades", "#22c55e"),
                ("SL", "Losing trades", "#ef4444"),
            ):
                outcome_rows = frequency[frequency["outcome"] == outcome]
                expiry_chart.add_bar(
                    x=outcome_rows["days_before_expiry"],
                    y=outcome_rows["trades"], name=label, marker_color=color,
                    offsetgroup=outcome, width=0.4,
                )
            expiry_chart.update_layout(
                **THEME, height=500, barmode="group", bargap=0.2, bargroupgap=0.08,
                title="Trade outcomes by working days before expiry",
                xaxis_title="Working days before expiry",
                yaxis_title="Number of trades",
                margin={"l": 65, "r": 25, "t": 65, "b": 55})

        if selected_trades.empty or "hours_before_expiry" not in selected_trades:
            hours_chart = empty_figure(
                "Regenerate the hourly results to populate hourly frequencies")
        else:
            hourly_frequency = selected_trades.groupby(
                ["hours_before_expiry", "outcome"], as_index=False
            ).size().rename(columns={"size": "trades"})
            hours_chart = go.Figure()
            for outcome, label, color in (
                ("TP", "Winning trades", "#22c55e"),
                ("SL", "Losing trades", "#ef4444"),
            ):
                outcome_rows = hourly_frequency[hourly_frequency["outcome"] == outcome]
                hours_chart.add_bar(
                    x=outcome_rows["hours_before_expiry"],
                    y=outcome_rows["trades"], name=label, marker_color=color,
                    offsetgroup=outcome, width=0.4,
                )
            hours_chart.update_layout(
                **THEME, height=500, barmode="group", bargap=0.2,
                bargroupgap=0.08, title="Trade outcomes by hours before expiry",
                xaxis_title="Hours before expiry", yaxis_title="Number of trades",
                margin={"l": 65, "r": 25, "t": 65, "b": 55})

        best = settings.iloc[0]
        cards = [("Settings", f"{len(settings):,}"),
                 ("Available contracts", f"{contracts['contract'].nunique():,}"),
                 ("Best setting", f"S{int(best['shortlist_case'])}"),
                 ("Best win rate", f"{best['win_rate']:.1%}"),
                 ("Best PnL", f"{best['total_pnl_ticks']:+,.0f} ticks"),
                 ("Resolved trades", f"{settings['resolved'].sum():,.0f}")]
        card_style = {"background": "#111827", "border": "1px solid #334155",
                      "borderRadius": "7px", "padding": "10px 16px",
                      "textAlign": "center"}
        kpis = [html.Div([html.Small(label), html.Div(value,
                    style={"fontSize": "19px", "fontWeight": "bold"})], style=card_style)
                for label, value in cards]

        setting_columns = ["shortlist_case", "ranking_method", "trade_threshold",
                           "required_minimum_swing_ticks", "lookback", "rank",
                           "max_parallel", "max_consecutive", "maximum_trade_days",
                           "no_trade_days", "swing_lookback", "swing_top_count",
                           "minimum_swing_ticks", "contracts", "resolved", "wins",
                           "losses", "unresolved", "win_rate", "total_pnl_ticks",
                           "average_pnl_ticks", "maximum_drawdown_ticks",
                           "average_holding_hours"]
        contract_columns = ["contract", "expression", "settings", "resolved", "wins",
                            "losses", "unresolved", "win_rate", "total_pnl_ticks",
                            "average_pnl_ticks", "average_holding_hours"]
        setting_columns = [column for column in setting_columns if column in settings]
        settings_data = settings[setting_columns].round(4).replace({np.nan: None})
        contracts_data = contracts[contract_columns].round(4).replace({np.nan: None})
        columns = lambda names: [{"name": name.replace("_", " ").title(), "id": name}
                                 for name in names]
        return (kpis, bar, contract_chart, expiry_chart, hours_chart,
                columns(setting_columns),
                settings_data.to_dict("records"), columns(contract_columns),
                contracts_data.to_dict("records"), save_status, trigger_status,
                hourly_status)
    return app


def load_initial_results(output_dir):
    reports = list(Path(output_dir).glob("*.xlsx"))
    if reports:
        newest = max(reports, key=lambda path: path.stat().st_mtime)
        return load_results(newest)
    summary = pd.DataFrame(columns=[
        "symbol", "bucket", "shortlist_case", "average_pnl_ticks", "win_rate"
    ])
    expressions = pd.DataFrame(columns=["symbol", "bucket", "shortlist_case"])
    return summary, expressions, pd.DataFrame()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--best-dir", default="data/best_parameters")
    parser.add_argument("--hourly-output-dir", default="data/hourly_test_results")
    parser.add_argument("--trigger-output-dir", default="data/trigger_levels")
    parser.add_argument("--port", type=int, default=8054)
    arguments = parser.parse_args()
    create_app(*load_initial_results(arguments.hourly_output_dir),
               arguments.best_dir,
               arguments.hourly_output_dir, arguments.trigger_output_dir).run(
        host="127.0.0.1", port=arguments.port, debug=False)


if __name__ == "__main__":
    main()

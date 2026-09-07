import json
from pathlib import Path
import webbrowser
from concurrent.futures import ProcessPoolExecutor, as_completed
from threading import Lock, Thread, Timer

import pandas as pd
import plotly.graph_objects as go
from dash import Dash, Input, Output, State, ctx, dcc, html, dash_table

from analysis.signal_research import scan_trade_ideas
from backtesting.runner import run_backtest
from backtesting.strategies import SeasonalStrategies
from backtesting.sl_tp_strategies import SLTPStrategies
from backtesting.reporting import write_walk_forward_report
from backtesting.parallel_worker import run_expression_batch
from market_data import MarketData


with (Path(__file__).resolve().parent / "data" / "contracts_list.json").open(
    encoding="utf-8"
) as file:
    universe = json.load(file)

db = MarketData()
try:
    db.cursor.execute("SELECT DISTINCT symbol FROM seac_settlements ORDER BY symbol")
    products = [row[0] for row in db.cursor.fetchall()]
finally:
    db.close()

expressions = [
    {"contract": contract, "strategy": strategy, "expression": expression}
    for contract, strategies in universe["contracts"].items()
    for strategy, expression in strategies.items()
]
scanner_lock = Lock()
scanner_state = {"job": 0, "processing": False, "status": "Select a product.",
                 "data": [], "columns": []}
backtest_lock = Lock()
backtest_state = {"job": 0, "processing": False, "status": "",
                  "summary": [], "summary_columns": [], "figure": go.Figure(),
                  "trades": [], "trade_columns": []}


def scan_worker(job, symbol, years, selected_strategies, signal_model):
    collected, errors = [], 0
    selected_expressions = [item for item in expressions
                            if item["strategy"] in selected_strategies]
    batches = [selected_expressions[index:index + 2]
               for index in range(0, len(selected_expressions), 2)]
    for number, batch in enumerate(batches, 1):
        try:
            ideas = scan_trade_ideas(symbol, batch, years=years,
                                     signal_model=signal_model)
        except Exception:
            errors += len(batch)
            ideas = None
        if ideas is not None and not ideas.empty:
            collected.extend(ideas.to_dict("records"))
            collected.sort(key=lambda row: abs(row["Score"]), reverse=True)
        with scanner_lock:
            if job != scanner_state["job"]:
                return
            scanner_state.update({
                "data": collected.copy(),
                "columns": ([{"name": key, "id": key} for key in collected[0]]
                            if collected else []),
                "status": (f"{symbol} ({years} years): processed "
                           f"{min(number * 2, len(selected_expressions))}/"
                           f"{len(selected_expressions)} expressions — {len(collected)} ideas"
                           f" — {errors} skipped"),
            })
    with scanner_lock:
        if job == scanner_state["job"]:
            scanner_state["processing"] = False
            scanner_state["status"] = (f"{symbol} ({years} years): scan complete — {len(collected)} ideas"
                                       f" — {errors} skipped")

def backtest_worker(job, symbol, analysis_years, test_years,
                    transaction_cost, stop_before_expiry, exit_method,
                    selected_strategies):
    selected = [item for item in expressions if item["strategy"] in selected_strategies]
    all_trades, skipped = [], 0
    bracket = "15Y" if analysis_years >= 11 else "10Y" if analysis_years >= 6 else "5Y"
    min_reward_risk = (0.0 if exit_method == "time" else
                       1.0 if exit_method == "symmetric" else 1.5)
    database = MarketData()
    seasonality = Seasonality(database)
    for number, item in enumerate(selected, 1):
        with backtest_lock:
            if job != backtest_state["job"]:
                database.close()
                return
        anchor_year = 2000 + int(item["contract"][1:])
        try:
            cache_key = (symbol, item["expression"],
                         anchor_year - analysis_years + 1, anchor_year, 400)
            seasonal_result = backtest_data_cache.get(cache_key)
            if seasonal_result is None:
                seasonal_result = seasonality.working_day_expression_seasonality(
                    symbol, item["expression"], start_year=cache_key[2],
                    end_year=anchor_year, window_days=400
                )
                backtest_data_cache[cache_key] = seasonal_result
            time_only = exit_method == "time"
            stop_multiplier, target_multiplier = (
                (1.0, 1.0) if exit_method == "symmetric" else (1.0, 2.0)
            )
            result = run_backtest(
                symbol=symbol, data_start_year=anchor_year - analysis_years + 1,
                test_start_year=anchor_year - test_years + 1,
                data_end_year=anchor_year, window_days=400,
                strategy_func=SeasonalStrategies.strict_research,
                features={f"UpRate_{bracket}_10D": {}, f"Expected_{bracket}_10D": {},
                          "TECH_ZScore": {"window": 20}, "LIVE_STD": {"window": 20}},
                strategy_config={"holding_days": 10,
                                 "stop_before_expiry": stop_before_expiry,
                                 "seasonality_bracket": bracket},
                expression=item["expression"],
                engine_config={"max_hold_days": 10, "transaction_cost": transaction_cost,
                               "min_reward_risk": 0.0 if time_only else 1.5,
                               "breakeven_after_r": None if time_only else 1.0,
                               "not_working_days": None if time_only else 4,
                               "sl_tp_strategy": (SLTPStrategies.time_only if time_only
                                                  else SLTPStrategies.feature_based),
                               "sl_tp_config": ({} if time_only else
                                                {"stop_multiplier": stop_multiplier,
                                                 "take_profit_multiplier": target_multiplier})},
                output_file=None,
                database=database, seasonality_result=seasonal_result,
            )
            trades = result["trades"]
            if not trades.empty:
                trades.insert(0, "Contract", item["contract"])
                trades.insert(1, "Strategy", item["strategy"])
                all_trades.append(trades)
        except Exception:
            skipped += 1
        if number % 2 == 0 or number == len(selected):
            snapshot = _backtest_results(
                all_trades, f"Processed {number}/{len(selected)} expressions — {skipped} skipped"
            )
            with backtest_lock:
                if job != backtest_state["job"]:
                    database.close()
                    return
                backtest_state.update(snapshot)
    database.close()
    with backtest_lock:
        if job == backtest_state["job"]:
            report = None
            if all_trades:
                report = write_walk_forward_report(
                    pd.concat(all_trades, ignore_index=True), symbol,
                    {"Analysis years": analysis_years, "Test years": test_years,
                     "Transaction cost": transaction_cost,
                     "Stop before expiry": stop_before_expiry,
                     "Exit method": exit_method,
                     "Minimum reward/risk": 1.5,
                     "Breakeven after R": 1.0,
                     "Not-working exit days": 4,
                     "Seasonality bracket": bracket,
                     "Expression types": ", ".join(selected_strategies)},
                )
            backtest_state["processing"] = False
            backtest_state["status"] = (f"Backtest complete — {len(selected)} expressions"
                                        f" — {skipped} skipped"
                                        f" — Excel: {report}" if report else
                                        f"Backtest complete — no trades — {skipped} skipped")


def backtest_worker(job, symbol, analysis_years, test_years,
                    transaction_cost, stop_before_expiry, exit_method,
                    selected_strategies, worker_count, signal_model):
    selected = [item for item in expressions if item["strategy"] in selected_strategies]
    batches = [selected[index:index + 5] for index in range(0, len(selected), 5)]
    all_trades, skipped, completed = [], 0, 0
    worker_count = max(1, min(10, int(worker_count)))
    executor = ProcessPoolExecutor(max_workers=worker_count)
    futures = {
        executor.submit(run_expression_batch, symbol, batch, analysis_years,
                        test_years, transaction_cost, stop_before_expiry,
                        exit_method, signal_model): len(batch)
        for batch in batches
    }
    cancelled = False
    try:
        for future in as_completed(futures):
            with backtest_lock:
                if job != backtest_state["job"]:
                    cancelled = True
                    break
            batch_size = futures[future]
            completed += batch_size
            try:
                trades, batch_skipped = future.result()
                skipped += batch_skipped
                if not trades.empty:
                    all_trades.append(trades)
            except Exception:
                skipped += batch_size
            snapshot = _backtest_results(
                all_trades,
                f"Processed {completed}/{len(selected)} expressions - {skipped} skipped"
            )
            with backtest_lock:
                if job != backtest_state["job"]:
                    cancelled = True
                    break
                backtest_state.update(snapshot)
    finally:
        executor.shutdown(wait=not cancelled, cancel_futures=cancelled)
    if cancelled:
        return

    available_history = analysis_years - test_years
    bracket = "15Y" if available_history >= 11 else "10Y" if available_history >= 6 else "5Y"
    min_reward_risk = (0.0 if exit_method == "time" else
                       1.0 if exit_method == "symmetric" else 1.5)
    report = None
    if all_trades:
        report = write_walk_forward_report(
            pd.concat(all_trades, ignore_index=True), symbol,
            {"Analysis years": analysis_years, "Test years": test_years,
             "Walk-forward method": "Each test year uses only preceding analysis years",
             "Transaction cost": transaction_cost,
             "Stop before expiry": stop_before_expiry,
             "Exit method": exit_method, "Workers": worker_count, "Batch size": 5,
             "CL/CO expected move filters": "MF > 0.02, MDF > 0.02, MDDF > 0.03",
             "Entry timing": ("Current-year momentum pullback/resumption"
                              if signal_model == "momentum" else
                              "Current-year Z-score re-entry inside 2 SD"
                              if signal_model == "mean_reversion" else
                              "Seasonal direction + opposite 1.6 SD extreme"),
             "Signal model": signal_model,
             "Stop basis": "1 sigma capped at 0.09 + 0.01 stop fill" if symbol.upper() in ("CL", "CO") else "1 sigma",
             "Target cap": 0.1 if symbol.upper() in ("CL", "CO") else "None",
             "Minimum reward/risk": min_reward_risk, "Breakeven": "Disabled",
             "Not-working exit days": "Disabled", "Seasonality bracket": bracket,
             "Expression types": ", ".join(selected_strategies)},
        )
    with backtest_lock:
        if job == backtest_state["job"]:
            backtest_state["processing"] = False
            backtest_state["status"] = (
                f"Backtest complete - {len(selected)} expressions - {skipped} skipped"
                f" - Excel: {report}" if report else
                f"Backtest complete - no trades - {skipped} skipped"
            )


def _backtest_results(all_trades, status):
    trades = pd.concat(all_trades, ignore_index=True) if all_trades else pd.DataFrame()
    figure = go.Figure()
    if trades.empty:
        return {"status": status, "summary": [], "summary_columns": [],
                "figure": figure, "trades": [], "trade_columns": []}
    trades = trades.sort_values("Exit_Date").reset_index(drop=True)
    trades["Cumulative_Move"] = trades["Price_Move"].cumsum()
    drawdown = trades["Cumulative_Move"] - trades["Cumulative_Move"].cummax()
    winners = trades["Price_Move"] > 0
    gross_profit = trades.loc[winners, "Price_Move"].sum()
    gross_loss = -trades.loc[~winners, "Price_Move"].sum()
    summary = pd.DataFrame([{
        "Trades": len(trades), "Win Rate": f"{winners.mean() * 100:.1f}%",
        "Total Move": round(trades["Price_Move"].sum(), 4),
        "Average Move": round(trades["Price_Move"].mean(), 4),
        "Profit Factor": "∞" if gross_loss == 0 else round(gross_profit / gross_loss, 2),
        "Max Drawdown": round(drawdown.min(), 4),
        "Average Hold": round(trades["Days_Held"].mean(), 1),
    }])
    trades["Actual_Exit_Date"] = pd.to_datetime(trades["Exit_Date"])
    trades["Actual_Year"] = trades["Actual_Exit_Date"].dt.year
    for year, yearly in trades.groupby("Actual_Year", sort=True):
        daily = yearly.groupby("Actual_Exit_Date", as_index=False)["Price_Move"].sum()
        daily["Yearly_Cumulative_Move"] = daily["Price_Move"].cumsum()
        actual_dates = daily["Actual_Exit_Date"]
        aligned_dates = pd.to_datetime("2000-" + actual_dates.dt.strftime("%m-%d"))
        figure.add_trace(go.Scatter(
            x=aligned_dates, y=daily["Yearly_Cumulative_Move"],
            customdata=actual_dates.dt.strftime("%Y-%m-%d"),
            mode="lines", name=str(year), line={"width": 1},
            hovertemplate=f"%{{customdata}}<br>Cumulative move: %{{y}}<extra>{year}</extra>",
        ))
    figure.update_layout(
        template="plotly_dark", title="Walk-Forward Cumulative Move by Year",
        xaxis_title="Month / day", yaxis_title="Cumulative move",
        xaxis_tickformat="%b %d", hovermode="x unified",
    )
    return {
        "status": status, "summary": summary.to_dict("records"),
        "summary_columns": [{"name": column, "id": column} for column in summary.columns],
        "figure": figure, "trades": trades.to_dict("records"),
        "trade_columns": [{"name": column.replace("_", " "), "id": column}
                          for column in trades.columns],
    }


app = Dash(__name__)
app.layout = html.Div([
    html.H2("Automatic Trade Idea Scanner"),
    html.Div([
        dcc.Dropdown(products, placeholder="Select product", id="research-product",
                     clearable=True, style={"color": "#111827", "backgroundColor": "white"}),
        dcc.Dropdown(
            id="research-signal-model", clearable=False, value="seasonal_sd",
            options=[{"label": "Strategy 1 - SEAC + 1.6 SD", "value": "seasonal_sd"},
                     {"label": "Strategy 2 - Current Year Momentum", "value": "momentum"},
                     {"label": "Strategy 3 - Current Year Mean Reversion",
                      "value": "mean_reversion"}],
            style={"color": "#111827", "backgroundColor": "white"},
        ),
        html.Label(["Analysis years", dcc.Input(
            id="research-years", type="number", value=10, min=4, max=20, step=1,
            debounce=True, style={"color": "#111827", "backgroundColor": "white"},
        )]),
        html.Div([
            html.Span("Expression types"),
            dcc.Checklist(
                id="research-strategies",
                options=[{"label": strategy, "value": strategy}
                         for strategy in universe["columns"]],
                value=[strategy for strategy in universe["columns"]
                       if not strategy.endswith("MS")],
                inline=True,
            ),
        ], className="research-strategies"),
        html.Button("Run Trade Idea Scan", id="run-research-scan", n_clicks=0),
    ], className="research-controls"),
    html.Div("Select a product to scan all expressions.", id="research-status"),
    dcc.Loading(dash_table.DataTable(
        id="research-ideas", page_size=25, sort_action="native",
        style_table={"overflowX": "auto"},
        style_header={"backgroundColor": "#17243a", "color": "white"},
        style_cell={"backgroundColor": "#111827", "color": "white",
                    "border": "1px solid #354258", "padding": "5px"},
    )),
    dcc.Interval(id="research-refresh", interval=750, n_intervals=0, disabled=True),
    html.H2("Walk-Forward Backtest"),
    html.Div([
        html.Label(["Test years", dcc.Input(id="backtest-years", type="number",
                                            value=5, min=1, max=10, step=1)]),
        html.Label(["Transaction cost", dcc.Input(id="backtest-cost", type="number",
                                                  value=0, min=0, step=0.0001)]),
        html.Label(["Stop entries before expiry", dcc.Input(
            id="backtest-stop-before", type="number", value=20, min=0, max=400, step=1
        )]),
        html.Label(["Exit method", dcc.Dropdown(
            id="backtest-exit-method", clearable=False, value="reward2",
            options=[{"label": "Stop 1σ / Target 2σ", "value": "reward2"},
                     {"label": "Stop 1σ / Target 1σ", "value": "symmetric"},
                     {"label": "Time exit only", "value": "time"}],
            style={"width": "190px", "color": "#111827"},
        )]),
        html.Label(["Workers", dcc.Input(id="backtest-workers", type="number",
                                          value=8, min=1, max=10, step=1)]),
        html.Button("Run Walk-Forward Test", id="run-backtest", n_clicks=0),
    ], className="backtest-controls"),
    html.Div(id="backtest-status"),
    dcc.Loading(html.Div([
        dash_table.DataTable(
            id="backtest-summary",
            style_header={"fontWeight": "bold", "backgroundColor": "#17243a", "color": "white"},
            style_cell={"backgroundColor": "#111827", "color": "white",
                        "border": "1px solid #354258", "padding": "5px"}),
        dcc.Graph(id="backtest-equity"),
        dash_table.DataTable(
            id="backtest-trades", page_size=20, sort_action="native",
            style_table={"overflowX": "auto"},
            style_header={"backgroundColor": "#17243a", "color": "white"},
            style_cell={"backgroundColor": "#111827", "color": "white",
                        "border": "1px solid #354258", "padding": "5px"}),
    ])),
    dcc.Interval(id="backtest-refresh", interval=1000, n_intervals=0, disabled=True),
], className="research-page")


@app.callback(
    Output("research-status", "children"), Output("research-ideas", "data"),
    Output("research-ideas", "columns"), Output("research-refresh", "disabled"),
    Input("run-research-scan", "n_clicks"), Input("research-refresh", "n_intervals"),
    State("research-product", "value"), State("research-years", "value"),
    State("research-strategies", "value"), State("research-signal-model", "value"),
    prevent_initial_call=True,
)
def scan_product(_, __, symbol, years, selected_strategies, signal_model):
    if ctx.triggered_id == "research-refresh":
        with scanner_lock:
            return (scanner_state["status"], scanner_state["data"],
                    scanner_state["columns"], not scanner_state["processing"])
    if not symbol:
        return "Select a product to scan all expressions.", [], [], True
    years = max(4, min(20, int(years or 10)))
    selected_strategies = selected_strategies or []
    if not selected_strategies:
        return "Select at least one expression type.", [], [], True
    with scanner_lock:
        job = scanner_state["job"] + 1
        scanner_state.update({"job": job, "processing": True, "data": [], "columns": [],
                              "status": f"{symbol} ({years} years): starting scan..."})
    Thread(target=scan_worker, args=(job, symbol, years, selected_strategies,
                                    signal_model or "seasonal_sd"),
           daemon=True).start()
    return f"{symbol} ({years} years): starting scan...", [], [], False


@app.callback(
    Output("backtest-status", "children"), Output("backtest-summary", "data"),
    Output("backtest-summary", "columns"), Output("backtest-equity", "figure"),
    Output("backtest-trades", "data"), Output("backtest-trades", "columns"),
    Output("backtest-refresh", "disabled"),
    Input("run-backtest", "n_clicks"), Input("backtest-refresh", "n_intervals"),
    State("research-product", "value"),
    State("research-years", "value"), State("research-strategies", "value"),
    State("backtest-years", "value"), State("backtest-cost", "value"),
    State("backtest-stop-before", "value"),
    State("backtest-exit-method", "value"), State("backtest-workers", "value"),
    State("research-signal-model", "value"),
    prevent_initial_call=True,
)
def run_walk_forward(_, __, symbol, analysis_years, selected_strategies,
                     test_years, transaction_cost, stop_before_expiry, exit_method,
                     worker_count, signal_model):
    if ctx.triggered_id == "backtest-refresh":
        with backtest_lock:
            return (backtest_state["status"], backtest_state["summary"],
                    backtest_state["summary_columns"], backtest_state["figure"],
                    backtest_state["trades"], backtest_state["trade_columns"],
                    not backtest_state["processing"])
    if not symbol or not selected_strategies:
        return "Select a product and expression types first.", [], [], go.Figure(), [], [], True
    with backtest_lock:
        job = backtest_state["job"] + 1
        backtest_state.update({"job": job, "processing": True,
                               "status": "Starting walk-forward backtest...",
                               "summary": [], "summary_columns": [], "figure": go.Figure(),
                               "trades": [], "trade_columns": []})
    Thread(target=backtest_worker,
           args=(job, symbol, int(analysis_years or 10), int(test_years or 5),
                 float(transaction_cost or 0), int(stop_before_expiry or 0),
                 exit_method or "reward2", selected_strategies,
                 max(1, min(10, int(worker_count or 8))),
                 signal_model or "seasonal_sd"), daemon=True).start()
    return "Starting walk-forward backtest...", [], [], go.Figure(), [], [], False


if __name__ == "__main__":
    Timer(1, lambda: webbrowser.open("http://127.0.0.1:8051")).start()
    app.run(host="127.0.0.1", port=8051, debug=False, threaded=True)

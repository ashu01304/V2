"""Live Chart 2 signals from saved best settings."""
from pathlib import Path
import pandas as pd
import plotly.graph_objects as go
from dash import Dash, Input, Output, dash_table, dcc, html
from analysis.contract_universe import DEFAULT_EXPIRY_PATH
from CL_CO_testing.config import DEFAULT_SYMBOL
from CL_CO_testing.engine import load_intraday_series, ranked_chart
from CL_CO_testing.charts import make_intraday_figure
from CL_CO_testing.universe import UNIVERSE, expression_rows
from market_data import LiveMarketDataClient

SETTINGS = Path(__file__).with_name("best_chart_2_settings.xlsx")
CONTRACTS = [row["Contract"] for row in expression_rows()[:7]]

def create_app():
    settings = pd.read_excel(SETTINGS)
    expiry_frame = pd.read_csv(DEFAULT_EXPIRY_PATH)
    expiries = expiry_frame[expiry_frame.symbol == "CO"].set_index("contract_code").expiry_date
    live, app = LiveMarketDataClient(), Dash(__name__)
    app.title = "CL-CO Live Signals"
    panel = {"background": "#111827", "border": "1px solid #334155",
             "borderRadius": "8px", "padding": "15px", "margin": "12px auto",
             "maxWidth": "1500px"}
    input_style = {"width": "100%", "color": "#111827"}
    app.layout = html.Div([
        html.H2("CL-CO 1MF Live Signals", style={"textAlign": "center"}),
        html.P("Live Chart 2 signals filtered by the saved best settings.",
               style={"textAlign": "center", "color": "#94a3b8"}),
        html.Div([
            html.Label(["Contracts", dcc.Dropdown(
                id="contracts", options=CONTRACTS, value=CONTRACTS, multi=True,
                style=input_style)]),
            html.Label(["Timeframes", dcc.Dropdown(
                id="timeframes", options=sorted(settings.timeframe.unique()),
                value=sorted(settings.timeframe.unique()), multi=True,
                style=input_style)]),
            dcc.Checklist(id="assume", value=["yes"], options=[{
                "label": " Assume every valid signal is executed", "value": "yes"}]),
        ], style={**panel, "display": "grid",
                  "gridTemplateColumns": "repeat(3, minmax(220px, 1fr))",
                  "gap": "15px", "alignItems": "end"}),
        html.Div(id="status", style=panel),
        dash_table.DataTable(id="signals", sort_action="native", filter_action="native",
            fixed_rows={"headers": True},
            style_table={"width": "fit-content", "maxWidth": "calc(100vw - 40px)",
                         "margin": "0 auto", "overflowX": "auto"},
            style_header={"backgroundColor": "#1e293b", "color": "white"},
            style_cell={"backgroundColor": "#0f172a", "color": "white",
                        "border": "1px solid #334155", "padding": "7px",
                        "minWidth": "105px", "textAlign": "center"},
            style_data_conditional=[
                {"if": {"filter_query": "{Signal} = LONG", "column_id": "Signal"},
                 "color": "#4ade80", "fontWeight": "bold"},
                {"if": {"filter_query": "{Signal} = SHORT", "column_id": "Signal"},
                 "color": "#f87171", "fontWeight": "bold"}]),
        dcc.Graph(id="contract-chart"),
        dcc.Interval(id="refresh", interval=10_000, n_intervals=0),
    ], style={"background": "#020617", "color": "white", "minHeight": "100vh",
              "padding": "10px 20px 40px"})

    @app.callback(Output("status", "children"), Output("signals", "data"),
                  Output("signals", "columns"), Output("contract-chart", "figure"),
                  Output("contract-chart", "style"),
                  Input("refresh", "n_intervals"),
                  Input("assume", "value"), Input("contracts", "value"),
                  Input("timeframes", "value"), Input("signals", "active_cell"))
    def refresh(_, assume, selected_contracts, selected_timeframes, active_cell):
        load_intraday_series.cache_clear()
        now, rows, results = pd.Timestamp.now(tz="UTC"), [], {}
        for contract in selected_contracts or []:
            expression = UNIVERSE["contracts"][contract]["1MF"]
            try:
                price = (float(live.expression("CL", expression)["value"]) -
                         float(live.expression("CO", expression)["value"]))
                expiry = pd.Timestamp(expiries[contract], tz="UTC")
                evaluations = []
                for setting in settings.itertuples(index=False):
                    if int(setting.timeframe) not in (selected_timeframes or []):
                        continue
                    candles = load_intraday_series(DEFAULT_SYMBOL, expression,
                                                   int(setting.timeframe))
                    first = expiry - pd.Timedelta(days=int(setting.days_before_expiry))
                    last = expiry - pd.Timedelta(days=int(setting.no_trade_days))
                    result = ranked_chart({
                        "candles": candles, "tp_ticks": int(setting.tp_sl_ticks),
                        "sl_ticks": int(setting.tp_sl_ticks),
                        "symbol": DEFAULT_SYMBOL, "expression": expression,
                        "timeframe_minutes": int(setting.timeframe),
                        "z_score_threshold": 0, "z_score_lookback": 0,
                        "minimum_trade_gap": int(setting.minimum_trade_gap),
                        "max_parallel_trades": int(setting.max_parallel),
                        "max_consecutive_direction": int(setting.max_consecutive),
                        "first_entry_time": first, "last_entry_time": last,
                    }, int(setting.rank_lookback), int(setting.band_rank))
                    upper, lower = result["rank_upper"].iloc[-1], result["rank_lower"].iloc[-1]
                    signal = "SHORT" if price >= upper else "LONG" if price <= lower else "WAIT"
                    open_count = sum(t["outcome"] == "OPEN" for t in result["trades"])
                    reason = "Band reached" if signal != "WAIT" else "Inside bands"
                    if signal != "WAIT" and "yes" in (assume or []):
                        streak = 0
                        for trade in reversed(result["trades"]):
                            if trade["direction"] != signal: break
                            streak += 1
                        if not first <= now < last: signal, reason = "WAIT", "Outside window"
                        elif open_count >= setting.max_parallel: signal, reason = "WAIT", "Max parallel"
                        elif (result["trades"] and now < result["trades"][-1]["entry_time"] +
                              pd.Timedelta(minutes=int(setting.timeframe) *
                                           int(setting.minimum_trade_gap))):
                            signal, reason = "WAIT", "Minimum gap"
                        elif streak >= setting.max_consecutive: signal, reason = "WAIT", "Direction limit"
                    evaluations.append((signal, result, upper, lower, open_count))
                if not evaluations:
                    continue
                long_count = sum(item[0] == "LONG" for item in evaluations)
                short_count = sum(item[0] == "SHORT" for item in evaluations)
                signal = ("LONG" if long_count > short_count else
                          "SHORT" if short_count > long_count else "WAIT")
                active = [item for item in evaluations if item[0] == signal]
                chosen = active[0] if active else evaluations[0]
                results[contract] = chosen[1]
                rows.append({
                    "Contract": contract, "Signal": signal,
                    "Support": f"{long_count} LONG | {short_count} SHORT",
                    "Settings": len(evaluations), "Live": round(price, 2),
                    "Upper": round(chosen[2], 2), "Lower": round(chosen[3], 2),
                    "Open": chosen[4],
                })
            except Exception:
                continue
        rows.sort(key=lambda row: row.get("Signal") == "WAIT")
        columns = [{"name": key, "id": key} for key in rows[0]] if rows else []
        figure = go.Figure()
        if rows and active_cell:
            selected = rows[min(active_cell.get("row", 0), len(rows) - 1)]
            result = results.get(selected.get("Contract"))
            if result:
                figure = make_intraday_figure(result, ranked=True)
        status = f"Active signals: {sum(r.get('Signal') in ('LONG', 'SHORT') for r in rows)}"
        return status, rows, columns, figure, ({"display": "block"} if figure.data
                                               else {"display": "none"})
    return app

if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=8059, debug=False)

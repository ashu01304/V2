"""Live saved-band dashboard for the first five active CL-CO 1MF contracts."""
import json
import math
from pathlib import Path
from threading import Lock
import pandas as pd
import plotly.graph_objects as go
from dash import Dash, Input, Output, dash_table, dcc, html
from analysis.contract_universe import DEFAULT_EXPIRY_PATH
from CL_CO_testing.config import DEFAULT_SYMBOL
from CL_CO_testing.engine import load_intraday_series, ranked_bands, ranked_chart
from CL_CO_testing.charts import make_intraday_figure
from CL_CO_testing.universe import UNIVERSE, expression_rows
from market_data import LiveMarketDataClient

ROOT = Path(__file__).parent
SETTINGS_FILE = ROOT / "best_chart_2_settings.xlsx"
LEVELS_FILE = ROOT / "live_band_levels.json"


def active_contracts():
    expiries = pd.read_csv(DEFAULT_EXPIRY_PATH)
    expiries = expiries[expiries.symbol == "CO"].set_index("contract_code").expiry_date
    now = pd.Timestamp.now(tz="UTC").normalize()
    return [row["Contract"] for row in expression_rows()
            if row["Contract"] in expiries
            and pd.Timestamp(expiries[row["Contract"]], tz="UTC") > now][:5]


CONTRACTS = active_contracts()
HITS = {}

def signal_style(contract, state, ticks):
    if ticks < 1:
        return [{"selector": f'td[data-dash-column="{contract}"]',
                 "rule": "background-color: #0f172a !important; font-weight: normal; animation: none !important; opacity: 1 !important;"}]
    color = "#166534" if state == "BUY" else "#991b1b"
    speed = min(ticks - 1, 3)
    animation = (f"band-cell-pulse {1 / speed:.6f}s steps(1, end) infinite"
                 if speed else "none")
    return [{"selector": f'td[data-dash-column="{contract}"]',
             "rule": (f"background-color: {color} !important; font-weight: bold; "
                      f"animation: {animation};")}]


def better_ticks(price, boundary, state):
    distance = boundary - price if state == "BUY" else price - boundary
    return max(0, math.floor(distance / 0.01 + 1e-8))


def live_price(client, expression):
    cl = float(client.expression("CL", expression)["value"])
    try:
        co = float(client.expression("CO", expression)["value"])
    except Exception:
        co = float(client.expression("LCO", expression)["value"])
    return round(cl - co, 2)

def refresh_levels(settings, now=None):
    now = pd.Timestamp.now(tz="UTC") if now is None else now
    bucket = int(now.timestamp() // 900)
    load_intraday_series.cache_clear()
    levels = []
    for contract in CONTRACTS:
        expression = UNIVERSE["contracts"][contract]["1MF"]
        for setting in settings.itertuples(index=False):
            try:
                candles = load_intraday_series(DEFAULT_SYMBOL, expression,
                                               int(setting.timeframe))
                upper, lower = ranked_bands(candles, int(setting.rank_lookback),
                                            int(setting.band_rank))
                levels.append({"Contract": contract, "Run": int(setting.run_id),
                    "Timeframe": int(setting.timeframe), "TP/SL": int(setting.tp_sl_ticks),
                    "Lookback": int(setting.rank_lookback), "Rank": int(setting.band_rank),
                    "Upper": round(upper.iloc[-1], 2), "Lower": round(lower.iloc[-1], 2),
                    "Win rate": float(setting.win_rate), "Trades": int(setting.trades),
                    "Calculated": str(candles.index[-1])})
            except Exception:
                continue
    LEVELS_FILE.write_text(json.dumps({"version": 3, "bucket": bucket,
                                       "refreshed_at": now.isoformat(),
                                       "levels": levels}, indent=2),
                           encoding="utf-8")
    return pd.DataFrame(levels)

def hourly_figure(contract, price, setting, expiry):
    expression = UNIVERSE["contracts"][contract]["1MF"]
    timeframe = int(setting.timeframe)
    candles = load_intraday_series(DEFAULT_SYMBOL, expression, timeframe)
    result = ranked_chart({
        "candles": candles, "tp_ticks": int(setting.tp_sl_ticks),
        "sl_ticks": int(setting.tp_sl_ticks), "symbol": DEFAULT_SYMBOL,
        "expression": expression, "timeframe_minutes": timeframe, "expiry": expiry,
        "z_score_threshold": 0, "z_score_lookback": 0,
        "minimum_trade_gap": int(setting.minimum_trade_gap),
        "max_parallel_trades": int(setting.max_parallel),
        "max_consecutive_direction": int(setting.max_consecutive),
        "first_entry_time": expiry - pd.Timedelta(days=int(setting.days_before_expiry)),
        "last_entry_time": expiry - pd.Timedelta(days=int(setting.no_trade_days)),
    }, int(setting.rank_lookback), int(setting.band_rank))
    figure = make_intraday_figure(result, ranked=True, show_average=False)
    figure.add_trace(go.Scatter(x=[candles.index[-1]], y=[price], mode="markers",
        name="Live price", marker={"color": "#22c55e", "size": 7}))
    figure.update_layout(uirevision=f"live:{contract}:{int(setting.run_id)}")
    return figure, result

class HalfHourlyBandState:
    """Reload settings and database candles together on the first tick of each half hour."""

    def __init__(self):
        self.lock = Lock()
        self.bucket = None

    def get(self, now):
        bucket = int(now.timestamp() // 900)
        with self.lock:
            if bucket != self.bucket:
                settings = pd.read_excel(SETTINGS_FILE)
                expiry_frame = pd.read_csv(DEFAULT_EXPIRY_PATH)
                levels = refresh_levels(settings, now)
                self.settings, self.expiry_frame, self.levels = settings, expiry_frame, levels
                self.refreshed_at = now
                self.bucket = bucket
                HITS.clear()
            return self.settings, self.expiry_frame, self.levels, self.refreshed_at


def create_app():
    band_state = HalfHourlyBandState()
    client = LiveMarketDataClient()
    app = Dash(__name__)
    app.title = "⬛ ⬛ ⬛ ⬛ ⬛"
    app.index_string = app.index_string.replace("</head>", """
        <style>
        @keyframes band-cell-pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.35; }
        }
        </style></head>""")
    panel = {"background": "#111827", "border": "1px solid #334155",
             "borderRadius": "8px", "padding": "15px", "margin": "12px auto",
             "maxWidth": "1500px"}
    app.layout = html.Div([
        html.H2("CL-CO 1MF Live Bands", style={"textAlign": "center"}),
        html.Div(id="status", style=panel),
        dash_table.DataTable(id="contracts", columns=[{"name": c, "id": c} for c in CONTRACTS],
            data=[{c: "WAIT" for c in CONTRACTS}], cell_selectable=True,
            style_table={"maxWidth": "1500px", "margin": "auto"},
            style_header={"backgroundColor": "#1e293b", "color": "white",
                          "fontWeight": "bold"},
            style_cell={"backgroundColor": "#0f172a", "color": "white",
                        "padding": "15px", "textAlign": "center", "minWidth": "140px"}),
        html.Div(id="chart-summary", style={"display": "flex", "gap": "28px",
                 "padding": "12px 20px", "background": "#0f172a"}),
        dcc.Graph(id="chart", style={"display": "none"}),
        html.H3(id="levels-title", style={"textAlign": "center"}),
        dash_table.DataTable(id="levels", sort_action="native",
            style_table={"maxWidth": "1500px", "margin": "auto", "overflowX": "auto"},
            style_header={"backgroundColor": "#1e293b", "color": "white"},
            style_cell={"backgroundColor": "#0f172a", "color": "white",
                        "padding": "7px", "textAlign": "center"}),
        dcc.Interval(id="timer", interval=10_000, n_intervals=0),
        dcc.Store(id="favicon-state"),
        html.Div(id="favicon-updated", style={"display": "none"}),
    ], style={"background": "#020617", "color": "white", "minHeight": "100vh",
              "padding": "10px 20px 40px"})

    app.clientside_callback(
        """
        function(states) {
            window.bandFaviconStates = states || [];
            function drawBandTitle() {
                window.bandFaviconTick = (window.bandFaviconTick || 0) + 1;
                const tick = window.bandFaviconTick;
                const states = (window.bandFaviconStates || []).slice(0, 5);
                document.title = states.map(item => {
                    const speed = Math.min(Math.max(item.ticks - 1, 0), 3);
                    const visible = !speed || Math.floor(tick / (4 - speed)) % 2 === 0;
                    if (!visible) return '⬛';
                    return item.state === 'BUY' ? '🟩'
                         : item.state === 'SELL' ? '🟥' : '⬛';
                }).join(' ');
            }
            drawBandTitle();
            if (!window.bandFaviconTimer) {
                window.bandFaviconTimer = setInterval(drawBandTitle, 150);
            }
            return String(Date.now());
        }
        """,
        Output("favicon-updated", "children"), Input("favicon-state", "data"),
    )

    @app.callback(Output("contracts", "data"), Output("contracts", "css"),
        Output("chart", "figure"), Output("chart", "style"),
        Output("chart-summary", "children"),
        Output("levels", "data"), Output("levels", "columns"),
        Output("levels-title", "children"), Output("status", "children"),
        Output("favicon-state", "data"),
        Input("timer", "n_intervals"), Input("contracts", "active_cell"))
    def update(_, active):
        now, prices = pd.Timestamp.now(tz="UTC"), {}
        settings, expiry_frame, levels, refreshed_at = band_state.get(now)
        row, styles, favicon_state = {}, [], []
        for contract in CONTRACTS:
            ticks, current_state = 0, "WAIT"
            subset = levels[levels.Contract == contract] if not levels.empty else levels
            try:
                prices[contract] = live_price(client, UNIVERSE["contracts"][contract]["1MF"])
                buy_ticks = subset.Lower.map(lambda bound: better_ticks(prices[contract], bound, "BUY"))
                sell_ticks = subset.Upper.map(lambda bound: better_ticks(prices[contract], bound, "SELL"))
                buy, sell = (buy_ticks >= 1).any(), (sell_ticks >= 1).any()
                if buy != sell:
                    state = "BUY" if buy else "SELL"
                    plans = subset[buy_ticks >= 1] if buy else subset[sell_ticks >= 1]
                    plan = plans.iloc[0]
                    entry = plan.Lower if buy else plan.Upper
                    current_state = state
                    ticks = better_ticks(prices[contract], entry, state)
                    distance = plan["TP/SL"] * 0.01
                    tp = entry + distance if buy else entry - distance
                    sl = entry - distance if buy else entry + distance
                    HITS[contract] = (state, now + pd.Timedelta(minutes=5),
                                      round(entry, 2), round(tp, 2), round(sl, 2))
            except Exception:
                pass
            if ticks < 1:
                HITS.pop(contract, None)
            hit = HITS.get(contract)
            state = hit[0] if hit and hit[1] > now else "WAIT"
            row[contract] = (f"{state} | Entry {hit[2]:.2f} | TP {hit[3]:.2f} | "
                             f"SL {hit[4]:.2f}" if state != "WAIT" else "WAIT")
            if ticks >= 1:
                row[contract] += f" | {ticks} ticks better"
            styles.extend(signal_style(contract, current_state, ticks))
            favicon_state.append({"state": current_state if ticks >= 1 else "WAIT",
                                  "ticks": ticks})
        figure, chart_style, summary = go.Figure(), {"display": "none"}, []
        data, columns, title = [], [], ""
        selected = active.get("column_id") if active else None
        if (selected in CONTRACTS and selected in prices and not levels.empty
                and (levels.Contract == selected).any()):
            selected_levels = levels[levels.Contract == selected]
            run = int(selected_levels.iloc[0]["Run"])
            setting = settings[settings.run_id == run].iloc[0]
            expiry = pd.Timestamp(expiry_frame[
                (expiry_frame.symbol == "CO") &
                (expiry_frame.contract_code == selected)].expiry_date.iloc[0], tz="UTC")
            figure, result = hourly_figure(selected, prices[selected], setting, expiry)
            chart_style = {"display": "block", "width": "80%", "margin": "auto"}
            summary = [html.Span(f"Total trades: {result['total_trades']}"),
                html.Span(f"Resolved: {result['resolved_trades']}"),
                html.Span(f"Open: {result['open_trades']}"),
                html.Span(f"Win rate: {result['win_rate']:.1%}" if result["win_rate"] is not None else "Win rate: N/A"),
                html.Span(f"Total PnL: {result['total_pnl_ticks']:+d} ticks")]
            plans = []
            for level in selected_levels.to_dict("records"):
                distance = level["TP/SL"] * 0.01
                for direction, entry, sign in (("BUY", level["Lower"], 1),
                                                ("SELL", level["Upper"], -1)):
                    plans.append({"Direction": direction, "Entry": entry,
                        "TP": round(entry + sign * distance, 2),
                        "SL": round(entry - sign * distance, 2),
                        "Trades": level["Trades"],
                        "Weighted wins": level["Win rate"] * level["Trades"]})
            plans = pd.DataFrame(plans)
            grouped = plans.groupby(["Direction", "Entry", "TP", "SL"],
                as_index=False).agg(Settings=("Trades", "size"),
                    Trades=("Trades", "sum"), Weighted_wins=("Weighted wins", "sum"))
            grouped["Weighted accuracy"] = grouped["Weighted_wins"] / grouped["Trades"]
            grouped.drop(columns="Weighted_wins", inplace=True)
            grouped["Weighted accuracy"] = grouped["Weighted accuracy"].map(
                lambda value: f"{value:.1%}")
            data = grouped.to_dict("records")
            columns = [{"name": c.replace("_", " "), "id": c} for c in grouped.columns]
            title = f"{selected} grouped entry plans"
        latest_candle = levels["Calculated"].max() if not levels.empty else "unavailable"
        status = (f"Levels: {len(levels)} | Bands/settings refreshed: "
                  f"{refreshed_at:%Y-%m-%d %H:%M:%S UTC} | Latest candle: {latest_candle}"
                  f" | Live refresh: {now:%Y-%m-%d %H:%M:%S UTC}")
        return ([row], styles, figure, chart_style, summary, data, columns, title,
                status, favicon_state)
    return app

if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=8059, debug=False)

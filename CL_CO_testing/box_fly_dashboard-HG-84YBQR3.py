"""Current-session minute box flies compared across products and contracts."""

from datetime import date

import pandas as pd
import plotly.graph_objects as go
from dash import Dash, Input, Output, Patch, State, ctx, dcc, html, no_update

from analysis.contract_universe import generate_universe
from market_data.config import DATA_DIR
from market_data import LiveMarketDataClient


UNIVERSE = generate_universe(as_of=date.today())[0]


SYMBOL_LEGS = {
    "CL-CO": (("CL", "CL", 1), ("LCO", "CO", -1)),
    "CL": (("CL", "CL", 1),), "CO": (("LCO", "CO", 1),),
    "NG": (("NG", "NG", 1),), "HO": (("HO", "HO", 1),),
    "RB": (("RB", "RB", 1),), "FCPO": (("FCPO", "FCPO", 1),),
}
STRUCTURES = {
    "Spread": "1MS", "Fly": "1MF",
    "Defly": "1MDF", "Double defly": "1MDDF",
}
PRICE_DECIMALS = {"NG": 3}


def available_contracts():
    available = {}
    expiries = pd.read_csv(DATA_DIR / "expiry_dates.csv")
    expiries["expiry_date"] = pd.to_datetime(expiries["expiry_date"], utc=True)
    today = pd.Timestamp.now(tz="UTC").normalize()
    for symbol in SYMBOL_LEGS:
        expiry_symbol = "CO" if symbol == "CL-CO" else symbol
        expiry = expiries[expiries.symbol == expiry_symbol].set_index(
            "contract_code")["expiry_date"]
        structures = {}
        for label, strategy in STRUCTURES.items():
            contracts = [contract for contract, expressions in UNIVERSE["contracts"].items()
                         if expressions.get(strategy)
                         and not (contract in expiry and expiry[contract] <= today)]
            if contracts:
                structures[label] = contracts[:10]
        if structures:
            available[symbol] = structures
    return available


CONTRACTS = available_contracts()
app = Dash(__name__)
app.title = "Box Fly Minute Data"
app.layout = html.Div([
    html.H2(id="page-title"),
    html.Div([
        html.Label(["Symbol", dcc.Dropdown(
            id="symbol", options=list(CONTRACTS), value="CL-CO",
            clearable=False, persistence=True, persistence_type="local",
            style={"width": "140px", "color": "black"})]),
        html.Label(["Structure", dcc.Dropdown(
            id="structure", options=list(STRUCTURES), value="Fly",
            clearable=False, persistence=True, persistence_type="local",
            style={"width": "160px", "color": "black"})]),
        html.Label(["Contracts", dcc.Dropdown(
            id="contract", options=CONTRACTS.get("CL-CO", {}).get("Fly", []),
            value=CONTRACTS.get("CL-CO", {}).get("Fly", [])[:1], multi=True,
            persistence=True, persistence_type="local",
            style={"width": "600px", "maxWidth": "80vw", "color": "black"})]),
        dcc.Checklist(id="zero-base", options=[{"label": " Zero base", "value": "yes"}],
                      value=["yes"], persistence=True, persistence_type="local"),
        html.Label(["Past N days", dcc.Input(
            id="past-days", type="number", value=0, min=0, step=1,
            debounce=True, persistence=True, persistence_type="local",
            style={"display": "block", "width": "100px"})]),
        html.Button("Refresh", id="refresh", n_clicks=0),
    ], style={"display": "flex", "gap": "20px", "alignItems": "end"}),
    html.Div(id="status", style={"padding": "15px 0"}),
    dcc.Graph(id="chart"),
    dcc.Graph(id="continuous-chart"),
    dcc.Interval(id="live-refresh", interval=10_000, n_intervals=0),
], style={"background": "#0f172a", "color": "white", "minHeight": "100vh",
          "padding": "20px"})


@app.callback(Output("contract", "options"), Output("contract", "value"),
              Output("page-title", "children"), Input("symbol", "value"),
              Input("structure", "value"),
              State("contract", "value"))
def select_symbol(symbol, structure, selected):
    options = CONTRACTS.get(symbol, {}).get(structure, [])
    selected = [contract for contract in selected or [] if contract in options]
    return options, selected or options[:1], f"{symbol} {structure} | Change From Open"


@app.callback(Output("chart", "figure"), Output("continuous-chart", "figure"),
              Output("status", "children"),
              Input("symbol", "value"),
              Input("structure", "value"),
              Input("contract", "value"),
              Input("zero-base", "value"),
              Input("past-days", "value"),
              Input("refresh", "n_clicks"),
              Input("live-refresh", "n_intervals"),
              State("chart", "figure"), State("continuous-chart", "figure"))
def update_chart(symbol, structure, contracts, zero_base, past_days, _, __,
                 chart_state, continuous_state):
    decimals = PRICE_DECIMALS.get(symbol, 2)
    if ctx.triggered_id == "live-refresh" and chart_state and continuous_state:
        chart_patch, continuous_patch = Patch(), Patch()
        end = pd.Timestamp.now(tz="UTC")
        local_now = end.tz_convert("America/Chicago").tz_localize(None)
        session_open = (local_now - pd.Timedelta(hours=17)).normalize() + pd.Timedelta(hours=17)
        session_date = (session_open + pd.Timedelta(days=1)).date()
        client = LiveMarketDataClient()
        try:
            for contract in contracts or []:
                expression = UNIVERSE["contracts"][contract][STRUCTURES[structure]]
                try:
                    quotes = [(coefficient, client.expression(live, expression))
                              for _, live, coefficient in SYMBOL_LEGS[symbol]]
                    price = sum(coefficient * float(quote["value"])
                                for coefficient, quote in quotes)
                    timestamp = min(pd.to_datetime(quote["timestamp"], utc=True)
                                    for _, quote in quotes)
                except Exception:
                    continue
                local = timestamp.tz_convert("America/Chicago").tz_localize(None)
                minute = (local.hour * 60 + local.minute - 17 * 60) % 1440
                top_open = next((float(trace["customdata"][0][1])
                                 for trace in chart_state["data"]
                                 if trace.get("name") == f"{contract} | {session_date}"), price)
                period_open = next((float(trace["customdata"][0][1])
                                    for trace in continuous_state["data"]
                                    if trace.get("name") == f"{contract} | {expression}"), price)
                customdata = [[local.strftime("%Y-%m-%d %H:%M"), price]]
                for index, trace in enumerate(chart_state["data"]):
                    if trace.get("name") == f"{contract} live":
                        chart_patch["data"][index]["x"] = [minute]
                        chart_patch["data"][index]["y"] = [round(price - top_open, decimals)
                                                            if zero_base else round(price, decimals)]
                        chart_patch["data"][index]["customdata"] = customdata
                for index, trace in enumerate(continuous_state["data"]):
                    if trace.get("name") == f"{contract} live":
                        continuous_patch["data"][index]["x"] = [local.isoformat()]
                        continuous_patch["data"][index]["y"] = [round(price - period_open, decimals)
                                                                 if zero_base else round(price, decimals)]
                        continuous_patch["data"][index]["customdata"] = customdata
        finally:
            client.close()
        return chart_patch, continuous_patch, no_update
    figure = go.Figure()
    continuous = go.Figure()
    figure.update_layout(template="plotly_dark", height=750,
                         paper_bgcolor="#0f172a", plot_bgcolor="#0f172a",
                         uirevision="daily-overlay",
                         hovermode="x unified", xaxis_title="Session time (Chicago; opens 17:00)",
                         xaxis=dict(range=[0, 1380], tickmode="array",
                                    tickvals=list(range(0, 1380, 120)) + [1380],
                                    ticktext=[f"{(17 + hour) % 24:02d}:00"
                                              for hour in range(0, 23, 2)] + ["16:00"]),
                         yaxis_title="Change from open",
                         yaxis_tickformat=f".{decimals}f")
    continuous.update_layout(
        template="plotly_dark", height=750, paper_bgcolor="#0f172a",
        plot_bgcolor="#0f172a", hovermode="x unified", uirevision="continuous",
        xaxis_title="Time (Chicago)", xaxis_tickformat="%d %b<br>%H:%M",
        yaxis_tickformat=f".{decimals}f")
    try:
        if not contracts:
            raise ValueError("Select at least one contract")
        if past_days is None or past_days < 0 or not float(past_days).is_integer():
            raise ValueError("Past N days must be a non-negative whole number")
        past_days = int(past_days)
        end = pd.Timestamp.now(tz="UTC")
        local_now = end.tz_convert("America/Chicago").tz_localize(None)
        session_open = (local_now - pd.Timedelta(hours=17)).normalize() + pd.Timedelta(hours=17)
        period_start = session_open - pd.Timedelta(days=past_days)
        start = period_start.tz_localize("America/Chicago").tz_convert("UTC")
        session_close = (session_open + pd.Timedelta(hours=23)).tz_localize(
            "America/Chicago").tz_convert("UTC")
        session_date = (session_open + pd.Timedelta(days=1)).date()
        if session_open.dayofweek in (4, 5):
            raise ValueError("Market closed: no current trading session")
        client = LiveMarketDataClient()
        missing, openings, points, daily_lines = [], [], 0, 0
        try:
            for contract in contracts:
                expression = UNIVERSE["contracts"][contract][STRUCTURES[structure]]
                try:
                    series = {}
                    for product, live, coefficient in SYMBOL_LEGS[symbol]:
                        frame = pd.DataFrame(client.expression_history(
                            live, expression, start=start.isoformat(),
                            end=min(end, session_close).isoformat()))
                        if frame.empty:
                            raise ValueError(f"No {live} data")
                        frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
                        series[live] = frame.set_index("timestamp")["price"] * coefficient
                    aligned = pd.concat(series, axis=1, join="inner").dropna().sort_index()
                    aligned = aligned[aligned.index < session_close]
                    if aligned.empty:
                        raise ValueError(f"No common {symbol} minute data")
                except Exception as error:
                    missing.append(f"{contract}: {error}")
                    continue
                prices = aligned.sum(axis=1)
                local = prices.index.tz_convert("America/Chicago").tz_localize(None)
                period_opening = prices.iloc[0]
                continuous.add_trace(go.Scattergl(
                    x=local,
                    y=((prices - period_opening) if zero_base else prices).round(decimals),
                    mode="lines+markers", name=f"{contract} | {expression}",
                    customdata=list(zip(local.strftime("%Y-%m-%d %H:%M"),
                                        [period_opening] * len(prices))),
                    line={"width": 1.5}, marker={"size": 6},
                    hovertemplate=(f"%{{customdata[0]}} CT<br>Value: %{{y:+.{decimals}f}}"
                                   f"<br>Start: %{{customdata[1]:.{decimals}f}}"
                                   "<extra>%{fullData.name}</extra>")))
                sessions = pd.DataFrame({
                    "price": prices,
                    "time": local,
                    "minute": (local.hour * 60 + local.minute - 17 * 60) % 1440,
                    "day": (local - pd.Timedelta(hours=17) + pd.Timedelta(days=1)).date,
                })
                sessions = sessions[sessions["minute"] < 1380]
                for day, values in sessions.groupby("day"):
                    opening = values["price"].iloc[0]
                    openings.append(f"{contract} {day}: {opening:.{decimals}f}")
                    figure.add_trace(go.Scattergl(
                        x=values["minute"],
                        y=((values["price"] - opening) if zero_base
                           else values["price"]).round(decimals),
                        mode="lines+markers", name=f"{contract} | {day}",
                        customdata=list(zip(values["time"].dt.strftime("%Y-%m-%d %H:%M"),
                                            [opening] * len(values))),
                        line={"width": 1.5,
                              **({"color": "white"} if day == session_date else {})},
                        marker={"size": 6,
                                **({"color": "white"} if day == session_date else {})},
                        hovertemplate=(f"%{{customdata[0]}} CT<br>Value: %{{y:+.{decimals}f}}"
                                       f"<br>Open: %{{customdata[1]:.{decimals}f}}"
                                       "<extra>%{fullData.name}</extra>")))
                    daily_lines += 1
                    if day == session_date:
                        latest = values.iloc[-1]
                        figure.add_trace(go.Scattergl(
                            x=[latest["minute"]],
                            y=[round(latest["price"] - opening, decimals)
                               if zero_base else round(latest["price"], decimals)],
                            mode="markers", name=f"{contract} live", showlegend=False,
                            marker={"size": 14, "color": "#22c55e",
                                    "line": {"color": "white", "width": 2}},
                            customdata=[[latest["time"].strftime("%Y-%m-%d %H:%M"),
                                         latest["price"]]],
                            hovertemplate=(f"%{{customdata[0]}} CT<br>Live price: "
                                           f"%{{customdata[1]:.{decimals}f}}"
                                           "<extra>%{fullData.name}</extra>")))
                        continuous.add_trace(go.Scattergl(
                            x=[latest["time"]],
                            y=[round(latest["price"] - period_opening, decimals)
                               if zero_base else round(latest["price"], decimals)],
                            mode="markers", name=f"{contract} live", showlegend=False,
                            marker={"size": 14, "color": "#22c55e",
                                    "line": {"color": "white", "width": 2}},
                            customdata=[[latest["time"].strftime("%Y-%m-%d %H:%M"),
                                         latest["price"]]],
                            hovertemplate=(f"%{{customdata[0]}} CT<br>Live price: "
                                           f"%{{customdata[1]:.{decimals}f}}"
                                           "<extra>%{fullData.name}</extra>")))
                    points += len(values)
        finally:
            client.close()
        if zero_base:
            figure.add_hline(y=0, line_width=1, line_color="#94a3b8")
            continuous.add_hline(y=0, line_width=1, line_color="#94a3b8")
        figure.update_layout(
            title=(f"{'Change From Open' if zero_base else 'Structure Level'} | "
                   f"{symbol} {structure} | Daily Overlay"),
            yaxis_title="Change from open" if zero_base else f"{symbol} {structure.lower()}")
        continuous.update_layout(
            title=(f"{'Change From Period Start' if zero_base else 'Structure Level'} | "
                   f"{symbol} {structure} | Continuous"),
            yaxis_title="Change from period start" if zero_base else f"{symbol} {structure.lower()}")
        status = (f"{'Past ' + str(past_days) + ' days plus current session' if past_days else 'Session ' + str(session_date)} | "
                  f"{daily_lines} daily lines | {points:,} minute points | "
                  f"Starting values: {', '.join(openings)}")
        if missing:
            status += " | Unavailable: " + "; ".join(missing)
        return figure, continuous, status
    except Exception as error:
        return figure, continuous, str(error)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8055, debug=False)

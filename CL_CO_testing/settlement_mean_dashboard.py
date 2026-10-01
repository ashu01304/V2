"""Minute structures for individual products and the CL-CO difference."""

import sys
import re
import json
import hashlib
import math
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd
import plotly.graph_objects as go
from dash import Dash, Input, Output, State, ctx, dcc, html, no_update

from analysis.contract_universe import (build_universe, generate_universe, load_expiries)
from market_data.chart_updates import preserve_live_charts
from market_data import MarketData
from market_data.config import DATABASE_PATH
from market_data.live_client import LiveMarketDataClient


# Include every month within the configured horizon, including deferred months
# omitted by the shared testing universe. Data availability is checked per chart.
UNIVERSE, universe_config, as_of = generate_universe()
expiries = load_expiries(universe_config.symbol)
anchors = [code for code, expiry in expiries
           if (expiry >= as_of if universe_config.keep_expiry_day else expiry > as_of)
           and expiry.year <= as_of.year + universe_config.years_ahead]
UNIVERSE = build_universe(expiries, anchors, universe_config.custom_structures)
CONTRACTS = list(UNIVERSE["contracts"])
STRUCTURES = UNIVERSE["columns"]


def available_symbols():
    database = MarketData()
    try:
        products = set()
        for instrument in database.spreads():
            match = re.fullmatch(
                r"([A-Z]+)([FGHJKMNQUVXZ]\d{1,2})-([FGHJKMNQUVXZ]\d{1,2})",
                instrument)
            if match:
                products.add(match[1])
    finally:
        database.close()
    aliases = {"LCO": "CO", "LGO": "G", "DBIP": "DBI"}
    mapping = {aliases.get(product, product): (product,)
               for product in sorted(products)}
    mapping.update({"CL": ("CL",), "CO": ("LCO",), "CL-CO": ("CL", "LCO")})
    return mapping


SYMBOL_PRODUCTS = available_symbols()
SYMBOLS = ["CL-CO", "CL", "CO"] + sorted(set(SYMBOL_PRODUCTS) - {"CL-CO", "CL", "CO"})
app = Dash(__name__)
app.title = "Settlement Mean Experiment"
app.layout = html.Div([
    html.H2("Structures | Daily Mean Reversion"),
    html.Div([
        html.Label(["Symbol", dcc.Dropdown(
            id="symbol", options=SYMBOLS, value="CL-CO", clearable=False,
            style={"width": "180px", "color": "black"})]),
        html.Label(["Contracts", dcc.Dropdown(
            id="contract", options=CONTRACTS,
            value=CONTRACTS[:5], multi=True,
            style={"width": "600px", "maxWidth": "80vw", "color": "black"})]),
        html.Label(["Structures", dcc.Dropdown(
            id="structure", options=STRUCTURES, value=["1MF"], multi=True,
            style={"width": "300px", "color": "black"})]),
        dcc.Checklist(id="zero-base", options=[{"label": " Zero base", "value": "yes"}],
                      value=["yes"], persistence=True, persistence_type="local"),
        html.Label(["Past N days", dcc.Input(
            id="past-days", type="number", value=10, min=0, step=1,
            debounce=False, persistence=True, persistence_type="local",
            style={"display": "block", "width": "100px", "color": "black"})]),
        html.Button("Refresh", id="refresh", n_clicks=0),
    ], style={"display": "flex", "gap": "20px", "alignItems": "end", "flexWrap": "wrap"}),
    html.Div(id="data-updated", style={"paddingTop": "15px", "color": "#94a3b8"},
             title="Last modification time of the local master database file."),
    html.Div(id="live-data-updated", style={"paddingTop": "6px", "color": "#94a3b8"},
             title="Latest received quote for the selected symbol from the live service on port 8060; charts use stored minute data."),
    html.Div(id="status", style={"padding": "15px 0"}),
    html.Div([
        html.Label(["Mean start (CT, HH:MM)", dcc.Input(
            id="mean-start", type="text", value="17:00", debounce=True,
            persistence=True, persistence_type="local", style={"display": "block"})]),
        html.Label(["Mean end / analysis start (CT, HH:MM)", dcc.Input(
            id="mean-end", type="text", value="13:30", debounce=True,
            persistence=True, persistence_type="local", style={"display": "block"})]),
        html.Label(["Minimum absolute net move in prior 60 min (ticks)", dcc.Input(
            id="min-pre-move", type="number", value=0, min=0, step="any", debounce=True,
            persistence=True, persistence_type="local", placeholder="Blank disables filter",
            style={"display": "block"})]),
        html.Span("0.01 = 1 tick"),
    ], style={"display": "flex", "gap": "20px", "flexWrap": "wrap"}),
    html.P("Mean = arithmetic average of available minute prices between the selected CT times, "
           "including both boundaries, held fixed for each session. Evening times belong to the previous "
           "calendar day of the session. Analysis starts at the mean end time and runs through the last "
           "available observation before 16:00 CT. All tick values use "
           "0.01 = 1 tick. Past N days counts calendar days plus the current session. "
           "Chicago time observes daylight saving. The reference is a stored minute price, not an official settlement."),
    dcc.Graph(id="mean-chart"),
    html.Div(id="mean-summary"),
    html.Div(id="mean-results", style={"overflowX": "auto", "paddingBottom": "24px"}),
    dcc.Graph(id="chart"),
    dcc.Graph(id="continuous-chart"),
    dcc.Interval(id="live-refresh", interval=10_000, n_intervals=0),
], style={"background": "#0f172a", "color": "white", "minHeight": "100vh",
          "padding": "20px"})


def mean_window_minutes(start_text, end_text):
    def parse(value):
        if not isinstance(value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value.strip()):
            raise ValueError("Mean times must use 24-hour HH:MM, e.g. 17:00 or 13:30.")
        hour, minute = map(int, value.strip().split(":"))
        offset = (hour * 60 + minute - 17 * 60) % 1440
        if offset >= 1380:
            raise ValueError("Mean times must be within the session, 17:00 through 15:59 CT.")
        return offset
    start, end = parse(start_text), parse(end_text)
    if start >= end:
        raise ValueError("Mean start must precede mean end within the 17:00-to-16:00 session.")
    if end >= 1379:
        raise ValueError("Mean end must be before 15:59 CT to leave observations for analysis.")
    return start, end


@app.callback(Output("data-updated", "children"),
              Input("live-refresh", "n_intervals"),
              Input("refresh", "n_clicks"))
def show_database_updated(_, __):
    try:
        updated = pd.Timestamp(DATABASE_PATH.stat().st_mtime, unit="s", tz="UTC")
    except OSError:
        return "Database last updated: unavailable"
    minutes = max(0, int((pd.Timestamp.now(tz="UTC") - updated).total_seconds() // 60))
    age = "less than a minute ago" if minutes == 0 else f"{minutes:,} minutes ago"
    return f"Database last updated: {updated:%Y-%m-%d %H:%M:%S} UTC ({age})"


@app.callback(Output("live-data-updated", "children"),
              Input("live-refresh", "n_intervals"),
              Input("refresh", "n_clicks"),
              Input("symbol", "value"))
def show_live_data_updated(_, __, symbol):
    products = ["CL", "CO"] if symbol == "CL-CO" else [symbol]
    if symbol not in SYMBOL_PRODUCTS:
        return "Live feed last received: select a symbol"
    client = LiveMarketDataClient(timeout=2)
    try:
        health = client.health()
        rows = client.snapshot(products=products)
        timestamps = pd.to_datetime(
            [row.get("received_at") for row in rows], utc=True, errors="coerce")
        timestamps = timestamps.dropna()
        state = health.get("status", "UNKNOWN")
        if timestamps.empty:
            return f"Live feed last received ({symbol}): no quotes received | {state}"
        latest = timestamps.max()
        seconds = max(0, int((pd.Timestamp.now(tz="UTC") - latest).total_seconds()))
        return (f"Live feed last received ({symbol}): {latest:%Y-%m-%d %H:%M:%S} UTC "
                f"({seconds:,} seconds ago) | {state}")
    except Exception:
        return "Live feed last received: unavailable (cannot read live service on port 8060)"
    finally:
        client.close()


@app.callback(Output("chart", "figure"), Output("continuous-chart", "figure"),
              Output("status", "children"),
              Input("symbol", "value"),
              Input("contract", "value"),
              Input("structure", "value"),
              Input("zero-base", "value"),
              Input("past-days", "value"),
              Input("refresh", "n_clicks"),
              Input("live-refresh", "n_intervals"),
              Input("mean-start", "value"), Input("mean-end", "value"),
              State("chart", "figure"), State("continuous-chart", "figure"), State('chart', "figure"), State('continuous-chart', "figure"),
              running=[(Output("live-refresh", "disabled"), True, False)])
@preserve_live_charts([0, 1])
def update_chart(symbol, contracts, structures, zero_base, past_days, _, __, mean_start, mean_end,
                 chart_state=None, continuous_state=None):
    revision = json.dumps([symbol, contracts, structures, bool(zero_base), past_days, mean_start, mean_end])
    # Preserve the existing charts on transient refresh errors only when
    # they already represent the requested parameters.
    live_refresh = (
        set(ctx.triggered_prop_ids) == {"live-refresh.n_intervals"}
        and (chart_state or {}).get("layout", {}).get("uirevision") == "daily-overlay:" + revision
        and (continuous_state or {}).get("layout", {}).get("uirevision") == "continuous:" + revision
    )
    figure = go.Figure()
    continuous = go.Figure()
    figure.update_layout(template="plotly_dark", height=750,
                         paper_bgcolor="#0f172a", plot_bgcolor="#0f172a",
                         uirevision="daily-overlay:" + revision,
                         hovermode="x unified", xaxis_title="Session time (Chicago; opens 17:00)",
                         xaxis=dict(range=[0, 1380], tickmode="array",
                                    tickvals=list(range(0, 1380, 120)) + [1380],
                                    ticktext=[f"{(17 + hour) % 24:02d}:00"
                                              for hour in range(0, 23, 2)] + ["16:00"]),
                         yaxis_title="Change from open", yaxis_tickformat=".4f")
    continuous.update_layout(
        template="plotly_dark", height=750, paper_bgcolor="#0f172a",
        plot_bgcolor="#0f172a", hovermode="x unified", uirevision="continuous:" + revision,
        xaxis_title="Time (Chicago)", xaxis_tickformat="%d %b<br>%H:%M",
        yaxis_tickformat=".4f")
    try:
        mean_start_minute, mean_end_minute = mean_window_minutes(mean_start, mean_end)
        figure.add_vrect(x0=mean_start_minute, x1=mean_end_minute,
                         fillcolor="#38bdf8", opacity=0.08, line_width=0, layer="below")
        figure.add_vline(x=mean_start_minute, line_color="#38bdf8", line_dash="dash",
                         annotation_text=f"Mean start {mean_start} CT", annotation_position="top right")
        figure.add_vline(x=mean_end_minute, line_color="#fbbf24", line_dash="dash",
                         annotation_text=f"Mean end {mean_end} CT", annotation_position="top left")
        if symbol not in SYMBOL_PRODUCTS:
            raise ValueError("Select a valid symbol")
        products = SYMBOL_PRODUCTS[symbol]
        if not contracts:
            raise ValueError("Select at least one contract")
        if not structures:
            raise ValueError("Select at least one structure")
        if any(contract not in UNIVERSE["contracts"] for contract in contracts):
            raise ValueError("Unknown contract selected; reload the page")
        if any(structure not in STRUCTURES for structure in structures):
            raise ValueError("Unknown structure selected; reload the page")
        selections = {
            f"{contract} | {structure}": UNIVERSE["contracts"][contract][structure]
            for contract in contracts
            for structure in structures
        }
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
        if session_open.dayofweek in (4, 5) and past_days == 0:
            raise ValueError("Market closed: no current trading session")
        database = MarketData()
        missing, points = [], 0
        live_client = LiveMarketDataClient(timeout=2)
        try:
            for contract, expression in selections.items():
                try:
                    series = {}
                    for product in products:
                        frame = database.synthetic(product, expression, start=start,
                                                   end=min(end, session_close))
                        frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
                        series[product] = frame.set_index("timestamp")["price"]
                    aligned = pd.concat(series, axis=1, join="inner").dropna().sort_index()
                    aligned = aligned[aligned.index < session_close]
                    if aligned.empty:
                        raise ValueError(f"No aligned {symbol} minute data")
                except ValueError as error:
                    missing.append(f"{contract}: {error}")
                    continue
                prices = (aligned[products[0]] - aligned[products[1]]
                          if len(products) == 2 else aligned[products[0]])
                local = prices.index.tz_convert("America/Chicago").tz_localize(None)
                period_opening = prices.iloc[0]
                continuous.add_trace(go.Scatter(
                    x=[timestamp.isoformat() for timestamp in local],
                    y=((prices - period_opening) if zero_base else prices).round(4).tolist(),
                    mode="lines+markers", name=f"{contract} | {expression}",
                    customdata=list(zip(local.strftime("%Y-%m-%d %H:%M"),
                                        [period_opening] * len(prices))),
                    line={"width": 1.5}, marker={"size": 6},
                    hovertemplate="%{customdata[0]} CT<br>Value: %{y:+.4f}<br>Start: %{customdata[1]:.4f}<extra>%{fullData.name}</extra>"))
                sessions = pd.DataFrame({
                    "price": prices,
                    "time": local,
                    "minute": (local.hour * 60 + local.minute - 17 * 60) % 1440,
                    "day": (local - pd.Timedelta(hours=17) + pd.Timedelta(days=1)).date,
                })
                sessions = sessions[sessions["minute"] < 1380]
                quote = None
                if start <= end < session_close and session_open.dayofweek not in (4, 5):
                    try:
                        quote = live_client.expression(symbol, expression)
                    except Exception as error:
                        missing.append(f"{contract}: live quote unavailable ({error})")
                for day, values in sessions.groupby("day"):
                    opening = values["price"].iloc[0]
                    figure.add_trace(go.Scatter(
                        x=values["minute"].tolist(),
                        y=((values["price"] - opening) if zero_base
                           else values["price"]).round(4).tolist(),
                        mode="lines+markers", name=f"{contract} | {day}",
                        customdata=list(zip(values["time"].dt.strftime("%Y-%m-%d %H:%M"),
                                            [opening] * len(values))),
                        line={"width": 1.5,
                              **({"color": "white"} if day == session_date else {})},
                        marker={"size": 6,
                                **({"color": "white"} if day == session_date else {})},
                        hovertemplate="%{customdata[0]} CT<br>Value: %{y:+.4f}<br>Open: %{customdata[1]:.4f}<extra>%{fullData.name}</extra>"))
                    if day == session_date and quote is not None:
                        quote_time = pd.Timestamp(quote["timestamp"]).tz_convert("America/Chicago").tz_localize(None)
                        latest = {
                            "time": quote_time, "price": float(quote["value"]),
                            "minute": (quote_time.hour * 60 + quote_time.minute - 17 * 60) % 1440,
                        }
                        figure.add_trace(go.Scatter(
                            x=[latest["minute"]],
                            y=[round(latest["price"] - opening, 4)
                               if zero_base else round(latest["price"], 4)],
                            mode="markers", name=f"{contract} live", showlegend=False,
                            marker={"size": 14, "color": "#22c55e",
                                    "line": {"color": "white", "width": 2}},
                            customdata=[[latest["time"].strftime("%Y-%m-%d %H:%M"),
                                         latest["price"]]],
                            hovertemplate="%{customdata[0]} CT<br>Live price: %{customdata[1]:.4f}<extra>%{fullData.name}</extra>"))
                        continuous.add_trace(go.Scatter(
                            x=[latest["time"].isoformat()],
                            y=[round(latest["price"] - period_opening, 4)
                               if zero_base else round(latest["price"], 4)],
                            mode="markers", name=f"{contract} live", showlegend=False,
                            marker={"size": 14, "color": "#22c55e",
                                    "line": {"color": "white", "width": 2}},
                            customdata=[[latest["time"].strftime("%Y-%m-%d %H:%M"),
                                         latest["price"]]],
                            hovertemplate="%{customdata[0]} CT<br>Live price: %{customdata[1]:.4f}<extra>%{fullData.name}</extra>"))
                    points += len(values)
        finally:
            database.close()
            live_client.close()
        if zero_base:
            figure.add_hline(y=0, line_width=1, line_color="#94a3b8")
            continuous.add_hline(y=0, line_width=1, line_color="#94a3b8")
        figure.update_layout(
            title=(f"{'Change From Open' if zero_base else 'Structure Level'} | {symbol} | "
                   f"Daily Overlay"),
            yaxis_title="Change from open" if zero_base else f"{symbol} structure")
        continuous.update_layout(
            title=(f"{'Change From Period Start' if zero_base else 'Structure Level'} | "
                   f"{symbol} | Continuous"),
            yaxis_title="Change from period start" if zero_base else f"{symbol} structure")
        status = ""
        if not points:
            status = f"No {symbol} minute data for the selected period."
        if missing:
            status += (" | " if status else "") + "Unavailable: " + "; ".join(missing)
        for label, chart in (("daily", figure), ("continuous", continuous)):
            for trace in chart.data:
                # Plotly uses UIDs in CSS selectors; keep them alphanumeric.
                identity = json.dumps([symbol, label, trace.name])
                trace.uid = "trace" + hashlib.sha256(identity.encode("utf-8")).hexdigest()
        # Send a complete trace collection so removed contracts disappear.
        # Stable uirevision and trace UIDs preserve the view on timer updates.
        return figure, continuous, status
    except Exception as error:
        if live_refresh:
            return no_update, no_update, str(error)
        return figure, continuous, str(error)


@app.callback(Output("mean-chart", "figure"), Output("mean-summary", "children"),
              Output("mean-results", "children"),
              Input("symbol", "value"), Input("contract", "value"),
              Input("structure", "value"), Input("past-days", "value"),
              Input("mean-start", "value"), Input("mean-end", "value"),
              Input("min-pre-move", "value"), Input("refresh", "n_clicks"),
              Input("live-refresh", "n_intervals"))
def update_mean_experiment(symbol, contracts, structures, past_days, mean_start, mean_end, min_pre_move, _, __):
    tick_size = 0.01
    fig = go.Figure()
    fig.update_layout(template="plotly_dark", height=650,
                      paper_bgcolor="#0f172a", plot_bgcolor="#0f172a",
                      title="Daily distance from the fixed mean",
                      xaxis_title=f"Minutes after {mean_end} CT",
                      yaxis_title="Price minus mean (ticks)", hovermode="closest",
                      uirevision=json.dumps([symbol, contracts, structures, past_days, mean_start, mean_end, tick_size, min_pre_move]))
    fig.add_hline(y=0, line_color="#fbbf24", annotation_text="Daily mean")
    rows, notes = [], []
    try:
        start_minute, end_minute = mean_window_minutes(mean_start, mean_end)
        if min_pre_move is not None:
            min_pre_move = float(min_pre_move)
            if not math.isfinite(min_pre_move) or min_pre_move < 0:
                raise ValueError("Minimum prior-60-minute move must be non-negative, or blank to disable.")
            if end_minute < 60:
                raise ValueError("The selected end time must leave 60 minutes within the session for the move filter.")
        fig.update_xaxes(range=[0, 1380 - end_minute])
        if symbol not in SYMBOL_PRODUCTS or not contracts or not structures:
            raise ValueError("Select a symbol, contracts and structures.")
        if past_days is None or past_days < 0 or not float(past_days).is_integer():
            raise ValueError("Past N days must be a non-negative whole number.")
        end = pd.Timestamp.now(tz="UTC")
        local_now = end.tz_convert("America/Chicago").tz_localize(None)
        opening = (local_now - pd.Timedelta(hours=17)).normalize() + pd.Timedelta(hours=17)
        start = (opening - pd.Timedelta(days=int(past_days))).tz_localize("America/Chicago").tz_convert("UTC")
        products = SYMBOL_PRODUCTS[symbol]
        database = MarketData()
        try:
            for contract in contracts:
                for structure in structures:
                    label = f"{contract} | {structure}"
                    try:
                        expression = UNIVERSE["contracts"][contract][structure]
                        series = {}
                        for product in products:
                            frame = database.synthetic(product, expression, start=start, end=end)
                            frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
                            series[product] = frame.set_index("timestamp")["price"]
                        aligned = pd.concat(series, axis=1, join="inner").dropna().sort_index()
                        aligned = aligned[~aligned.index.duplicated(keep="last")]
                        if aligned.empty:
                            notes.append(f"{label}: no aligned minute data")
                            continue
                        prices = (aligned[products[0]] - aligned[products[1]]
                                  if len(products) == 2 else aligned[products[0]])
                        local = prices.index.tz_convert("America/Chicago").tz_localize(None)
                        data = pd.DataFrame({"price": prices.to_numpy(), "time": local,
                                             "minute": (local.hour * 60 + local.minute - 1020) % 1440,
                                             "day": (local - pd.Timedelta(hours=17) + pd.Timedelta(days=1)).date})
                        data = data[data["minute"] < 1380]
                        for day, values in data.groupby("day"):
                            window = values[values["minute"].between(start_minute, end_minute)]
                            after = values[values["minute"] > end_minute]
                            if window.empty:
                                notes.append(f"{label} {day}: no observations in the mean window")
                                continue
                            if after.empty:
                                notes.append(f"{label} {day}: no observations after {mean_end} CT")
                                continue
                            # Sparse series need not contain the exact boundary minute.
                            # Use only information available within the mean window.
                            reference = window.iloc[-1]
                            mean = float(window["price"].mean())
                            baseline = float(reference["price"])
                            if min_pre_move is not None:
                                # Last known price at/before each boundary; never use a future quote.
                                hour_start = values[values["minute"] <= end_minute - 60]
                                if hour_start.empty:
                                    notes.append(f"{label} {day}: no price at/before the start of the prior 60-minute window")
                                    continue
                                if int(reference["minute"]) <= end_minute - 60:
                                    notes.append(f"{label} {day}: no price update during the prior 60-minute window")
                                    continue
                                hour_reference = hour_start.iloc[-1]
                                pre_move = abs(baseline - float(hour_reference["price"])) / tick_size
                                if pre_move <= min_pre_move or math.isclose(pre_move, min_pre_move, rel_tol=1e-12, abs_tol=1e-9):
                                    notes.append(
                                        f"{label} {day}: prior-hour move {pre_move:.2f} ticks "
                                        f"does not exceed {min_pre_move:g} "
                                        f"(quotes {hour_reference['time']:%H:%M} to {reference['time']:%H:%M} CT)")
                                    continue
                            gap = baseline - mean
                            post = values[values["minute"] >= end_minute]
                            final_gap = float(after["price"].iloc[-1]) - mean
                            net_toward = (abs(gap) - abs(final_gap)) / tick_size
                            closer = net_toward > 1e-9
                            name = f"{label} | {day}"
                            fig.add_trace(go.Scatter(
                                x=(post["minute"] - end_minute).tolist(),
                                y=((post["price"] - mean) / tick_size).tolist(), mode="lines+markers", marker={"size": 4}, name=name,
                                customdata=post["time"].dt.strftime("%Y-%m-%d %H:%M CT").tolist(),
                                hovertemplate="%{customdata}<br>Price minus mean: %{y:.2f} ticks<extra>%{fullData.name}</extra>"))
                            session_label = str(day)
                            if len(contracts) * len(structures) > 1:
                                session_label += f" | {label}"
                            rows.append([
                                session_label, f"{mean:.2f}", f"{baseline:.2f}",
                                f"{gap / tick_size:+.2f}", f"{final_gap / tick_size:+.2f}",
                                f"{net_toward:+.2f}", "Yes" if closer else "No",
                            ])
                    except (ValueError, KeyError) as error:
                        notes.append(f"{label}: {error}")
        finally:
            database.close()
        headers = ["Session", "Mean", "Reference point", "Starting gap (ticks)",
                   "Last gap (ticks)", "Net movement toward mean (ticks)", "Ended closer?"]
        style = {"padding": "8px", "borderBottom": "1px solid #334155", "whiteSpace": "nowrap"}
        table = html.Table([
            html.Thead(html.Tr([html.Th(h, style=style) for h in headers])),
            html.Tbody([html.Tr([html.Td(value, style=style) for value in row]) for row in sorted(rows, reverse=True)])
        ], style={"width": "100%", "fontSize": "13px", "textAlign": "left"})
        filter_description = (
            "Prior-hour filter disabled. " if min_pre_move is None else
            f"Only sessions with absolute net price movement > {min_pre_move:g} ticks "
            f"in the 60 minutes before {mean_end} CT are included. "
            "Uses the last available quote at/before each boundary within the same session; "
            "sparse quotes may predate those boundaries. This is net change, not cumulative movement. ")
        summary = (f"{len(rows)} sessions shown. 0.01 = 1 tick. " + filter_description +
                   "Positive net movement means closer to the mean; negative means farther away. "
                   "Reference point is the last price in the mean window. "
                   "Last gap and ended closer use the latest available price after the window, "
                   "including for sessions still in progress. Calculations use unrounded prices.")
        return fig, html.Div([
            html.P(summary),
            html.Details([html.Summary(f"Excluded sessions / unavailable data ({len(notes)})"),
                          html.Ul([html.Li(note) for note in notes])], open=bool(notes)),
        ]), table
    except Exception as error:
        return fig, str(error), ""


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8056, debug=False)

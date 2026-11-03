"""Standalone candlestick dashboard for stored minute structures (port 8060)."""

import argparse
from datetime import date, timedelta
from pathlib import Path

import duckdb
import pandas as pd
import plotly.graph_objects as go
from dash import Dash, Input, Output, State, dcc, html

from market_data.data_client import DataClient
from market_data.queries import MarketData
from market_data.minute_resolver import interval_minutes
from market_data.structure_parser import MONTHS, from_legacy, parse_structure
from analysis.minute_candles import percentile_candles


SYMBOLS = ["CL", "CO", "G", "HO", "NG", "RB", "CL-CO"]
KINDS = {"MS": "Calendar spread", "MF": "Butterfly", "MDF": "Double fly",
         "MDDF": "Triple fly", "custom": "Custom expression"}
DATA_ERRORS = (ValueError, TypeError, OSError, RuntimeError, duckdb.Error)


def build_structure(symbol, anchor, kind, gap, custom):
    if symbol not in SYMBOLS:
        raise ValueError("Choose a symbol.")
    if kind == "custom":
        text = (custom or "").strip().upper()
        if not text:
            raise ValueError("Enter a custom expression.")
        # Full named expressions may combine symbols; bare month expressions use
        # the selected symbol, e.g. Z26-2*F27+G27.
        try:
            return parse_structure(text).text
        except ValueError:
            return from_legacy(symbol, text)
    if kind not in KINDS:
        raise ValueError("Choose a structure type.")
    if gap is None or not float(gap).is_integer() or not 1 <= float(gap) <= 24:
        raise ValueError("Spacing must be a whole number from 1 to 24 months.")
    return parse_structure(f"{symbol} {anchor} {int(gap)}{kind}").text


def empty_figure(message="Choose a structure and click Plot candles."):
    figure = go.Figure()
    figure.update_layout(
        template="plotly_dark", paper_bgcolor="#111c2d", plot_bgcolor="#111c2d",
        font={"family": "Arial, sans-serif", "color": "#dbe5f3"},
        margin={"l": 65, "r": 25, "t": 55, "b": 55}, height=650,
        xaxis={"title": "Time (UTC)", "rangeslider": {"visible": False}},
        yaxis={"title": "Structure price", "fixedrange": False},
        dragmode="pan", showlegend=False,
    )
    if message:
        figure.add_annotation(text=message, x=.5, y=.5, xref="paper", yref="paper",
                              showarrow=False)
    return figure


def plot_candles(symbol, anchor, kind, gap, custom, start_date, end_date, interval,
                 *, client=None):
    """Resolve aligned minute prices before aggregating them into candles."""
    try:
        if interval_minutes(interval) < 5:
            raise ValueError("Choose a candle interval of at least 5 minutes.")
        structure = build_structure(symbol, anchor, kind, gap, custom)
        start, last_day = pd.Timestamp(start_date), pd.Timestamp(end_date)
        if pd.isna(start) or pd.isna(last_day) or start > last_day:
            raise ValueError("Choose a valid start date on or before the end date.")
        # Date picker end date is inclusive; resolver end timestamp is exclusive.
        end = last_day + pd.Timedelta(days=1)
        if client is None:
            with DataClient() as data:
                result = data.minute(structure, start=start, end=end, interval=None,
                                     fetched_only=True, fill_live_gaps=True)
        else:
            result = client.minute(structure, start=start, end=end, interval=None,
                                   fetched_only=True, fill_live_gaps=True)
        # The resolver's structure_minutes query requires every stored recipe leg
        # at the exact same timestamp. Aggregate only that intersection, never
        # independently aggregated legs or forward-filled observations.
        minutes = result["data"].set_index("timestamp")["price"]
        candles, retained_count = percentile_candles(minutes, interval)
        if candles.empty:
            message = (result.get("message") or "No observed prices remain within each candle's P10–P90 bounds.")
            return empty_figure("No candles after P10–P90 filtering."), message, "No candles"
        figure = empty_figure(None)
        figure.add_trace(go.Candlestick(
            x=pd.to_datetime(candles["timestamp"], utc=True).dt.tz_localize(None),
            open=candles["open"], high=candles["high"], low=candles["low"],
            close=candles["close"], name=structure,
            increasing_line_color="#38d9ac", decreasing_line_color="#fb7185",
        ))
        figure.update_layout(title={"text": f"{structure} | {interval} | P10–P90", "font": {"size": 18}},
                             uirevision=f"p10-p90:{structure}:{interval}:{start}:{end}")
        first = pd.Timestamp(candles["timestamp"].iloc[0]).strftime("%d %b %Y %H:%M")
        last = pd.Timestamp(candles["timestamp"].iloc[-1]).strftime("%d %b %Y %H:%M")
        recipe = ", ".join(f"{weight:g} x {name}" for name, weight in result["recipe"].items())
        status = (f"Next fetched price, live fallback | {len(candles):,} candles | {len(minutes):,} minutes with all legs available "
                  f"at the same timestamp | {first} to {last} UTC | Stored legs: {recipe}")
        status += f" | Isolated spikes dropped: {result['diagnostics'].get('dropped_spikes', 0)}"
        status += f" | P10–P90: {retained_count:,} points used, {len(minutes) - retained_count:,} excluded"
        return figure, status, f"Last close: {candles['close'].iloc[-1]:.5g}"
    except DATA_ERRORS as error:
        return empty_figure("Unable to plot this selection."), str(error), "No candles"


def contract_options(symbol):
    """Include historical contracts and a rolling fallback when data is absent."""
    now = pd.Timestamp.now(tz="UTC")
    fallback = [f"{MONTHS[stamp.month - 1]}{stamp.year % 100:02d}"
                for stamp in pd.date_range(now.date().replace(day=1), periods=60, freq="MS")]
    try:
        database = MarketData()
        try:
            codes = database.contract_codes(symbol)
        finally:
            database.close()
    except DATA_ERRORS:
        codes = []
    codes = sorted(set(codes) | set(fallback), key=lambda code: (int(code[1:]), MONTHS.index(code[0])))
    # Start with the next calendar month rather than an already expiring contract.
    anchor = fallback[1]
    options = [{"label": f"{code} ({MONTHS.index(code[0]) + 1:02d}/20{code[1:]})", "value": code}
               for code in codes]
    return options, anchor


def field(label, control):
    return html.Div([html.Label(label), control], className="candle-field")


def layout():
    today = date.today()
    return html.Main([
        html.Header([html.Div([html.Div("MINUTE DATA", className="eyebrow"),
                              html.H1("Structure candles"),
                              html.P("Explore your stored minute prices, one structure at a time.")]),
                     html.Span("Next fetched price · live fallback", className="badge")]),
        html.Section([
            html.Div([
                field("Symbol", dcc.Dropdown(id="candle-symbol", options=SYMBOLS, value="CL", clearable=False)),
                field("Structure", dcc.Dropdown(id="candle-kind", options=[{"label": label, "value": kind}
                      for kind, label in KINDS.items()], value="MS", clearable=False)),
                field("Anchor contract", dcc.Dropdown(id="candle-anchor", clearable=False)),
                field("Spacing (months)", dcc.Input(id="candle-gap", type="number", value=1, min=1, max=24, step=1)),
                field("Candle interval", dcc.Dropdown(id="candle-interval", options=[
                    {"label": label, "value": value} for label, value in [
                        ("5 minutes", "5min"), ("15 minutes", "15min"),
                        ("30 minutes", "30min"), ("1 hour", "1h"), ("4 hours", "4h"), ("1 day", "1d")]],
                    value="15min", clearable=False)),
            ], className="candle-grid"),
            html.Div([
                field("Date range (UTC, inclusive)", dcc.DatePickerRange(id="candle-dates",
                      start_date=(today - timedelta(days=14)).isoformat(), end_date=today.isoformat(),
                      display_format="DD MMM YYYY", minimum_nights=0)),
                html.Button("Plot candles", id="candle-plot", n_clicks=0),
            ], className="candle-actions"),
            html.Div(field("Custom expression", dcc.Input(id="candle-custom", type="text",
                placeholder="Z26-2*F27+G27 or CL Z26 1MF - CO Z26 1MF")),
                id="candle-custom-field", style={"display": "none"}),
            html.Div(id="candle-selection", className="selection"),
        ], className="controls"),
        html.Section([
            html.Div([html.H2("Candlestick chart"), html.Span(id="candle-last")], className="chart-heading"),
            dcc.Loading(dcc.Graph(id="candle-chart", figure=empty_figure(),
                config={"displaylogo": False, "scrollZoom": True, "responsive": True}), color="#38d9ac"),
            html.Div(id="candle-status", role="status", className="status"),
        ], className="chart-panel"),
        html.P("Live-recorded points use the next fetched price for that leg. "
               "If there is no next fetched price, the stored live value is kept. "
               "Detected structure-price spike minutes are dropped before candles are built. "
               "Only minutes with a valid price for every required stored leg at the exact same timestamp are used. "
               "Each candle uses only observed minute prices between its own 10th and 90th percentiles, inclusive. "
               "Open and close are the first and last retained observations; high and low use the retained range. "
               "Candles with no retained observations are omitted. Use Plot candles to reload published data.",
               className="footnote"),
    ], className="candle-app")


app = Dash(__name__, title="Structure candles", assets_folder=str(Path(__file__).with_name("candle_dashboard_assets")))
app.layout = layout
app.callback(Output("candle-anchor", "options"), Output("candle-anchor", "value"),
             Input("candle-symbol", "value"))(contract_options)


@app.callback(Output("candle-selection", "children"), Output("candle-custom-field", "style"),
              Output("candle-anchor", "disabled"), Output("candle-gap", "disabled"),
              Input("candle-symbol", "value"), Input("candle-anchor", "value"),
              Input("candle-kind", "value"), Input("candle-gap", "value"), Input("candle-custom", "value"))
def selection_preview(symbol, anchor, kind, gap, custom):
    is_custom = kind == "custom"
    try:
        text = build_structure(symbol, anchor, kind, gap, custom)
    except (ValueError, TypeError) as error:
        text = str(error)
    return text, {"display": "block" if is_custom else "none"}, is_custom, is_custom


@app.callback(Output("candle-chart", "figure"), Output("candle-status", "children"),
              Output("candle-last", "children"), Input("candle-plot", "n_clicks"),
              State("candle-symbol", "value"), State("candle-anchor", "value"),
              State("candle-kind", "value"), State("candle-gap", "value"), State("candle-custom", "value"),
              State("candle-dates", "start_date"), State("candle-dates", "end_date"),
              State("candle-interval", "value"), prevent_initial_call=True)
def update_chart(_clicks, *selection):
    return plot_candles(*selection)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8060)
    args = parser.parse_args()
    app.run(host="127.0.0.1", port=args.port, debug=False)

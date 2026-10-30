"""Local OHLCV/database chart comparison: python -m diagnostics.api_dashboard"""

import argparse
import re
from pathlib import Path

import duckdb
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from dash import Dash, Input, Output, State, dcc, html

from API import QHClient, QHAPIError
from API.endpoints import ohlc
from API.explorer import explorer_layout, register_explorer
from API.vap_panel import vap_layout, register_vap
from market_data.queries import MarketData
from market_data.data_client import DataClient
from market_data.workflow_client import full_structure

INTERVALS = {"1m": ("1M", "1min"), "5m": ("5M", "5min"),
             "1h": ("1H", "1h"), "1d": ("1D", "1D")}
CODE = re.compile(r"([A-Z]+?)([FGHJKMNQUVXZ]\d{2}(?:-[FGHJKMNQUVXZ]\d{2}){1,2})")
SAMPLES = ["CLZ26-F27-G27", "CLZ26-F27", "CLF27-G27"]


def db_contract(code, override):
    match = CODE.fullmatch(code)
    if not match:
        raise ValueError("Use a QH calendar spread or butterfly, e.g. CLZ26-F27 or CLZ26-F27-G27")
    product, legs = match.groups()
    contracts = legs.split("-")
    # QH three-month shorthand is treated as a 1:-2:1 butterfly; UI permits override.
    expression = (f"{contracts[0]}-2*{contracts[1]}+{contracts[2]}"
                  if len(contracts) == 3 else legs)
    return product, (override or expression).strip().upper()


def chart(title, prices=None, volume=None, *, minute=False, split_volume=False, message=None):
    figure = make_subplots(rows=2, cols=1, shared_xaxes=True,
                           row_heights=[0.78, 0.22], vertical_spacing=0.04)
    # Categorical UTC labels preserve timestamp identity without closed-market space.
    def labels(frame):
        return pd.to_datetime(frame.timestamp, utc=True).dt.strftime("%Y-%m-%d %H:%M:%S").tolist()

    categories = set()
    if prices is not None and not prices.empty:
        prices = prices.sort_values("timestamp").drop_duplicates("timestamp")
        price_x = labels(prices)
        categories.update(price_x)
        figure.add_trace(go.Candlestick(x=price_x, open=prices.open,
                          high=prices.high, low=prices.low, close=prices.close,
                          name="DB candles" if minute else "Price", showlegend=False), row=1, col=1)
    if volume is not None and not volume.empty:
        volume_x = labels(volume)
        categories.update(volume_x)
        if split_volume:
            for field, name, color in (("buy_volume", "Buy volume", "#16a34a"),
                                       ("sell_volume", "Sell volume", "#dc2626")):
                if field in volume:
                    figure.add_trace(go.Bar(x=volume_x, y=volume[field], name=name,
                                           marker_color=color), row=2, col=1)
            missing = [field for field in ("buy_volume", "sell_volume") if field not in volume]
            if missing:
                figure.add_annotation(text="API did not return: " + ", ".join(missing),
                                      x=0.5, y=0.08, xref="paper", yref="paper", showarrow=False)
        elif "volume" in volume:
            figure.add_trace(go.Bar(x=volume_x, y=volume.volume,
                              name="Volume", marker_color="#64748b", showlegend=False), row=2, col=1)
    if message:
        figure.add_annotation(text=message, x=0.5, y=0.7, xref="paper", yref="paper", showarrow=False)
    figure.update_layout(title=title, template="plotly_white", height=460,
                         margin=dict(l=55, r=20, t=55, b=35), showlegend=split_volume,
                         barmode="group", legend=dict(orientation="h", y=1.1, x=0.65))
    categories = sorted(categories)
    ticks = categories[::max(1, (len(categories) + 7) // 8)]
    figure.update_xaxes(type="category", categoryorder="array", categoryarray=categories,
                        rangeslider_visible=False, tickmode="array", tickvals=ticks,
                        ticktext=[stamp[:16].replace(" ", "<br>") for stamp in ticks])
    figure.update_xaxes(title_text="UTC (non-trading gaps removed)", row=2, col=1)
    figure.update_yaxes(title_text="Price", row=1, col=1)
    figure.update_yaxes(title_text="Volume", row=2, col=1)
    return figure


def create_app():
    options, database_note = set(SAMPLES), ""
    try:
        db = MarketData()
        try:
            options.update(code for code in db.spreads() if CODE.fullmatch(code))
        finally:
            db.close()
    except (duckdb.Error, OSError, RuntimeError) as error:
        database_note = f"Database selector unavailable ({type(error).__name__}); API examples remain selectable."
    app = Dash(__name__,assets_folder=str(Path(__file__).resolve().parents[1]/'assets'))
    app.title = "API chart comparison"
    app.layout = html.Div([
        html.H3("Contract chart comparison"),
        html.Div("OHLCV candles and candles from database minutes, with separate buy/sell volume. All times UTC."),
        html.Div(database_note),
        html.Div([
            dcc.Dropdown(sorted(options), SAMPLES[0], id="contract", clearable=False, style={"minWidth": "270px"}),
            dcc.Input(id="custom", placeholder="Or enter another QH code", debounce=True),
            dcc.Dropdown(list(INTERVALS), "5m", id="interval", clearable=False, style={"width": "100px"}),
            dcc.Input(id="count", type="number", value=100, min=1, max=1000, step=1, style={"width": "80px"}),
            dcc.Input(id="end", placeholder="End UTC, blank = now", debounce=True),
            dcc.Input(id="expression", placeholder="DB expression override (optional)", debounce=True, style={"width": "255px"}),
            html.Button("Plot / refresh", id="plot", n_clicks=0),
        ], style={"display": "flex", "flexWrap": "wrap", "gap": "10px", "margin": "18px 0"}),
        dcc.Loading([html.Div(id="status", role="status"),
                     *[dcc.Graph(id=name, figure=chart(title)) for name, title in
                       [("ohlcv", "OHLCV"), ("database", "Database candles")]]]),
        vap_layout(),
        explorer_layout(),
    ], style={"maxWidth": "1400px", "margin": "auto", "padding": "20px", "fontFamily": "Arial"})

    @app.callback(Output("ohlcv", "figure"),
                  Output("database", "figure"), Output("status", "children"), Input("plot", "n_clicks"),
                  *[State(name, "value") for name in ("contract", "custom", "interval", "count", "end", "expression")],
                  prevent_initial_call=True)
    def plot(_, selected, custom, interval, count, end_text, override):
        titles = ["OHLCV", "Database candles + API volume"]
        try:
            code = (custom or selected or "").strip().upper()
            if count is None or int(count) != count or not 1 <= count <= 1000:
                raise ValueError("Candle count must be an integer from 1 to 1000")
            api_interval, frequency = INTERVALS[interval]
            end = pd.Timestamp(end_text) if end_text else pd.Timestamp.now(tz="UTC")
            end = end.tz_localize("UTC") if end.tzinfo is None else end.tz_convert("UTC")
            boundary = end.floor(frequency)
            start = boundary - int(count) * pd.Timedelta(frequency)
            last = boundary - pd.Timedelta(seconds=1)
            client = QHClient()  # Disk cache is shared; reads the latest saved token.
        except (ValueError, KeyError, TypeError) as error:
            return *[chart(title, message="Check inputs or login") for title in titles], str(error)
        frames, notes = [pd.DataFrame(), pd.DataFrame()], []
        messages = ["No price data in this window"] * 2
        with client:
            try:
                result = ohlc.fetch(client, code, interval=api_interval, count=int(count),
                                    extra_fields=["buyvolume", "sellvolume"],
                                    end=int(last.timestamp()) if end_text else None)
                frame = result.to_frame()
                if not frame.empty:
                    required = {"timestamp", "open", "high", "low", "close"}
                    if not required.issubset(frame.columns):
                        raise ValueError("API response is missing candle fields; inspect the endpoint with API.probe")
                    frames[0] = frame[frame.timestamp.between(start, last)].sort_values("timestamp")
                if frames[0].empty:
                    messages[0] = (f"API returned no candles for {code}" if frame.empty else
                                   "API candles fall outside the selected UTC window")
                    notes.append(f"OHLCV: {messages[0]}")
                    if not frame.empty:
                        notes.append(f"OHLCV returned range: {frame.timestamp.min()} to {frame.timestamp.max()}")
                notes.append(f"OHLCV: {len(frames[0])} candles ({'cached' if client.last_response.get('cached') else 'fetched'})")
            except (QHAPIError, ValueError, KeyError, OSError) as error:
                messages[0] = str(error)
                notes.append(f"OHLCV: {error}")
        try:
            product, expression = db_contract(code, override)
            data = DataClient()
            try:
                result=data.minute(full_structure(product,expression),start=start,end=boundary,interval=None)
                frames[1] = result['data']
                if frames[1].empty:
                    messages[1]=result['message']
            finally:
                data.close()
            frames[1]["timestamp"] = pd.to_datetime(frames[1].timestamp, utc=True)
            minute_count = len(frames[1])
            frames[1] = (frames[1].sort_values("timestamp").set_index("timestamp")["price"]
                         .resample(frequency, origin="start_day", closed="left", label="left")
                         .ohlc().dropna().reset_index())
            notes.append(f"DB: {minute_count} minutes -> {len(frames[1])} candles; {product} {expression}")
        except (duckdb.Error, ValueError, OSError, RuntimeError) as error:
            messages[1] = str(error)
            notes.append(f"DB: {error}")
        figures = [chart(title, frame, frames[0], minute=index == 1,
                         split_volume=True,
                         message=messages[index] if frame.empty else None)
                   for index, (title, frame) in enumerate(zip(titles, frames))]
        notes.append(f"{code} | {start} to {last} | cache 60s; DB OHLC uses minute samples")
        return *figures, html.Ul([html.Li(note) for note in notes])

    register_explorer(app)
    register_vap(app)
    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8058)
    create_app().run(host="127.0.0.1", port=parser.parse_args().port, debug=False)

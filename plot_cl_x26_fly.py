import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from analysis.seasonality import Seasonality
from market_data import MarketData


PRODUCT = "CL"
EXPRESSION = "X26-2*Z26+F27"
TITLE = f"{PRODUCT} {EXPRESSION}"


def load_seac(database):
    result = Seasonality(database).working_day_expression_seasonality(
        PRODUCT, EXPRESSION, start_year=2026, end_year=2026, window_days=400
    )
    current = result.get("series", {}).get(2026)
    if current is None or current.empty:
        raise ValueError("No SEAC or live-overlay history for the selected fly")
    return current.set_index("date")["value"].sort_index().rename("SEAC + live")


def load_intraday(database):
    minute = database.synthetic(PRODUCT, EXPRESSION)
    if minute.empty:
        raise ValueError("No minute data for the required CL spreads")
    return (minute.set_index("timestamp")["price"].sort_index()
            .resample("30min").ohlc().dropna())


def make_figure(seac, intraday):
    figure = make_subplots(
        rows=2, cols=1, shared_xaxes=False, vertical_spacing=0.10,
        subplot_titles=("SEAC daily settlement value", "30-minute candlesticks"),
        row_heights=[0.38, 0.62],
    )
    figure.add_trace(go.Scatter(
        x=seac.index, y=seac.values, mode="lines",
        name="SEAC daily", line={"color": "#2563eb", "width": 2},
    ), row=1, col=1)
    figure.add_trace(go.Candlestick(
        x=intraday.index, open=intraday["open"], high=intraday["high"],
        low=intraday["low"], close=intraday["close"],
        name="30-minute OHLC",
        increasing_line_color="#16a34a", decreasing_line_color="#dc2626",
    ), row=2, col=1)
    figure.update_layout(
        title=TITLE, template="plotly_white", height=900,
        hovermode="x unified", xaxis2_rangeslider_visible=False,
        legend={"orientation": "h", "y": 1.04},
    )
    figure.update_yaxes(title_text="Fly value", row=1, col=1)
    figure.update_yaxes(title_text="Fly value", row=2, col=1)
    figure.update_xaxes(title_text="Trading date", row=1, col=1)
    figure.update_xaxes(title_text="Timestamp", row=2, col=1)
    return figure


def main():
    database = MarketData()
    try:
        seac = load_seac(database)
        intraday = load_intraday(database)
    finally:
        database.close()
    make_figure(seac, intraday).show()


if __name__ == "__main__":
    main()

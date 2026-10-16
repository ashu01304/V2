"""On-demand synthetic intraday candles and their Plotly figure."""

from collections import defaultdict
from math import ceil

import pandas as pd
import plotly.graph_objects as go

from market_data import MarketData
from market_data.contracts import spread_recipe

TIMEFRAMES = (15, 30, 60)
MAX_CANDLES = 1_500


def price_recipe(product, expression):
    weights = defaultdict(float)
    products = [("CL", 1), ("LCO", -1)] if product == "CL-CO" else [
        ("LCO" if product == "CO" else product, 1)
    ]
    for stored_product, sign in products:
        for weight, instrument in spread_recipe(stored_product, expression):
            weights[instrument] += sign * weight
    recipe = {instrument: weight for instrument, weight in weights.items()
              if abs(weight) > 1e-10}
    if not recipe:
        raise ValueError("The expression has no non-zero spread legs.")
    return recipe


def load_candles(product, expression, timeframe=15, max_candles=MAX_CANDLES,
                 database=None):
    """Align minute legs before OHLC aggregation; return newest weekday bars."""
    if timeframe not in TIMEFRAMES:
        raise ValueError("Choose a 15-, 30- or 60-minute timeframe.")
    if not 1 <= max_candles <= MAX_CANDLES:
        raise ValueError(f"Request between 1 and {MAX_CANDLES:,} candles.")
    recipe = price_recipe(product, expression)
    owned = database is None
    db = MarketData() if owned else database
    try:
        connection = db.connection
        placeholders = ", ".join("?" for _ in recipe)
        bounds = connection.execute(f"""
            SELECT instrument, MIN(timestamp), MAX(timestamp)
            FROM spread_prices WHERE instrument IN ({placeholders})
            GROUP BY instrument
        """, list(recipe)).fetchall()
        missing = set(recipe) - {row[0] for row in bounds}
        if missing:
            raise ValueError("No stored minute data for " + ", ".join(sorted(missing)))
        earliest = max(pd.Timestamp(row[1]) for row in bounds)
        latest = min(pd.Timestamp(row[2]) for row in bounds)
        if earliest > latest:
            raise ValueError("The required spreads have no overlapping history.")
        values = ", ".join("(?, ?)" for _ in recipe)
        weights = [value for item in recipe.items() for value in item]
        # Start with a recent window, extending it only when bars are sparse.
        days = max(7, ceil(max_candles * timeframe / 1440 * 7 / 5 * 1.5))
        sql = f"""
            WITH recipe(instrument, weight) AS (VALUES {values}),
            minutes AS (
                SELECT p.timestamp, SUM(p.price * r.weight) AS price
                FROM spread_prices p JOIN recipe r USING (instrument)
                WHERE p.timestamp >= ? AND p.timestamp <= ?
                  AND isfinite(p.price)
                GROUP BY p.timestamp
                HAVING COUNT(DISTINCT p.instrument) = ?
            ),
            candles AS (
                SELECT time_bucket(INTERVAL '{timeframe} minutes',
                                   timezone('UTC', timestamp)) AS candle_time,
                       arg_min(price, timestamp) AS open,
                       MAX(price) AS high, MIN(price) AS low,
                       arg_max(price, timestamp) AS close
                FROM minutes
                WHERE isodow(timezone('UTC', timestamp)) BETWEEN 1 AND 5
                GROUP BY candle_time
            )
            SELECT * FROM candles ORDER BY candle_time DESC LIMIT ?
        """
        while True:
            start = max(earliest, latest - pd.Timedelta(days=days))
            candles = connection.execute(
                sql, [*weights, start.to_pydatetime(), latest.to_pydatetime(),
                      len(recipe), max_candles],
            ).fetchdf()
            if len(candles) >= max_candles or start == earliest:
                break
            days *= 2
        if candles.empty:
            raise ValueError("No common weekday minute prices for this expression.")
        candles["candle_time"] = pd.to_datetime(candles["candle_time"], utc=True)
        return candles.set_index("candle_time").sort_index()
    finally:
        if owned:
            db.close()


def make_intraday_figure(candles, product, expression, timeframe=15,
                         live_quote=None, title=None):
    decimals = 2 if product in {"CL", "CO", "CL-CO"} else 3
    figure = go.Figure(go.Candlestick(
        uid="intraday-candles",
        x=candles.index, open=candles["open"], high=candles["high"],
        low=candles["low"], close=candles["close"], name="Minute-derived prices",
        increasing_line_color="#22c55e", decreasing_line_color="#ef4444",
        hovertemplate=(
            f"%{{x|%Y-%m-%d %H:%M}} UTC<br>Open: %{{open:.{decimals}f}}"
            f"<br>High: %{{high:.{decimals}f}}<br>Low: %{{low:.{decimals}f}}"
            f"<br>Close: %{{close:.{decimals}f}}<extra></extra>"
        ),
    ))
    live_x, live_y = [], []
    if live_quote:
        timestamp = pd.to_datetime(live_quote["timestamp"], utc=True)
        if timestamp.dayofweek < 5:
            live_x, live_y = [timestamp], [float(live_quote["value"])]
    figure.add_trace(go.Scatter(
        x=live_x, y=live_y, mode="markers", name="Live price", uid="intraday-live",
        marker={"size": 12, "color": "#22c55e",
                "line": {"color": "#dcfce7", "width": 2}},
        hovertemplate=f"%{{x|%Y-%m-%d %H:%M:%S}} UTC<br>Live: "
                      f"%{{y:.{decimals}f}}<extra></extra>",
    ))
    figure.update_layout(
        title=title or f"{product} {expression} | {timeframe}-minute prices",
        template="plotly_dark", height=650, hovermode="x unified", dragmode="zoom",
        paper_bgcolor="#111827", plot_bgcolor="#111827",
        font={"color": "#f8fafc"}, margin={"l": 65, "r": 35, "t": 60, "b": 55},
        uirevision=f"{product}:{expression}:{timeframe}",
        xaxis={"title": "Time (UTC)", "rangeslider": {"visible": False},
               "rangebreaks": [{"bounds": ["sat", "mon"]}]},
        yaxis={"tickformat": f".{decimals}f", "title": "Price"},
        legend={"orientation": "h", "y": 1.03, "x": 1, "xanchor": "right"},
    )
    return figure

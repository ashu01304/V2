from analysis.plotting import OHLCPlotter
from database.api_manager import APIDatabaseManager
from Mini_Tools.api_data_sync import sync
import pandas as pd
from dash import Dash, dcc, html


# API parameters
INSTRUMENTS = ["COZ27"]
INTERVAL = "1H"              # 1M, 5M, 1H, or 1D
COUNT = 401                  # Use None when supplying START and END
START = None                 # Optional Unix timestamp in seconds
END = None                   # Optional Unix timestamp in seconds
# Plot parameters
FETCH_FROM_API = True        # False loads previously stored candles only
CHART_TYPE = "candlestick"  # candlestick or line
X_AXIS = "candle_number"    # candle_number or datetime
CHART_TITLE = "QH API OHLC"
CHART_HEIGHT = 1200
CORRELATION_WINDOWS = [7, 14, 21, 42, 50, 100]
BOLLINGER_WINDOW = 20
BOLLINGER_STD =1.8
BOLLINGER_METHOD = "ewm"
HOST = "127.0.0.1"
PORT = 8051


def support_resistance(frame):
    df = frame.tail(100).copy()
    previous = df["Close"].shift()
    true_range = pd.concat([
        df["High"] - df["Low"],
        (df["High"] - previous).abs(),
        (df["Low"] - previous).abs(),
    ], axis=1).max(axis=1)
    atr = true_range.rolling(14).mean().iloc[-1]
    if pd.isna(atr) or atr == 0:
        return {}

    swings = []
    for i in range(3, len(df) - 3):
        high, low = df["High"].iloc[i], df["Low"].iloc[i]
        if high > df["High"].iloc[i - 3:i].max() and high > df["High"].iloc[i + 1:i + 4].max():
            swings.append((i, high, "high"))
        if low < df["Low"].iloc[i - 3:i].min() and low < df["Low"].iloc[i + 1:i + 4].min():
            swings.append((i, low, "low"))

    zones = []
    for point in swings:
        zone = next((z for z in zones if abs(z["price"] - point[1]) <= .3 * atr), None)
        if zone:
            zone["points"].append(point)
            zone["price"] = sum(p[1] for p in zone["points"]) / len(zone["points"])
        else:
            zones.append({"price": point[1], "points": [point]})

    scored = []
    for zone in zones:
        touches = []
        for point in sorted(zone["points"]):
            if not touches or point[0] - touches[-1][0] >= 3:
                touches.append(point)
        if len(touches) < 2:
            continue
        rejection = sum(
            ((point[1] - df["Low"].iloc[point[0] + 1:point[0] + 4].min()) if point[2] == "high"
             else (df["High"].iloc[point[0] + 1:point[0] + 4].max() - point[1])) / atr
            for point in touches
        ) / len(touches)
        recency = 1 - (len(df) - 1 - touches[-1][0]) / len(df)
        scored.append((zone["price"], len(touches) * 2 + rejection + recency))

    price = df["Close"].iloc[-1]
    choose = lambda items: max(items, key=lambda z: z[1] - .1 * abs(z[0] - price) / atr)[0] if items else None
    levels = {"Support": choose([z for z in scored if z[0] < price]),
              "Resistance": choose([z for z in scored if z[0] > price])}
    return {name: value for name, value in levels.items() if value is not None}


def main():
    if FETCH_FROM_API:
        sync(INSTRUMENTS, INTERVAL, COUNT, START, END)

    database = APIDatabaseManager()
    try:
        data = database.load_ohlc(INSTRUMENTS, INTERVAL)
    finally:
        database.close()

    closes = pd.concat(
        {name: frame["Close"] for name, frame in data.items()}, axis=1
    ).dropna()
    if len(closes.columns) == 2:
        correlations = {
            bars: closes.tail(bars).corr().iloc[0, 1]
            for bars in CORRELATION_WINDOWS if len(closes) >= bars
        }
        print("\nClose-price correlation (common bars):")
        print(pd.Series(correlations, name="Correlation").to_string())
    else:
        print("Correlation unavailable: both contracts need stored data.")

    levels = {name: support_resistance(frame) for name, frame in data.items()}
    print("Support/resistance:", levels)

    figure = OHLCPlotter().plot(
        data=data,
        chart_type=CHART_TYPE,
        title=CHART_TITLE,
        height=CHART_HEIGHT,
        x_axis=X_AXIS,
        show=False,
        bollinger_window=BOLLINGER_WINDOW,
        bollinger_std=BOLLINGER_STD,
        bollinger_method=BOLLINGER_METHOD,
        levels=levels,
    )
    app = Dash(__name__)
    app.layout = html.Div(dcc.Graph(figure=figure))
    print(f"OHLC chart: http://{HOST}:{PORT}")
    app.run(host=HOST, port=PORT, debug=False)


if __name__ == "__main__":
    main()

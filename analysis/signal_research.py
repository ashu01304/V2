import numpy as np
import pandas as pd

from backtesting.trade_rules import get_trade_rules
from backtesting.strategies import SeasonalStrategies

from analysis.seasonality import Seasonality
from analysis.expression import parse_expression
from market_data import MarketData


SIGNAL_DEFAULTS = {
    "trend": (20, 0.0),
    "mean_reversion": (20, 1.5),
    "breakout": (20, 0.0),
    "volatility": (20, 1.5),
    "seasonality": (10, 60.0),
}


def scan_trade_ideas(symbol, expression_items, years=10,
                     signal_model="seasonal_sd"):
    db, ideas = MarketData(), []
    try:
        seasonality = Seasonality(db)
        for item in expression_items:
            expression = item["expression"]
            legs = parse_expression(expression)
            if not legs:
                continue
            anchor_year = 2000 + int(legs[0][1][1:])
            result = seasonality.working_day_expression_seasonality(
                symbol, expression, anchor_year - years + 1, anchor_year, window_days=400
            )
            available = sorted(result.get("series", {}))
            if anchor_year not in available:
                continue
            current_year = anchor_year
            current = result["series"][current_year].sort_values("date")
            values = current.set_index("date")["value"].dropna()
            if len(values) < (25 if signal_model == "momentum" else 21):
                continue

            day = current.index.max()
            mean, std = values.tail(20).mean(), values.tail(20).std()
            zscore = ((values.iloc[-1] - mean) / std
                      if pd.notna(std) and std else np.nan)
            rules = get_trade_rules(symbol, item["strategy"])
            if signal_model == "mean_reversion":
                signal, _ = SeasonalStrategies.current_year_mean_reversion(
                    pd.Series({"DAYS_TO_EXPIRY": 400}), values,
                    stop_before_expiry=0,
                    minimum_expected_move=rules["minimum_expected_move"],
                )
                if signal == "NONE":
                    continue
                side = 1 if signal == "LONG" else -1
                median_move = mean - values.iloc[-1]
                score, direction, window = round(-zscore * 50), np.nan, 10
            elif signal_model == "momentum":
                fast, slow = values.tail(5).mean(), mean
                previous_slow = values.iloc[-25:-5].mean()
                changes = values.diff().tail(10)
                median_move = values.iloc[-1] - values.iloc[-11]
                long_entry = (fast > slow and slow > previous_slow and median_move > 0
                              and (changes > 0).sum() >= 6 and 0 <= zscore <= 2.5
                              and values.iloc[-2] <= fast and values.iloc[-1] > values.iloc[-2])
                short_entry = (fast < slow and slow < previous_slow and median_move < 0
                               and (changes < 0).sum() >= 6 and -2.5 <= zscore <= 0
                               and values.iloc[-2] >= fast and values.iloc[-1] < values.iloc[-2])
                if not long_entry and not short_entry:
                    continue
                side = 1 if long_entry else -1
                score = ((changes > 0).sum() * 10 if side > 0
                         else -(changes < 0).sum() * 10)
                direction, window = np.nan, 10
            else:
                window, direction, median_move = _best_seasonal_window(
                    result["series"], available[:-1], day
                )
                if (pd.isna(direction) or pd.isna(median_move) or abs(direction) < 70
                        or np.sign(direction) != np.sign(median_move)):
                    continue
                score = direction
                side = 1 if direction > 0 else -1
            current_value = values.iloc[-1]
            entry_price = current_value
            expected_move = abs(median_move)
            if (expected_move <= rules["minimum_expected_move"] or pd.isna(zscore)):
                continue
            if signal_model == "seasonal_sd" and (
                    (side > 0 and zscore > -rules["entry_zscore"])
                    or (side < 0 and zscore < rules["entry_zscore"])):
                continue
            expected_move = min(expected_move, rules["max_target"]) if rules["max_target"] else expected_move
            exit_target = entry_price + side * expected_move
            exit_date = values.index[-1] + pd.offsets.BDay(window or 10)
            price = lambda value: _price(value, symbol)
            entry_plan = f"Enter {'LONG' if side > 0 else 'SHORT'} now at {price(entry_price)}"
            ideas.append({
                "Contract": item["contract"], "Strategy": item["strategy"],
                "Signal Model": ("Momentum" if signal_model == "momentum" else
                                 "Mean Reversion" if signal_model == "mean_reversion"
                                 else "SEAC + 1.6 SD"),
                "Expression": expression, "Side": "LONG" if side > 0 else "SHORT",
                "Score": round(score), "Window": window,
                "Value": price(current_value),
                "Entry Plan": entry_plan,
                "Entry Price": price(entry_price), "Exit Target": price(exit_target),
                "Exit By": exit_date.strftime("%Y-%m-%d"),
                "Exit Plan": f"Take profit at {price(exit_target)} or exit by {exit_date:%Y-%m-%d}",
                "Seasonal Bias": None if pd.isna(direction) else round(float(direction)),
                "Z-score": round(float(zscore), 3),
                "Median Move": None if pd.isna(median_move) else price(median_move),
            })
    finally:
        db.close()
    if not ideas:
        return pd.DataFrame()
    output = pd.DataFrame(ideas)
    output["_strength"] = output["Score"].abs()
    return output.sort_values(
        ["_strength", "Contract", "Strategy"], ascending=[False, True, True]
    ).drop(columns="_strength")


def _best_seasonal_window(series, years, day):
    best = None
    for window in (10,):
        changes = []
        for year in years:
            historical = series[year]["value"]
            if day in historical.index and day + window in historical.index:
                changes.append(historical.loc[day + window] - historical.loc[day])
        if len(changes) < 3:
            continue
        changes = np.asarray(changes, dtype=float)
        direction = np.sign(changes).sum() / len(changes) * 100
        median = np.median(changes)
        if np.sign(direction) != np.sign(median):
            direction *= 0.5
        candidate = (abs(direction), abs(median), window, direction, median)
        if best is None or candidate[:2] > best[:2]:
            best = candidate
    return (None, np.nan, np.nan) if best is None else (best[2], best[3], best[4])


def _rollover_bias(result, window):
    moves = []
    for item in result.get("series", {}).values():
        if item.get("active"):
            continue
        after = item["data"][item["data"].index >= 0]
        after = after[after.index <= window]
        if not after.empty:
            moves.append(after["value"].iloc[-1])
    return (np.nan if len(moves) < 3
            else np.sign(moves).sum() / len(moves) * 100)


def run_signal_research(symbol, expression, signal_name, start_year, end_year,
                        holding_days=10, lookback=20, threshold=1.5, cost=0.0):
    db = MarketData()
    try:
        result = Seasonality(db).working_day_expression_seasonality(
            symbol, expression, int(start_year), int(end_year), window_days=400
        )
    finally:
        db.close()

    trades = []
    years = sorted(result.get("series", {}))
    for year in years:
        frame = result["series"][year].sort_values("date")
        values = frame.set_index("date")["value"]
        if signal_name == "seasonality":
            signals = _seasonal_signals(result["series"], year, holding_days, threshold)
            signals = pd.Series(signals, index=frame["date"]).groupby(level=0).last()
            signals = signals.reindex(values.index).fillna(0)
        else:
            signals = _technical_signals(values, signal_name, lookback, threshold)
        trades.extend(_make_trades(
            values, signals, year, holding_days, float(cost), signal_name
        ))

    trades = pd.DataFrame(trades)
    if trades.empty:
        return {"summary": _empty_summary(), "trades": trades, "equity": pd.DataFrame()}
    trades = trades.sort_values("Exit_Date").reset_index(drop=True)
    trades["Cumulative_Move"] = trades["Net_Move"].cumsum()
    equity = trades[["Exit_Date", "Cumulative_Move"]]
    winners = trades["Net_Move"] > 0
    gross_profit = trades.loc[winners, "Net_Move"].sum()
    gross_loss = -trades.loc[~winners, "Net_Move"].sum()
    drawdown = trades["Cumulative_Move"] - trades["Cumulative_Move"].cummax()
    summary = {
        "Trades": len(trades),
        "Win Rate": f"{winners.mean() * 100:.1f}%",
        "Total Move": _number(trades["Net_Move"].sum()),
        "Average Move": _number(trades["Net_Move"].mean()),
        "Profit Factor": "∞" if gross_loss == 0 else f"{gross_profit / gross_loss:.2f}",
        "Max Drawdown": _number(drawdown.min()),
    }
    return {"summary": summary, "trades": trades, "equity": equity}


def _technical_signals(values, name, lookback, threshold):
    lookback = max(3, int(lookback))
    if name == "trend":
        fast = values.rolling(max(2, lookback // 4)).mean()
        slow = values.rolling(lookback).mean()
        signal = np.sign(fast - slow)
    elif name == "mean_reversion":
        mean, std = values.rolling(lookback).mean(), values.rolling(lookback).std()
        zscore = (values - mean) / std.replace(0, np.nan)
        signal = pd.Series(np.where(zscore <= -threshold, 1,
                          np.where(zscore >= threshold, -1, 0)), index=values.index)
    elif name == "breakout":
        high = values.shift(1).rolling(lookback).max()
        low = values.shift(1).rolling(lookback).min()
        signal = pd.Series(np.where(values > high, 1,
                          np.where(values < low, -1, 0)), index=values.index)
    elif name == "volatility":
        changes = values.diff()
        short_vol = changes.rolling(max(3, lookback // 4)).std()
        long_vol = changes.rolling(lookback).std()
        expanding = short_vol > float(threshold) * long_vol
        signal = np.sign(changes.rolling(max(2, lookback // 4)).sum()).where(expanding, 0)
    else:
        raise ValueError(f"Unknown signal: {name}")
    return pd.Series(signal, index=values.index).fillna(0)


def _seasonal_signals(series, test_year, holding_days, threshold):
    current = series[test_year]
    prior = [year for year in sorted(series) if year < test_year]
    output = []
    for day in current.index:
        changes = []
        for year in prior:
            historical = series[year]["value"]
            if day in historical.index and day + holding_days in historical.index:
                changes.append(historical.loc[day + holding_days] - historical.loc[day])
        if len(changes) < 3:
            output.append(0)
            continue
        changes = np.asarray(changes, dtype=float)
        direction = np.sign(changes).sum() / len(changes) * 100
        median = np.median(changes)
        output.append(1 if direction >= threshold and median > 0
                      else -1 if direction <= -threshold and median < 0 else 0)
    return output


def _make_trades(values, signals, year, holding_days, cost, signal_name):
    rows, index = [], values.index
    position = 0
    while position + holding_days + 1 < len(values):
        side = int(np.sign(signals.iloc[position]))
        if not side:
            position += 1
            continue
        entry_position = position + 1
        exit_position = entry_position + holding_days
        entry, exit_value = values.iloc[entry_position], values.iloc[exit_position]
        gross = side * (exit_value - entry)
        rows.append({
            "Signal": signal_name.replace("_", " ").title(), "Year": year,
            "Side": "LONG" if side > 0 else "SHORT",
            "Signal_Date": index[position], "Entry_Date": index[entry_position],
            "Exit_Date": index[exit_position], "Entry": entry, "Exit": exit_value,
            "Gross_Move": gross, "Cost": cost, "Net_Move": gross - cost,
        })
        position = exit_position + 1
    return rows


def _number(value):
    return np.format_float_positional(float(value), precision=8, trim="-")


def _price(value, symbol):
    decimals = 2 if str(symbol).upper() in {"CO", "CL"} else 3
    return f"{float(value):.{decimals}f}"


def _empty_summary():
    return {"Trades": 0, "Win Rate": "-", "Total Move": "-",
            "Average Move": "-", "Profit Factor": "-", "Max Drawdown": "-"}

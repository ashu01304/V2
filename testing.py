"""Interactive range-reversion explorer for configured market products."""

from functools import lru_cache
from threading import Lock

import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from dash import Dash, Input, Output, State, dash_table, dcc, html
from scipy.signal import find_peaks

from analysis.contract_universe import load_universe
from analysis.expression import parse_expression
from analysis.seasonality import Seasonality
from market_data import MarketData
from product_config import PRODUCT_CONFIG


TEST_SYMBOLS = ("CL", "CO")
DEFAULT_SYMBOL = "CL"
WINDOW_DAYS = 400
DEFAULT_MODE = "extrema"
DEFAULT_PEAK_LOOKBACK = 10
DEFAULT_PEAK_RANK = 3
DEFAULT_VALLEY_LOOKBACK = 10
DEFAULT_VALLEY_RANK = 3
DEFAULT_MAX_PARALLEL_POSITIONS = 8
DEFAULT_MAX_CONSECUTIVE_DIRECTION = 4
DEFAULT_NO_TRADE_DAYS = 80
DEFAULT_MAX_TRADE_DAYS = 400
SWING_DIFFERENCE_LOOKBACK = 25
SWING_DIFFERENCE_TOP_COUNT = 6
DEFAULT_HISTORY_YEARS = 10


UNIVERSE = load_universe()
STRATEGIES = UNIVERSE["columns"]
SERIES_CACHE = {}
CACHE_LOCK = Lock()


def available_contract_codes(symbol):
    database = MarketData()
    try:
        rows = database.cursor.execute(
            "SELECT DISTINCT contract_code FROM seac_settlements WHERE symbol = ?",
            [symbol],
        ).fetchall()
        return {code for code, in rows}
    finally:
        database.close()


def expression_is_available(expression, available):
    return all(contract in available
               for _, contract in parse_expression(expression))


def expression_rows(symbol):
    available, rows = available_contract_codes(symbol), []
    for contract, expressions in UNIVERSE["contracts"].items():
        row = {"Contract": contract}
        for strategy, expression in expressions.items():
            if expression_is_available(expression, available):
                row[strategy] = " "
        if len(row) > 1:
            rows.append(row)
    return rows


def visible_expression_rows(symbol, selected_strategies):
    selected = set(selected_strategies or [])
    return [row for row in EXPRESSION_ROWS.get(symbol, [])
            if any(strategy in row for strategy in selected)]


EXPRESSION_ROWS = {symbol: expression_rows(symbol) for symbol in TEST_SYMBOLS}
DEFAULT_ROWS = EXPRESSION_ROWS[DEFAULT_SYMBOL]

DEFAULT_ROW = 0
DEFAULT_STRATEGY = next(
    strategy for strategy in STRATEGIES if DEFAULT_ROWS[DEFAULT_ROW].get(strategy)
)


def load_expression_series(symbol, expression, current_year, history_years):
    start_year = max(2000, current_year - history_years)
    key = (symbol, expression, start_year, current_year)
    with CACHE_LOCK:
        cached = SERIES_CACHE.get(key)
    if cached is not None:
        return cached
    database = MarketData()
    try:
        result = Seasonality(database).working_day_expression_seasonality(
            symbol, expression, start_year, current_year, WINDOW_DAYS,
        )
    finally:
        database.close()
    if not result["series"]:
        warnings = "; ".join(result.get("warnings", [])) or "no seasonality data"
        raise ValueError(warnings)
    series = {year: frame.sort_index() for year, frame in result["series"].items()}
    with CACHE_LOCK:
        SERIES_CACHE[key] = series
    return series


def ranked_extrema_level(values, lookback, rank, peaks=True):
    if lookback < 1 or rank < 1 or rank > lookback:
        raise ValueError("Rank must be between 1 and its lookback")
    array = values.to_numpy(dtype=float)
    positions = find_peaks(array if peaks else -array)[0]
    confirmed = {position + 1: array[position] for position in positions}
    recent = []
    level = np.full(len(array), np.nan)
    for position in range(len(array)):
        if position in confirmed:
            recent.append(confirmed[position])
            recent = recent[-lookback:]
        if len(recent) == lookback:
            level[position] = sorted(recent, reverse=peaks)[rank - 1]
    return level


def ranked_daily_level(values, lookback, rank, highest=True):
    if lookback < 1 or rank < 1 or rank > lookback:
        raise ValueError("Rank must be between 1 and its lookback")
    index = lookback - rank if highest else rank - 1
    return values.rolling(lookback, min_periods=lookback).apply(
        lambda window: np.partition(window, index)[index], raw=True
    ).to_numpy()


def boundary(values, mode, lookback, rank, upper):
    if mode == "extrema":
        return ranked_extrema_level(values, lookback, rank, peaks=upper)
    if mode == "days":
        return ranked_daily_level(values, lookback, rank, highest=upper)
    raise ValueError(f"Unknown calculation mode: {mode}")


def strongest_recent_swing_ticks(values, tick_size, swing_lookback, swing_top_count):
    """Return the causal transformed average of the strongest recent swings."""
    array = values.to_numpy(dtype=float)
    peak_positions = set(find_peaks(array)[0])
    valley_positions = set(find_peaks(-array)[0])
    confirmed = {}
    for position in peak_positions | valley_positions:
        # A local turning point is knowable only after the following settlement.
        confirmed[position + 1] = (position, array[position])

    indicator = np.full(len(array), np.nan)
    recent_differences = []
    previous_turning_position = None
    previous_turning_value = None
    current_average = np.nan
    for confirmation_position in range(len(array)):
        turning_point = confirmed.get(confirmation_position)
        if turning_point is not None:
            turning_position, turning_value = turning_point
            if (previous_turning_position is not None
                    and turning_position > previous_turning_position):
                recent_differences.append(
                    abs(turning_value - previous_turning_value)
                )
                recent_differences = recent_differences[-swing_lookback:]
                if len(recent_differences) == swing_lookback:
                    strongest = sorted(recent_differences, reverse=True)[
                        :swing_top_count
                    ]
                    average_ticks = np.mean(strongest) / tick_size
                    current_average = float(np.ceil(average_ticks ** 0.9))
            previous_turning_position = turning_position
            previous_turning_value = turning_value
        indicator[confirmation_position] = current_average
    return indicator


def entry_candidates(values, upper, lower):
    """Return causal crossings of boundaries known at the prior settlement."""
    prices = values.to_numpy(dtype=float)
    candidates = {}
    for index in range(1, len(prices)):
        previous_price = prices[index - 1]
        price = prices[index]
        known_upper = upper[index - 1]
        known_lower = lower[index - 1]
        if not np.isfinite(known_upper) or not np.isfinite(known_lower):
            continue
        if previous_price < known_upper <= price:
            candidates[index] = (-1, known_upper)
        elif previous_price > known_lower >= price:
            candidates[index] = (1, known_lower)
    return candidates


def backtest_range(values, upper, lower, swing_ticks, max_parallel_positions,
                   max_consecutive_direction, no_trade_days, max_trade_days,
                   minimum_swing_ticks, tick_size, include_entries=False):
    prices = values.to_numpy(dtype=float)
    candidates = entry_candidates(values, upper, lower)
    outcomes, entries, trades = [], [], []
    positions = []
    last_direction = None
    consecutive_direction = 0
    for index, price in enumerate(prices):
        remaining = []
        for trade in positions:
            direction, target, stop = trade["direction"], trade["target"], trade["stop"]
            target_hit = price >= target if direction == 1 else price <= target
            stop_hit = price <= stop if direction == 1 else price >= stop
            if target_hit:
                outcomes.append("TP")
                trade.update(outcome="TP", exit_index=index, exit_price=target,
                             pnl_ticks=trade["distance_ticks"])
            elif stop_hit:
                outcomes.append("SL")
                trade.update(outcome="SL", exit_index=index, exit_price=stop,
                             pnl_ticks=-trade["distance_ticks"])
            else:
                remaining.append(trade)
        positions = remaining

        candidate = candidates.get(index)
        if candidate is not None:
            day = float(values.index[index])
            if day < -max_trade_days or day >= -no_trade_days:
                candidate = None
        if candidate is not None and len(positions) < max_parallel_positions:
            direction, entry = candidate
            if (direction == last_direction
                    and consecutive_direction >= max_consecutive_direction):
                continue
            distance_ticks = swing_ticks[index - 1]
            if not np.isfinite(distance_ticks) or distance_ticks <= minimum_swing_ticks:
                continue
            distance = distance_ticks * tick_size
            target = entry + direction * distance
            stop = entry - direction * distance
            trade = {
                "direction": direction, "entry_index": index, "entry_price": entry,
                "target": target, "stop": stop, "distance_ticks": distance_ticks,
                "outcome": "OPEN", "exit_index": None, "exit_price": None,
                "pnl_ticks": None,
            }
            positions.append(trade)
            trades.append(trade)
            consecutive_direction = (consecutive_direction + 1
                                     if direction == last_direction else 1)
            last_direction = direction
            label = "LONG" if direction == 1 else "SHORT"
            entries.append((index, label, entry, target, stop))
    unresolved = len(positions)
    return ((outcomes, entries, trades, unresolved) if include_entries
            else (outcomes, trades, unresolved))


def trade_rows(trades, frame, decimals):
    rows = []
    for trade in trades:
        rows.append({
            "Direction": "LONG" if trade["direction"] == 1 else "SHORT",
            "Entry price": round(trade["entry_price"], decimals),
            "Exit price": (round(trade["exit_price"], decimals)
                           if trade["exit_price"] is not None else None),
            "Outcome": trade["outcome"],
            "Holding days": (int(frame.index[trade["exit_index"]]
                                 - frame.index[trade["entry_index"]])
                             if trade["exit_index"] is not None else None),
            "TP ticks": int(trade["distance_ticks"]),
            "SL ticks": int(trade["distance_ticks"]),
            "PnL ticks": (int(trade["pnl_ticks"])
                          if trade["pnl_ticks"] is not None else None),
        })
    return rows


def accuracy(outcomes):
    return outcomes.count("TP") / len(outcomes) if outcomes else np.nan


@lru_cache(maxsize=512)
def calculate_expression(symbol, contract, strategy, mode, peak_lookback, peak_rank,
                         valley_lookback, valley_rank, max_parallel_positions,
                         max_consecutive_direction, no_trade_days, max_trade_days,
                         swing_lookback, swing_top_count, minimum_swing_ticks,
                         history_years):
    config = PRODUCT_CONFIG[symbol]
    tick_size = config["tick_size"]
    max_parallel_positions = (
        DEFAULT_MAX_PARALLEL_POSITIONS
        if max_parallel_positions is None
        else int(max_parallel_positions)
    )
    if max_parallel_positions < 1:
        raise ValueError("Max parallel positions must be at least 1")
    max_consecutive_direction = (
        DEFAULT_MAX_CONSECUTIVE_DIRECTION
        if max_consecutive_direction is None
        else int(max_consecutive_direction)
    )
    if max_consecutive_direction < 1:
        raise ValueError("Max consecutive same-direction entries must be at least 1")
    no_trade_days = DEFAULT_NO_TRADE_DAYS if no_trade_days is None else int(no_trade_days)
    if no_trade_days < 0:
        raise ValueError("No-trade days before expiry cannot be negative")
    max_trade_days = DEFAULT_MAX_TRADE_DAYS if max_trade_days is None else int(max_trade_days)
    if max_trade_days <= no_trade_days:
        raise ValueError("Maximum days before expiry must exceed no-trade days")
    swing_lookback, swing_top_count = int(swing_lookback), int(swing_top_count)
    minimum_swing_ticks = int(minimum_swing_ticks)
    if swing_lookback < 1 or not 1 <= swing_top_count <= swing_lookback:
        raise ValueError("Swing top count must be between 1 and swing lookback")
    expression = UNIVERSE["contracts"][contract][strategy]
    current_year = 2000 + int(contract[1:])
    history_years = int(history_years)
    if history_years < 1:
        raise ValueError("Number of historical years must be at least 1")
    series_by_year = load_expression_series(
        symbol, expression, current_year, history_years
    )
    if current_year not in series_by_year:
        raise ValueError(f"No current contract-year data for {contract}")

    current = series_by_year[current_year]
    current_upper = boundary(
        current["value"], mode, int(peak_lookback), int(peak_rank), True
    )
    current_lower = boundary(
        current["value"], mode, int(valley_lookback), int(valley_rank), False
    )
    current_swing = strongest_recent_swing_ticks(
        current["value"], tick_size, swing_lookback, swing_top_count)
    current_outcomes, entries, current_trades, current_open = backtest_range(
        current["value"], current_upper, current_lower, current_swing,
        max_parallel_positions, max_consecutive_direction, no_trade_days, max_trade_days,
        minimum_swing_ticks, tick_size,
        include_entries=True,
    )
    current_trade_rows = trade_rows(current_trades, current, config["price_decimals"])
    historical_outcomes, historical_open, historical_pnl = [], 0, 0
    yearly_results = [{
        "Year": current_year, "Wins": current_outcomes.count("TP"),
        "Resolved": len(current_outcomes), "Unresolved": current_open,
        "Win rate": f"{accuracy(current_outcomes):.1%}",
    }]
    for year, frame in series_by_year.items():
        if year >= current_year:
            continue
        upper = boundary(frame["value"], mode, int(peak_lookback), int(peak_rank), True)
        lower = boundary(frame["value"], mode, int(valley_lookback),
                         int(valley_rank), False)
        outcomes, trades, unresolved = backtest_range(
            frame["value"], upper, lower,
            strongest_recent_swing_ticks(
                frame["value"], tick_size, swing_lookback, swing_top_count),
            max_parallel_positions, max_consecutive_direction, no_trade_days,
            max_trade_days,
            minimum_swing_ticks, tick_size,
        )
        historical_outcomes.extend(outcomes)
        historical_open += unresolved
        historical_pnl += sum(trade["pnl_ticks"] or 0 for trade in trades)
        yearly_results.append({
            "Year": year, "Wins": outcomes.count("TP"), "Resolved": len(outcomes),
            "Unresolved": unresolved, "Win rate": f"{accuracy(outcomes):.1%}",
        })
    yearly_results.sort(key=lambda row: row["Year"], reverse=True)
    return {
        "symbol": symbol, "expression": expression, "current_year": current_year,
        "current": current, "upper": current_upper, "lower": current_lower,
        "swing_ticks": current_swing,
        "entries": entries, "current_outcomes": current_outcomes,
        "trade_rows": current_trade_rows,
        "current_open": current_open,
        "historical_outcomes": historical_outcomes,
        "historical_open": historical_open,
        "historical_pnl": int(historical_pnl),
        "yearly_results": yearly_results,
        "max_parallel_positions": max_parallel_positions,
        "max_consecutive_direction": max_consecutive_direction,
        "no_trade_days": no_trade_days,
        "max_trade_days": max_trade_days,
        "tick_size": tick_size, "swing_lookback": swing_lookback,
        "swing_top_count": swing_top_count,
        "minimum_swing_ticks": minimum_swing_ticks,
    }


def make_figure(result):
    current = result["current"]
    figure = make_subplots(
        rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.08,
        row_heights=[0.72, 0.28],
        subplot_titles=(None, f"ceil((average of {result['swing_top_count']} largest "
                        f"swings in last {result['swing_lookback']})^0.9)"),
    )
    figure.add_trace(go.Scatter(
        x=current.index, y=current["value"], mode="lines",
        name=str(result["current_year"]), line={"color": "white", "width": 2},
        customdata=current["date"].dt.strftime("%Y-%m-%d"),
        hovertemplate="Date: %{customdata}<br>Value: %{y}<extra></extra>",
    ), row=1, col=1)
    figure.add_trace(go.Scatter(
        x=current.index, y=result["upper"], mode="lines", name="Upper boundary",
        line={"color": "#fb7185", "width": 2, "shape": "hv"},
    ), row=1, col=1)
    figure.add_trace(go.Scatter(
        x=current.index, y=result["lower"], mode="lines", name="Lower boundary",
        line={"color": "#34d399", "width": 2, "shape": "hv"},
    ), row=1, col=1)
    for direction, color, symbol in (
        ("LONG", "#22c55e", "triangle-up"),
        ("SHORT", "#ef4444", "triangle-down"),
    ):
        selected = [entry for entry in result["entries"] if entry[1] == direction]
        figure.add_trace(go.Scatter(
            x=[current.index[entry[0]] for entry in selected],
            y=[entry[2] for entry in selected], mode="markers",
            name=f"{direction} entry",
            marker={"color": color, "size": 13, "symbol": symbol,
                    "line": {"color": "white", "width": 1}},
            customdata=[current.iloc[entry[0]]["date"].strftime("%Y-%m-%d")
                        for entry in selected],
            hovertemplate=(f"{direction}<br>Date: %{{customdata}}"
                           "<br>Entry: %{y}<extra></extra>"),
        ), row=1, col=1)
    entries = result["entries"]
    figure.add_trace(go.Scatter(
        x=[current.index[entry[0]] for entry in entries],
        y=[max(entry[3], entry[4]) for entry in entries], mode="text",
        text=[
            (f"TP: {round(abs(entry[3] - entry[2]) / result['tick_size'])} ticks"
             if entry[1] == "LONG"
             else f"SL: {round(abs(entry[4] - entry[2]) / result['tick_size'])} ticks")
            for entry in entries
        ],
        textposition="top center", textfont={"color": "#f8fafc", "size": 11},
        name="Upper TP/SL values", showlegend=False, hoverinfo="skip",
    ), row=1, col=1)
    figure.add_trace(go.Scatter(
        x=[current.index[entry[0]] for entry in entries],
        y=[min(entry[3], entry[4]) for entry in entries], mode="text",
        text=[
            (f"SL: {round(abs(entry[4] - entry[2]) / result['tick_size'])} ticks"
             if entry[1] == "LONG"
             else f"TP: {round(abs(entry[3] - entry[2]) / result['tick_size'])} ticks")
            for entry in entries
        ],
        textposition="bottom center", textfont={"color": "#cbd5e1", "size": 11},
        name="Lower TP/SL values", showlegend=False, hoverinfo="skip",
    ), row=1, col=1)
    figure.add_trace(go.Scatter(
        x=current.index, y=result["swing_ticks"], mode="lines",
        name="Transformed swing ticks",
        line={"color": "#f59e0b", "width": 2, "shape": "hv"},
        customdata=current["date"].dt.strftime("%Y-%m-%d"),
        hovertemplate="Date: %{customdata}<br>Transformed swing: %{y:.0f} ticks<extra></extra>",
    ), row=2, col=1)
    figure.update_layout(
        title=f"{result['symbol']} | {result['expression']}",
        template="plotly_dark", paper_bgcolor="black", plot_bgcolor="black",
        hovermode="x unified", height=1050,
    )
    figure.update_xaxes(gridcolor="#333")
    figure.update_xaxes(title_text="Working days to expiry", row=2, col=1)
    figure.update_yaxes(title_text="Value", gridcolor="#333", row=1, col=1)
    figure.update_yaxes(title_text="Transformed swing (ticks)", gridcolor="#333",
                        row=2, col=1)
    return figure


app = Dash(__name__)
app.title = "Range Testing"
INPUT_STYLE = {
    "width": "110px", "backgroundColor": "white", "color": "black",
    "border": "1px solid #94a3b8", "borderRadius": "4px",
    "padding": "6px 8px", "fontWeight": "600",
}


def number_control(label, control_id, value, minimum=1, step=1):
    return html.Label([
        html.Span(label, style={"display": "block", "marginBottom": "5px"}),
        dcc.Input(id=control_id, type="number", min=minimum, step=step,
                  value=value, debounce=True, style=INPUT_STYLE,
                  persistence=True, persistence_type="local"),
    ])


def dropdown_control(label, control_id, options, value, width="145px"):
    return html.Label([
        html.Span(label, style={"display": "block", "marginBottom": "5px"}),
        dcc.Dropdown(id=control_id, options=options, value=value, clearable=False,
                     persistence=True, persistence_type="local",
                     style={"width": width, "color": "black"}),
    ])


def result_table(table_id, columns, width, **kwargs):
    return dash_table.DataTable(
        id=table_id,
        columns=[{"name": column, "id": column} for column in columns],
        style_table={"width": width, "maxWidth": "calc(100vw - 40px)",
                     "overflowX": "auto", "margin": "0 auto 30px"},
        style_header={"backgroundColor": "#1e293b", "color": "white",
                      "fontWeight": "700"},
        style_cell={"backgroundColor": "#0f172a", "color": "white",
                    "border": "1px solid #334155", "padding": "7px",
                    "textAlign": "center"},
        **kwargs,
    )


CONTROLS = (
    ("Peak lookback", "peak-lookback", DEFAULT_PEAK_LOOKBACK, 1),
    ("Peak rank", "peak-rank", DEFAULT_PEAK_RANK, 1),
    ("Valley lookback", "valley-lookback", DEFAULT_VALLEY_LOOKBACK, 1),
    ("Valley rank", "valley-rank", DEFAULT_VALLEY_RANK, 1),
    ("Max parallel positions", "max-parallel-positions", DEFAULT_MAX_PARALLEL_POSITIONS, 1),
    ("Max consecutive one direction", "max-consecutive-direction", DEFAULT_MAX_CONSECUTIVE_DIRECTION, 1),
    ("No trade days before expiry", "no-trade-days", DEFAULT_NO_TRADE_DAYS, 0),
    ("Maximum days before expiry", "max-trade-days", DEFAULT_MAX_TRADE_DAYS, 1),
    ("Swing lookback", "swing-lookback", SWING_DIFFERENCE_LOOKBACK, 1),
    ("Largest swings", "swing-top-count", SWING_DIFFERENCE_TOP_COUNT, 1),
    ("Minimum swing ticks", "minimum-swing-ticks",
     next(iter(PRODUCT_CONFIG[DEFAULT_SYMBOL]["buckets"].values()))[
         "minimum_swing_ticks"
     ][0], 1),
    ("Historical years", "history-years", DEFAULT_HISTORY_YEARS, 1),
)


app.layout = html.Div([
    html.Div([
        html.Strong("Range settings", style={"alignSelf": "center", "color": "#f97316"}),
        dropdown_control("Symbol", "symbol", TEST_SYMBOLS, DEFAULT_SYMBOL, "110px"),
        dropdown_control("Calculation", "mode", [
            {"label": "Peaks / valleys", "value": "extrema"},
            {"label": "All trailing days", "value": "days"},
        ], DEFAULT_MODE, "175px"),
        *[number_control(label, control_id, value, minimum)
          for label, control_id, value, minimum in CONTROLS],
    ], style={
        "display": "grid",
        "gridTemplateColumns": "repeat(auto-fit, minmax(145px, 1fr))",
        "alignItems": "end", "gap": "16px", "padding": "18px 24px",
        "background": "#111827", "color": "white",
        "borderBottom": "1px solid #334155",
    }),
    html.Div([
        html.Strong("Expression columns", style={"color": "#f97316"}),
        dcc.Checklist(
            id="column-selector",
            options=[{"label": strategy, "value": strategy} for strategy in STRATEGIES],
            value=STRATEGIES,
            persistence=True,
            persistence_type="local",
            inline=True,
            labelStyle={"marginRight": "18px"},
        ),
    ], style={"padding": "12px 20px", "background": "#111827", "color": "white"}),
    html.Div(id="selected-expression", style={
        "padding": "12px 20px", "background": "#0f172a", "color": "white",
        "fontSize": "18px", "fontWeight": "600",
    }),
    dash_table.DataTable(
        id="expression-table",
        columns=[{"name": "Contract", "id": "Contract"}]
                + [{"name": strategy, "id": strategy} for strategy in STRATEGIES],
        data=DEFAULT_ROWS,
        active_cell={"row": DEFAULT_ROW, "column": STRATEGIES.index(DEFAULT_STRATEGY) + 1,
                     "column_id": DEFAULT_STRATEGY, "row_id": None},
        fixed_rows={"headers": True},
        style_table={"width": "fit-content", "maxWidth": "calc(100vw - 40px)",
                     "margin": "0 auto", "overflowX": "auto", "maxHeight": "520px",
                     "border": "1px solid #334155"},
        style_header={"backgroundColor": "#1e293b", "color": "white",
                      "fontWeight": "700"},
        style_cell={"backgroundColor": "#0f172a", "color": "white",
                    "border": "1px solid #334155", "padding": "7px",
                    "width": "210px", "minWidth": "210px", "maxWidth": "210px",
                    "textAlign": "center",
                    "overflow": "hidden", "textOverflow": "ellipsis"},
        css=[{
            "selector": ".dash-spreadsheet-container .dash-spreadsheet-inner",
            "rule": "width: fit-content; min-width: 0;",
        }],
        style_data_conditional=[
            {"if": {"column_id": "Contract"}, "width": "90px",
             "minWidth": "90px", "maxWidth": "90px"},
            {"if": {"state": "active"}, "backgroundColor": "#1d4ed8",
             "border": "2px solid #93c5fd"},
        ],
    ),
    html.Div(id="accuracy-summary", style={
        "display": "flex", "gap": "30px", "padding": "12px 20px",
        "background": "#0f172a", "color": "white", "fontSize": "18px",
    }),
    html.Div(id="parameter-error", style={"color": "#f87171", "padding": "0 20px"}),
    dcc.Loading(dcc.Graph(id="seasonality-chart"), type="circle"),
    html.H3("Trade analysis", style={"color": "white", "textAlign": "center"}),
    result_table("trade-table", (
        "Direction", "Entry price", "Exit price", "Outcome", "TP ticks",
        "SL ticks", "Holding days", "PnL ticks",
    ), "fit-content", page_size=25, sort_action="native", filter_action="native",
        style_data_conditional=[
            {"if": {"filter_query": "{PnL ticks} > 0", "column_id": "PnL ticks"},
             "color": "#4ade80"},
            {"if": {"filter_query": "{PnL ticks} < 0", "column_id": "PnL ticks"},
             "color": "#f87171"},
        ]),
    html.H3("Year-wise success rate", style={"color": "white", "textAlign": "center"}),
    result_table("yearly-table",
                 ("Year", "Wins", "Resolved", "Unresolved", "Win rate"), "650px"),
], style={"background": "black", "minHeight": "100vh"})


@app.callback(
    Output("expression-table", "columns"),
    Input("column-selector", "value"),
)
def select_expression_columns(selected):
    selected = selected or []
    return ([{"name": "Contract", "id": "Contract"}]
            + [{"name": strategy, "id": strategy}
               for strategy in STRATEGIES if strategy in selected])


@app.callback(
    Output("expression-table", "active_cell"),
    Input("symbol", "value"),
)
def select_symbol(symbol):
    rows = EXPRESSION_ROWS.get(symbol, [])
    if not rows:
        return None
    strategy = next((name for name in STRATEGIES if rows[0].get(name)), None)
    active = ({"row": 0, "column": STRATEGIES.index(strategy) + 1,
               "column_id": strategy, "row_id": None} if strategy else None)
    return active


@app.callback(
    Output("seasonality-chart", "figure"),
    Output("accuracy-summary", "children"),
    Output("parameter-error", "children"),
    Output("selected-expression", "children"),
    Output("trade-table", "data"),
    Output("yearly-table", "data"),
    Output("expression-table", "data"),
    Input("symbol", "value"),
    Input("expression-table", "active_cell"),
    Input("mode", "value"),
    Input("peak-lookback", "value"), Input("peak-rank", "value"),
    Input("valley-lookback", "value"), Input("valley-rank", "value"),
    Input("max-parallel-positions", "value"),
    Input("max-consecutive-direction", "value"),
    Input("no-trade-days", "value"),
    Input("max-trade-days", "value"),
    Input("swing-lookback", "value"),
    Input("swing-top-count", "value"),
    Input("minimum-swing-ticks", "value"),
    Input("history-years", "value"),
    Input("column-selector", "value"),
    State("expression-table", "data"),
)
def update_expression(symbol, active_cell, mode, peak_lookback, peak_rank,
                      valley_lookback, valley_rank, max_parallel_positions,
                      max_consecutive_direction, no_trade_days, max_trade_days,
                      swing_lookback, swing_top_count, minimum_swing_ticks,
                      history_years, selected_strategies, existing_table_rows):
    try:
        if not active_cell or active_cell.get("column_id") == "Contract":
            raise ValueError("Click a strategy expression cell in the table")
        row = int(active_cell["row"])
        rows_for_symbol = visible_expression_rows(symbol, selected_strategies)
        contract = rows_for_symbol[row]["Contract"]
        strategy = active_cell["column_id"]
        if not rows_for_symbol[row].get(strategy):
            raise ValueError("This expression has unavailable contract legs")
        result = calculate_expression(
            symbol, contract, strategy, mode, peak_lookback, peak_rank,
            valley_lookback, valley_rank, max_parallel_positions,
            max_consecutive_direction, no_trade_days, max_trade_days, swing_lookback,
            swing_top_count, minimum_swing_ticks, history_years,
        )
        current = result["current_outcomes"]
        historical = result["historical_outcomes"]
        rows = result["trade_rows"]
        total_pnl = sum(row["PnL ticks"] or 0 for row in rows)
        total_holding_days = sum(row["Holding days"] or 0 for row in rows)
        summary = [
            html.Span(
                f"Current: {current.count('TP')}/{len(current)} wins "
                f"({accuracy(current):.1%}), unresolved: {result['current_open']}"
            ),
            html.Span(
                f"Historical: {historical.count('TP')}/{len(historical)} wins "
                f"({accuracy(historical):.1%}), unresolved: {result['historical_open']} | "
                f"PnL: {result['historical_pnl']:+d} ticks"
            ),
            html.Span(f"Total realized PnL: {total_pnl:+d} ticks"),
            html.Span(f"Total holding days: {total_holding_days}"),
        ]
        selected = f"Selected: {symbol} / {contract} / {strategy} / {result['expression']}"
        source_rows = rows_for_symbol
        expected_contracts = [item["Contract"] for item in source_rows]
        existing_contracts = [item.get("Contract") for item in (existing_table_rows or [])]
        table_rows = ([dict(item) for item in existing_table_rows]
                      if existing_contracts == expected_contracts
                      else [dict(item) for item in source_rows])
        table_rows[row][strategy] = (
            f"{accuracy(historical):.1%} | {len(historical)} trades"
            if historical else "No historical trades"
        )
        return (make_figure(result), summary, "", selected, rows,
                result["yearly_results"], table_rows)
    except Exception as error:
        return (go.Figure(), [], str(error), "No expression selected", [], [],
                visible_expression_rows(symbol, selected_strategies))


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8051, debug=False)

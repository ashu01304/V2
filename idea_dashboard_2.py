"""SEAC seasonality builder on port 8056."""

import argparse
from functools import lru_cache
from math import isfinite

import duckdb
import pandas as pd
import plotly.graph_objects as go
from dash import Dash, Input, Output, State, Patch, MATCH, dash_table, dcc, html

from requests import RequestException
from dash.exceptions import PreventUpdate

from analysis.contract_universe import load_expiries
from analysis.expression import format_contract_label
from market_data import LiveMarketDataClient, MarketData
from market_data.queries import database_signature
from dashboard_components.seac_builder import builder_layout, register_seac_callbacks

MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
PAIRS = {month + 1: f"{label}-{MONTHS[(month + 1) % 12]}" for month, label in enumerate(MONTHS)}
CELL_STYLE = {"backgroundColor": "#111827", "color": "#e2e8f0", "border": "1px solid #334155", "padding": "8px"}


def tick_number(value, label):
    if value in (None, ""):
        return None
    number = float(value)
    if not isfinite(number) or not number.is_integer():
        raise ValueError(f"{label} must be a whole number of ticks.")
    return int(number)


def rolling_pairs(now=None):
    """List 12 calendar spreads from the first common unexpired CL/CO month."""
    now = pd.Timestamp.now(tz="UTC") if now is None else pd.Timestamp(now)
    cl, co = dict(load_expiries("CL")), dict(load_expiries("CO"))
    codes = "FGHJKMNQUVXZ"
    active = [contract for contract in cl.keys() & co.keys() if min(cl[contract], co[contract]) >= now.date()]
    if active:
        first = min(active, key=lambda contract: (int(contract[1:]), codes.index(contract[0])))
        start = pd.Timestamp(2000 + int(first[1:]), codes.index(first[0]) + 1, 1)
    else:
        start = now.tz_localize(None).normalize().replace(day=1)
    months = pd.date_range(start, periods=13, freq="MS")
    result = []
    for near, far in zip(months, months[1:]):
        first, second = (f"{codes[date.month-1]}{date.year % 100:02d}" for date in (near, far))
        result.append({"month": near.month, "pair": PAIRS[near.month], "first": first, "second": second,
                       "contract": format_contract_label(f"{first}-{second}")})
    return result


def live_month_ticks(now=None):
    """Nearest unexpired month pairs, using fresh matching CL/CO outright legs."""
    now = pd.Timestamp.now(tz="UTC") if now is None else pd.Timestamp(now)
    client = LiveMarketDataClient(timeout=2)
    try:
        if not client.health().get("status", "").startswith("CONNECTED:"):
            return {}, "Live feed disconnected"
        legs = {}
        for row in client.snapshot(["CL", "CO"]):
            try:
                value = float(row["value"])
                received = pd.to_datetime(row["received_at"], utc=True)
                if isfinite(value) and value != -100 and pd.notna(received) and 0 <= (now-received).total_seconds() <= 120:
                    legs[(row["product"], row["contract"])] = (value, received)
            except (KeyError, TypeError, ValueError):
                continue
        result = {}
        for pair in rolling_pairs(now):
            month, first, second = pair["month"], pair["first"], pair["second"]
            keys = [("CL", first), ("CO", first), ("CL", second), ("CO", second)]
            if not all(key in legs for key in keys):
                continue
            values = [legs[key][0] for key in keys]
            result[month] = {"live": int(round((values[0]-values[1]-values[2]+values[3])*100)),
                             "contract": format_contract_label(f"{first}-{second}"),
                             "live_asof": min(legs[key][1] for key in keys).isoformat()}
        return result, f"Live prices: {len(result)}/12 pairs available | refreshed {now.strftime('%H:%M:%S UTC')}"
    except (RequestException, OSError, KeyError, TypeError, ValueError) as error:
        return {}, f"Live prices unavailable: {error}"
    finally:
        client.close()


def parse_filters(rows, now=None):
    now = pd.Timestamp.now(tz="UTC") if now is None else pd.Timestamp(now)
    filters = {}
    for row in rows or []:
        month = int(row["month"])
        k = tick_number(row.get("k"), f"{PAIRS[month]} +/-K")
        if k is None:
            continue
        if k < 0:
            raise ValueError("+/-K must be zero or positive.")
        live = tick_number(row.get("live"), "Live price")
        asof = pd.to_datetime(row.get("live_asof"), utc=True)
        if live is None or pd.isna(asof) or not 0 <= (now-asof).total_seconds() <= 120:
            raise ValueError(f"{PAIRS[month]}: a fresh live price is required for +/-K.")
        filters[month] = ((live-k)/100, (live+k)/100)
    return filters


def visible_pairs(skip_months=3):
    skip = max(0, min(11, int(3 if skip_months is None else skip_months)))
    return rolling_pairs()[skip:]


def live_marker(row, now=None):
    now = pd.Timestamp.now(tz="UTC") if now is None else pd.Timestamp(now)
    price = row.get("live")
    asof = pd.to_datetime(row.get("live_asof"), utc=True)
    if price is None or not isfinite(float(price)) or pd.isna(asof) or not 0 <= (now-asof).total_seconds() <= 120:
        return [], []
    return [int(price)], [.06]


def central_95_prices(values):
    """Frequency-weighted percentile bounds; retain whole one-tick price bins."""
    values = values.sort_values("price")
    if values.empty or values.frequency.sum() == 0:
        return values
    cumulative = values.frequency.cumsum()
    total = cumulative.iloc[-1]
    low = int(cumulative.searchsorted(total * .025, side="left"))
    high = int(cumulative.searchsorted(total * .975, side="left"))
    return values.iloc[low:high + 1]


def histogram_cards(frame, pairs=None):
    cards = []
    for pair in (pairs if pairs is not None else visible_pairs())[:8]:
        month = int(pair["month"])
        label = pair.get("contract") or PAIRS[month]
        values = central_95_prices(frame.loc[frame.month == month])
        count = int(values.frequency.sum())
        figure = go.Figure(go.Bar(x=[int(round(price*100)) for price in values.price], y=values.frequency.tolist(), width=.8,
            marker_color="#60a5fa", hovertemplate="Price: %{x:.0f} ticks<br>Frequency: %{y:,}<extra></extra>"))
        live_x, live_y = live_marker(pair)
        figure.add_trace(go.Scatter(x=live_x, y=live_y, yaxis="y2", mode="markers", name="Live price",
            marker={"color": "#22c55e", "size": 12, "line": {"color": "#ffffff", "width": 2}},
            hovertemplate="Live price: %{x:.0f} ticks<extra></extra>"))
        figure.update_layout(title=f"{label} | central 95% | {count:,} observations", template="plotly_dark",
            paper_bgcolor="#111827", plot_bgcolor="#111827", height=320,
            font={"color": "#f8fafc"}, hoverlabel={"bgcolor": "#1e293b", "font_color": "#f8fafc"},
            showlegend=False, dragmode="zoom", margin={"l": 60, "r": 15, "t": 50, "b": 55},
            xaxis={"tickformat": ",d"},
            yaxis={"rangemode": "tozero", "tickformat": ",d"},
            yaxis2={"overlaying": "y", "range": [0, 1], "visible": False, "fixedrange": True},
            uirevision=f"histogram-{month}")
        if not count:
            figure.add_annotation(text="No matching prices", x=.5, y=.5, xref="paper", yref="paper", showarrow=False)
        cards.append(dcc.Graph(id={"type": "filter-histogram", "month": month}, figure=figure,
                               config={"scrollZoom": False, "displaylogo": False}))
    return cards


app = Dash(__name__, assets_folder="idea_dashboard_2_assets", title="SEAC | Seasonality workspace")


def conditional_layout():
    return html.Div([
        html.H2("CL-CO | Conditional price frequencies"),
        html.Label(["Past N days ", dcc.Input(id="filter-days", type="number", min=1, max=3650, step=1,
            value=30, persistence=True, persistence_type="local", style={"width": "75px"})]),
        html.Label([" Skip first N months ", dcc.Input(id="filter-skip", type="number", min=0, max=11,
            step=1, value=3, debounce=True, persistence=True, persistence_type="local", style={"width": "65px"})]),
        html.Div(id="filter-live-status", children="Waiting for live prices...", style={"color": "#cbd5e1", "margin": "12px 0"}),
        dash_table.DataTable(id="filter-ranges", data=[{"month": pair["month"], "pair": pair["pair"],
            "k": None, "live": None, "contract": pair["contract"], "live_asof": None}
            for pair in visible_pairs()], columns=[
                {"name": "Calendar spread", "id": "pair", "editable": False},
                {"name": "Live contract", "id": "contract", "editable": False},
                {"name": "Live (ticks)", "id": "live", "type": "numeric", "editable": False, "format": {"specifier": ".0f"}},
                {"name": "+/-K (ticks)", "id": "k", "type": "numeric", "editable": True}],
            style_table={"maxWidth": "1000px", "margin": "20px 0", "overflowX": "auto"},
            style_header={"backgroundColor": "#1e293b", "color": "white", "fontWeight": "bold"}, style_cell=CELL_STYLE),
        html.Button("Refresh now", id="filter-run", n_clicks=0),
        html.P("Across years: Jan-Feb 2026 and Jan-Feb 2027 are pooled only after filtering each year separately. "
               "Dec-Jan uses December's contract year. Each observation is one matching minute in one contract year; "
               "multiple contract years at the same timestamp count separately. Missing prices are never filled.",
               style={"fontSize": "12px", "color": "#94a3b8"}),
        html.P("Live quotes and +/-K calculations update automatically every 10 seconds. "
               "Changing K or the lookback also updates results.", style={"fontSize": "12px", "color": "#94a3b8"}),
        dcc.Interval(id="filter-live-tick", interval=10_000),
        dcc.Loading(html.Div([
            html.Div("Loading live prices and filtered results...", id="filter-status", style={"margin": "16px 0"}),
            html.Div(id="filter-histograms", style={"display": "grid", "gap": "16px",
                "gridTemplateColumns": "repeat(auto-fit, minmax(min(100%, 400px), 1fr))"}),
        ])),
    ], style={"background": "#111827", "color": "white", "minHeight": "100vh", "padding": "24px", "fontFamily": "Arial, sans-serif"})


def layout():
    return html.Div(builder_layout(), className="ideas2-workspace")


app.layout = layout
register_seac_callbacks(app)

def refresh_live(_ticks, skip_months, rows, active_tab="conditional"):
    if active_tab != "conditional":
        raise PreventUpdate
    quotes, status = live_month_ticks()
    pairs = visible_pairs(skip_months)
    contracts = {pair["month"]: pair["contract"] for pair in pairs}
    if [row["month"] for row in rows or []] != [pair["month"] for pair in pairs]:
        previous = {row["month"]: row for row in rows or []}
        return [{"month": pair["month"], "pair": pair["pair"], "k": previous.get(pair["month"], {}).get("k"),
                 "contract": pair["contract"], "live": None, "live_asof": None,
                 **quotes.get(pair["month"], {})} for pair in pairs], status
    update = Patch()
    for index, row in enumerate(rows or []):
        quote = quotes.get(int(row["month"]), {})
        for key in ("live", "contract", "live_asof"):
            update[index][key] = quote.get(key, contracts.get(int(row["month"]), "") if key == "contract" else None)
    return update, status


def refresh_histogram_live(rows, graph_id, active_tab="conditional"):
    if active_tab != "conditional":
        raise PreventUpdate
    row = next((row for row in rows or [] if int(row["month"]) == graph_id["month"]), {})
    x, y = live_marker(row)
    update = Patch()
    update["data"][1]["x"] = x
    update["data"][1]["y"] = y
    return update


@lru_cache(maxsize=8)
def histogram_results(filters, days, minute, signature):
    """Reuse results only while ranges, reference minute, and stored data are unchanged."""
    end = pd.Timestamp(minute)
    db = MarketData()
    try:
        return db.calendar_filter_histograms(dict(filters), end-pd.Timedelta(days=days), end)
    finally:
        db.close()


def apply_filters(_clicks, rows, days, skip_months=3, active_tab="conditional"):
    if active_tab != "conditional":
        raise PreventUpdate
    try:
        days = tick_number(days, "Past N days")
        if days is None or not 1 <= days <= 3650:
            raise ValueError("Past N days must be between 1 and 3650.")
        now = pd.Timestamp.now(tz="UTC")
        previous = {int(row["month"]): row for row in rows or []}
        rows = [{**pair, **previous.get(pair["month"], {})} for pair in visible_pairs(skip_months)]
        filters = parse_filters(rows)
        frame, stats = histogram_results(tuple(sorted(filters.items())), days, now.floor("min").isoformat(),
                                        tuple(database_signature() or ()))
        status = (f"Updated {now.strftime('%H:%M:%S UTC')} | Past {days} days | {len(filters)} active filters | {stats['samples']:,} matching minute/contract-year observations | "
                  f"{stats['minutes']:,} distinct UTC minutes | {stats['years']} contract years. "
                  "Each histogram counts available prices at those matches; target counts may differ when prices are missing.")
        if filters:
            ranges = [f"{PAIRS[month]}: {round(low*100) if low is not None else 'any'} to "
                      f"{round(high*100) if high is not None else 'any'} ticks" for month, (low, high) in filters.items()]
            status += " Applied ranges: " + "; ".join(ranges)
        return histogram_cards(frame, rows or None), status
    except (ValueError, TypeError, KeyError, duckdb.Error, OSError, RuntimeError) as error:
        return [], f"Unable to load results: {error}"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8056)
    args = parser.parse_args()
    app.run(host="127.0.0.1", port=args.port, debug=False)

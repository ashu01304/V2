"""On-demand minute charts used in the selected-cell popup."""

from math import isfinite

import duckdb
import pandas as pd
import plotly.graph_objects as go
from dash import Input, Output, State, dcc, html, no_update
from requests import RequestException

from analysis.intraday_chart import TIMEFRAMES, load_candles, make_intraday_figure
from analysis.session_overlay import load_session_minutes, make_session_overlay, make_session_live_trace
from market_data.live_client import LiveMarketDataClient
from market_data.config import DATABASE_PATH

GRAPH_CONFIG = {"displaylogo": False, "displayModeBar": False, "scrollZoom": False}
DATA_ERRORS = (ValueError, TypeError, OSError, RuntimeError, duckdb.Error)


def minute_charts_layout():
    return html.Div([
        html.Div([
            html.Div(dcc.Dropdown(
                id="popup-candle-timeframe",
                options=[{"label": f"{minutes} minutes", "value": minutes} for minutes in TIMEFRAMES],
                value=15, clearable=False, persistence=True, persistence_type="local",
                className="popup-timeframe",
            ), className="popup-minute-controls"),
            html.Div(dcc.Graph(
                id="popup-candle-chart", figure=go.Figure(), config=GRAPH_CONFIG,
                responsive=True, style={"height": "610px", "width": "100%"},
            )),
        ], className="popup-minute-panel"),
        html.Div([
            html.Div([
                html.Label(["Past N days", dcc.Input(
                    id="popup-overlay-days", type="number", min=1, step=1, value=16,
                    debounce=True, persistence=True, persistence_type="local",
                    className="popup-day-count",
                )], title="Calendar days including the current session; weekends are omitted."),
                dcc.Checklist(
                    id="popup-overlay-zero-base",
                    options=[{"label": " Zero base", "value": "yes"}], value=["yes"],
                    persistence=True, persistence_type="local",
                ),
            ], className="popup-minute-controls"),
            html.Div([
                dcc.Store(id="popup-overlay-data"),
                dcc.Graph(
                    id="popup-overlay-chart", figure=go.Figure(), config=GRAPH_CONFIG,
                    responsive=True, style={"height": "610px", "width": "100%"},
                ),
                dcc.Store(id="popup-minute-publication"),
                dcc.Store(id="popup-minute-live"),
                dcc.Store(id="popup-minute-live-ack"),
            ]),
        ], className="popup-minute-panel"),
    ], id="popup-minute-charts", className="popup-minute-charts")


def chart_error(message):
    figure = go.Figure().update_layout(
        template="plotly_dark", height=610, paper_bgcolor="#111827", plot_bgcolor="#111827",
        xaxis={"visible": False}, yaxis={"visible": False},
    )
    figure.add_annotation(text=str(message), x=0.5, y=0.5,
                          xref="paper", yref="paper", showarrow=False)
    return figure


def fetch_live_quote(product, expression):
    quote = None
    client = LiveMarketDataClient(timeout=2)
    try:
        quote = client.expression(product, expression)
    except (RequestException, KeyError, ValueError, TypeError):
        pass
    finally:
        client.close()
    return quote


def database_signature():
    try:
        stat = DATABASE_PATH.stat()
        # Browser numbers lose precision above 2**53. Keep signatures exact.
        return [str(stat.st_mtime_ns), str(stat.st_size), str(stat.st_ino)]
    except OSError:
        return None


def update_candle_chart(selection, timeframe, _publication=None):
    if not selection:
        return no_update
    product, expression = selection["symbol"], selection["expression"]
    signature = database_signature()
    try:
        candles = load_candles(product, expression, int(timeframe))
    except DATA_ERRORS as error:
        return chart_error(error)
    quote = fetch_live_quote(product, expression)
    figure = make_intraday_figure(candles, product, expression, int(timeframe), quote)
    figure.update_layout(title=None, showlegend=False, height=610,
                         meta={"minute_database_signature": signature, "selection": selection},
                         margin={"l": 55, "r": 20, "t": 15, "b": 50})
    return figure


def load_overlay_data(selection, past_days, _publication=None):
    if not selection:
        return None
    signature = database_signature()
    try:
        minutes = load_session_minutes(selection["symbol"], selection["expression"], past_days)
        return {"selection": selection, "past_days": past_days, "database_signature": signature,
                "timestamps": [timestamp.isoformat() for timestamp in minutes.index],
                "prices": minutes["price"].tolist(),
                "live_quote": fetch_live_quote(selection["symbol"], selection["expression"])}
    except DATA_ERRORS as error:
        return {"error": str(error), "selection": selection, "past_days": past_days,
                "database_signature": signature}


def update_overlay_chart(data, zero_base):
    if not data:
        return no_update
    try:
        if "error" in data:
            return chart_error(data["error"])
        selection = data["selection"]
        minutes = pd.DataFrame({"price": data["prices"]},
                               index=pd.to_datetime(data["timestamps"], utc=True))
        figure = make_session_overlay(
            minutes, selection["symbol"], selection["expression"], bool(zero_base),
            live_quote=data.get("live_quote"),
        )
        figure.update_layout(
            height=610, uirevision=f"{figure.layout.uirevision}:{data['past_days']}",
        )
        return figure
    except (ValueError, TypeError, KeyError) as error:
        return chart_error(error)


def refresh_minute_live_markers(_tick, selection, data, zero_base, candle_figure, overlay_figure):
    if not selection:
        return None, no_update
    signature = database_signature()
    meta = (candle_figure or {}).get("layout", {}).get("meta") or {}
    overlay_changed = (data and data.get("selection") == selection
                       and data.get("database_signature") is not None
                       and data["database_signature"] != signature)
    candle_changed = (meta.get("selection") == selection
                      and meta.get("minute_database_signature") is not None
                      and meta["minute_database_signature"] != signature)
    publication = signature if signature and (overlay_changed or candle_changed) else no_update
    quote = fetch_live_quote(selection["symbol"], selection["expression"])
    candle_trace = go.Scatter(
        x=[], y=[], mode="markers", name="Live price", showlegend=False,
        uid="intraday-live",
        cliponaxis=False,
        marker={"size": 12, "color": "#22c55e",
                "line": {"color": "#dcfce7", "width": 2}},
    )
    decimals = 2 if selection["symbol"] in {"CL", "CO", "CL-CO"} else 3
    candle_trace.hovertemplate = (f"%{{x|%Y-%m-%d %H:%M:%S}} UTC"
                                  f"<br>Live: %{{y:.{decimals}f}}<extra></extra>")
    if quote:
        try:
            timestamp = pd.to_datetime(quote["timestamp"], utc=True)
            price = float(quote["value"])
            if timestamp.dayofweek < 5 and isfinite(price):
                candle_trace.x, candle_trace.y = [timestamp], [price]
        except (KeyError, TypeError, ValueError, OverflowError):
            pass
    payload = {
        "selection": selection, "zero_base": bool(zero_base),
        "candle": candle_trace.to_plotly_json() if (candle_figure or {}).get("data") else None,
    }
    # A slow response for a previously selected cell must not mark the new series.
    if not data or data.get("selection") != selection or "error" in data:
        return payload, publication
    minutes = pd.DataFrame({"price": data["prices"]},
                           index=pd.to_datetime(data["timestamps"], utc=True))
    overlay_trace = make_session_live_trace(minutes, selection["symbol"], bool(zero_base), quote)
    if (overlay_figure or {}).get("data"):
        payload["overlay"] = overlay_trace.to_plotly_json()
    return payload, publication


def register_minute_chart_callbacks(app):
    # History loads after selection, control changes, or a database publication.
    app.callback(
        Output("popup-candle-chart", "figure"),
        Input("selected-cell", "data"), Input("popup-candle-timeframe", "value"),
        Input("popup-minute-publication", "data"),
        prevent_initial_call=True,
    )(update_candle_chart)
    app.callback(
        Output("popup-overlay-data", "data"),
        Input("selected-cell", "data"), Input("popup-overlay-days", "value"),
        Input("popup-minute-publication", "data"),
        prevent_initial_call=True,
    )(load_overlay_data)
    # Zero-base reuses the browser's loaded minutes and never touches the database.
    app.callback(
        Output("popup-overlay-chart", "figure"),
        Input("popup-overlay-data", "data"), Input("popup-overlay-zero-base", "value"),
        prevent_initial_call=True,
    )(update_overlay_chart)

    # Check only the file signature each tick; reload history only after publication.
    app.callback(
        Output("popup-minute-live", "data"),
        Output("popup-minute-publication", "data"),
        Input("live-chart-refresh", "n_intervals"),
        State("selected-cell", "data"), State("popup-overlay-data", "data"),
        State("popup-overlay-zero-base", "value"),
        State("popup-candle-chart", "figure"), State("popup-overlay-chart", "figure"),
        prevent_initial_call=True,
    )(refresh_minute_live_markers)

    # Restyle only the marker in Plotly; do not send Graph.figure updates on ticks.
    app.clientside_callback(
        """
        function(data, selection, zeroBase) {
            return window.marketCurveMinuteMarkers.update(data, selection, zeroBase);
        }
        """,
        Output("popup-minute-live-ack", "data"),
        Input("popup-minute-live", "data"), Input("selected-cell", "data"),
        Input("popup-overlay-zero-base", "value"),
        prevent_initial_call=True,
    )

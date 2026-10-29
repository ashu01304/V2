"""Small Dash explorer for the endpoints outside the candle comparison."""

from datetime import date, time, timedelta
import json

import pandas as pd
import plotly.graph_objects as go
from dash import Input, Output, State, dash_table, dcc, html

from . import QHClient, QHAPIError
from .endpoints import fairvalue, fairvalue_historical, tas
from .probe import safe_preview

ENDPOINTS = {"fairvalue": "Fairvalue - latest prices", "fairvalue_historical": "Fairvalue - history",
             "tas": "TAS - individual trades"}
ROW = {"display": "flex", "flexWrap": "wrap", "gap": "10px", "margin": "12px 0"}


def explorer_layout():
    return html.Section([
        html.H3("Other endpoints"),
        html.Div("Choose an endpoint and click Fetch. Blank instruments use the contract selected above; comma-separated codes are supported."),
        html.Div([
            dcc.Dropdown([{"label": label, "value": key} for key, label in ENDPOINTS.items()],
                         "fairvalue", id="explore-endpoint", clearable=False, style={"width": "270px"}),
            dcc.Input(id="explore-symbols", placeholder="Instruments, e.g. CLZ26-F27-G27", style={"width": "300px"}),
            html.Button("Fetch", id="explore-fetch", n_clicks=0),
            dcc.Checklist([{"label": "Fresh request (bypass cache)", "value": "fresh"}], [], id="explore-fresh"),
        ], style=ROW),
        html.Div([
            html.Span("Fairvalue history:"),
            dcc.Dropdown(["1m", "5m", "15m", "1h", "4h", "1d"], "5m",
                         id="explore-interval", clearable=False, style={"width": "100px"}),
            html.Span("Count per instrument"),
            dcc.Input(id="explore-count", type="number", value=50, min=1, max=1000, step=1),
            dcc.Input(id="explore-end", placeholder="End UTC, blank = latest"),
            html.Small("Latest fairvalue ignores these controls. VAP has its own panel above."),
        ], style=ROW),
        html.Details([html.Summary("TAS query window (required for TAS)"), html.Div([
            dcc.Input(id="explore-dates", value=(pd.Timestamp.now(tz="UTC").date() - timedelta(days=1)).isoformat(),
                      placeholder="YYYY-MM-DD,YYYY-MM-DD", style={"width": "260px"}),
            dcc.Input(id="explore-start-time", value="10:00:00", placeholder="Start HH:MM:SS"),
            dcc.Input(id="explore-end-time", value="11:00:00", placeholder="End HH:MM:SS"),
            html.Small("TAS uses the API's time-zone convention; this input zone is not yet confirmed."),
        ], style=ROW)]),
        dcc.Loading(html.Div(id="explore-output", children="No endpoint requested yet.")),
    ], style={"borderTop": "1px solid #ddd", "marginTop": "25px", "paddingTop": "10px"})


def explorer_figure(frame, endpoint):
    figure = go.Figure()
    if not frame.empty:
        if endpoint == "fairvalue":
            figure.add_trace(go.Bar(x=frame.instrument, y=frame.price, name="Latest fairvalue"))
        else:
            for instrument, rows in frame.groupby("instrument", sort=False):
                rows = rows.sort_values("timestamp")
                figure.add_trace(go.Scatter(x=rows.timestamp.dt.strftime("%Y-%m-%d %H:%M:%S.%f"),
                    y=rows["close" if endpoint == "fairvalue_historical" else "price"],
                    mode="lines+markers" if endpoint == "fairvalue_historical" else "markers", name=instrument))
    figure.update_layout(title=ENDPOINTS[endpoint], template="plotly_white", height=380, barmode="group",
                         margin=dict(l=50, r=15, t=50, b=70))
    figure.update_xaxes(type="category", categoryorder="category ascending",
                       title_text="Instrument" if endpoint == "fairvalue" else "UTC")
    return figure


def register_explorer(app):
    @app.callback(Output("explore-output", "children"), Input("explore-fetch", "n_clicks"),
        *[State(name, "value") for name in ("explore-endpoint", "explore-symbols", "explore-interval",
          "explore-count", "explore-end", "explore-dates", "explore-start-time", "explore-end-time",
          "explore-fresh", "contract", "custom")], prevent_initial_call=True)
    def fetch(_, endpoint, symbols, interval, count, end_text, dates, start_time, end_time, fresh, selected, custom):
        try:
            codes = [code.strip().upper() for code in (symbols or custom or selected or "").split(",")]
            if not all(codes) or len(codes) > 50:
                raise ValueError("Provide 1 to 50 instrument codes")
            if endpoint not in ENDPOINTS:
                raise ValueError("Select an endpoint")
            if endpoint == "fairvalue_historical" and (count is None or int(count) != count or not 1 <= count <= 1000):
                raise ValueError("Count must be an integer from 1 to 1000")
            end = None
            if end_text and endpoint == "fairvalue_historical":
                stamp = pd.Timestamp(end_text)
                stamp = stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")
                end = int(stamp.timestamp())
            products = []
            if endpoint == "tas":
                days = [date.fromisoformat(day.strip()).isoformat() for day in (dates or "").split(",")]
                first, last = time.fromisoformat(start_time), time.fromisoformat(end_time)
                if first.tzinfo or last.tzinfo or first > last:
                    raise ValueError("TAS start/end must be local clock times in ascending order")
                products = [{"id": code, "dates": days, "start": first.isoformat(), "end": last.isoformat()} for code in codes]
            with QHClient(cache_ttl=0 if "fresh" in (fresh or []) else 60) as client:
                if endpoint == "fairvalue":
                    result = fairvalue.fetch(client, codes)
                elif endpoint == "fairvalue_historical":
                    result = fairvalue_historical.fetch(client, codes, timeinterval=interval, count=int(count),
                                                       end=end, fields=["open", "high", "low"])
                else:
                    result = tas.fetch(client, products)
                info = dict(client.last_response)
            frame = result.to_frame()
            table = frame.head(1000).copy()
            for column in table.select_dtypes(include=["datetimetz", "datetime"]).columns:
                table[column] = table[column].astype(str)
            for column in table.select_dtypes(include="object").columns:
                table[column] = table[column].map(lambda value: json.dumps(value) if isinstance(value, (dict, list)) else value)
            data = json.loads(table.to_json(orient="records"))
            summary = f"{ENDPOINTS[endpoint]}: {len(frame)} rows; {'cached' if info.get('cached') else 'fetched'}; displaying {len(table)} rows."
            if endpoint == "tas":
                summary += f" Page {result.metadata.get('page', '?')} of {result.metadata.get('total_pages', '?')}."
                if result.has_more:
                    summary += " More pages exist; this view contains only the returned page."
            return [html.P(summary if not frame.empty else summary + " No data returned for these inputs."),
                dcc.Graph(figure=explorer_figure(frame, endpoint)),
                dash_table.DataTable(data=data, columns=[{"name": col, "id": col} for col in table.columns],
                    page_size=20, sort_action="native", filter_action="native", style_table={"overflowX": "auto"},
                    style_cell={"textAlign": "left", "fontSize": 12, "maxWidth": "350px", "overflow": "hidden"}),
                html.Details([html.Summary("Response details and raw sample"), html.Pre(json.dumps({
                    "request": info, "metadata": safe_preview(result.metadata), "raw_sample": safe_preview(result.raw)},
                    indent=2, default=str)[:16000], style={"whiteSpace": "pre-wrap"})])]
        except (QHAPIError, ValueError, TypeError, KeyError, OSError) as error:
            return html.P(str(error), style={"color": "#b91c1c"})

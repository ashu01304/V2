"""Dedicated VAP views; changing the display never issues an API request."""

import json

import pandas as pd
import plotly.graph_objects as go
from dash import Input, Output, State, dash_table, dcc, html

from . import QHClient, QHAPIError
from .endpoints import vap
from .probe import safe_preview

ROW = {"display": "flex", "flexWrap": "wrap", "gap": "12px", "alignItems": "center", "margin": "14px 0"}
METRICS = {"combined": "Combined volume (1 + 2)", "volume_1": "Volume 1", "volume_2": "Volume 2"}


def vap_layout():
    return html.Section([
        html.H3("Volume at price", style={"marginBottom": "6px"}),
        html.Div("Explore the price ladder and how volume changes across time. Blue = volume 1; orange = volume 2."),
        html.Small("The API's buy/sell mapping is unconfirmed, so these labels preserve its original order."),
        html.Div([
            dcc.Input(id="vap-symbols", placeholder="Instruments, blank = chart selection", style={"width": "310px"}),
            dcc.Dropdown([{"label": "5 minutes", "value": "5M"}, {"label": "Daily", "value": "1D"}],
                         "5M", id="vap-interval", clearable=False, style={"width": "140px"}),
            html.Label(["Buckets per instrument ", dcc.Input(id="vap-count", type="number", value=24,
                        min=1, max=200, step=1, style={"width": "75px"})]),
            dcc.Input(id="vap-end", placeholder="End UTC, blank = latest"),
            html.Button("Load VAP", id="vap-fetch", n_clicks=0),
            dcc.Checklist([{"label": "Fresh request", "value": "fresh"}], [], id="vap-fresh"),
        ], style=ROW),
        dcc.Loading(html.Div(id="vap-status", children="Load data to start. Responses are cached for 60 seconds.")),
        dcc.Store(id="vap-data", storage_type="memory"),
        html.Div([
            html.Label("Instrument"),
            dcc.Dropdown(id="vap-instrument", options=[], clearable=False, style={"minWidth": "240px"}),
            html.Label("Profile window"),
            dcc.Dropdown(id="vap-bucket", options=[], clearable=False, style={"minWidth": "250px"}),
            html.Label("Heatmap"),
            dcc.Dropdown([{"label": label, "value": field} for field, label in METRICS.items()],
                         "combined", id="vap-metric", clearable=False, style={"minWidth": "230px"}),
        ], style=ROW),
        dcc.Loading(html.Div(id="vap-view")),
    ], style={"background": "#f8fafc", "border": "1px solid #dbe3ec", "borderRadius": "12px",
              "padding": "22px", "margin": "28px 0"})


def profile_figure(profile, instrument, bucket):
    figure = go.Figure()
    for field, label, color in (("volume_1", "Volume 1", "#2563eb"), ("volume_2", "Volume 2", "#f97316")):
        figure.add_trace(go.Bar(x=profile[field], y=profile.price, orientation="h", name=label,
            marker_color=color, hovertemplate="Price %{y}<br>" + label + ": %{x}<extra></extra>"))
    peak = profile.loc[profile.combined.idxmax()]
    if peak.combined > 0:
        figure.add_hline(y=float(peak.price), line_dash="dot", line_color="#475569",
                        annotation_text=f"Peak combined volume @ {peak.price:g}")
    figure.update_layout(title=f"{instrument} - {'All loaded buckets' if bucket == 'all' else bucket + ' UTC'}",
        template="plotly_white", barmode="group", height=490, margin=dict(l=65, r=25, t=65, b=45),
        legend=dict(orientation="h", y=1.05), xaxis_title="Volume", yaxis_title="Price")
    return figure


def heatmap_figure(frame, metric, instrument):
    matrix = frame.pivot_table(index="price", columns="timestamp", values=metric, aggfunc="sum").sort_index()
    stamps = matrix.columns.strftime("%Y-%m-%d %H:%M:%S").tolist()
    ticks = stamps[::max(1, (len(stamps) + 6) // 7)]
    figure = go.Figure(go.Heatmap(x=stamps, y=matrix.index.tolist(), z=matrix.to_numpy().tolist(),
        colorscale="Blues", zmin=0, colorbar=dict(title="Volume"), hoverongaps=False,
        hovertemplate="%{x} UTC<br>Price %{y}<br>" + METRICS[metric] + ": %{z}<extra></extra>"))
    figure.update_layout(title=f"{instrument} - {METRICS[metric]} across all loaded buckets",
        template="plotly_white", height=490, margin=dict(l=65, r=25, t=65, b=70), yaxis_title="Price")
    figure.update_xaxes(type="category", categoryorder="array", categoryarray=stamps,
        tickmode="array", tickvals=ticks, ticktext=[stamp[:16].replace(" ", "<br>") for stamp in ticks],
        title_text="UTC (non-trading gaps removed)")
    return figure


def register_vap(app):
    @app.callback(Output("vap-data", "data"), Output("vap-instrument", "options"),
        Output("vap-instrument", "value"), Output("vap-status", "children"), Input("vap-fetch", "n_clicks"),
        *[State(name, "value") for name in ("vap-symbols", "vap-interval", "vap-count", "vap-end",
                                            "vap-fresh", "contract", "custom")], prevent_initial_call=True)
    def fetch(_, symbols, interval, count, end_text, fresh, selected, custom):
        try:
            codes = list(dict.fromkeys(code.strip().upper() for code in (symbols or custom or selected or "").split(",")))
            if not all(codes) or len(codes) > 50:
                raise ValueError("Provide 1 to 50 instrument codes")
            if count is None or int(count) != count or not 1 <= count <= 200:
                raise ValueError("Choose 1 to 200 buckets per instrument")
            end = None
            if end_text:
                stamp = pd.Timestamp(end_text)
                stamp = stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")
                end = int(stamp.timestamp())
            with QHClient(cache_ttl=0 if "fresh" in (fresh or []) else 60) as client:
                result = vap.fetch(client, codes, interval=interval, count=int(count), end=end)
                records = result.levels
                source = "cached" if client.last_response.get("cached") else "fetched"
            instruments = sorted({row["instrument"] for row in records})
            payload = {"records": records, "raw_sample": safe_preview(result.raw)}
            missing = [code for code in codes if code not in instruments]
            note = f"{source.capitalize()}: {len(records):,} price-level rows across {len(instruments)} instruments."
            if missing:
                note += " No price levels returned for: " + ", ".join(missing)
            return payload, instruments, instruments[0] if instruments else None, note
        except (QHAPIError, ValueError, TypeError, KeyError, OSError) as error:
            return None, [], None, html.Span(str(error), style={"color": "#b91c1c"})

    @app.callback(Output("vap-bucket", "options"), Output("vap-bucket", "value"),
                  Input("vap-data", "data"), Input("vap-instrument", "value"))
    def buckets(payload, instrument):
        stamps = sorted({row["timestamp"] for row in (payload or {}).get("records", [])
                         if row["instrument"] == instrument}, reverse=True)
        options = [{"label": "All loaded buckets (aggregate)", "value": "all"}]
        options += [{"label": pd.Timestamp(stamp, unit="ms", tz="UTC").strftime("%Y-%m-%d %H:%M UTC"),
                     "value": str(stamp)} for stamp in stamps]
        return (options, str(stamps[0])) if stamps else ([], None)

    @app.callback(Output("vap-view", "children"), Input("vap-data", "data"), Input("vap-instrument", "value"),
                  Input("vap-bucket", "value"), Input("vap-metric", "value"))
    def render(payload, instrument, bucket, metric):
        if not payload or not payload.get("records") or not instrument or bucket is None:
            return html.P("Choose instruments and load VAP to see the profile and heatmap.")
        frame = pd.DataFrame(payload["records"])
        frame = frame[frame.instrument == instrument].copy()
        if frame.empty or metric not in METRICS:
            return html.P("No data for this selection.")
        selected = frame if bucket == "all" else frame[frame.timestamp.astype(str) == bucket]
        if selected.empty:
            return html.P("Select a bucket from the loaded data.")
        profile = selected.groupby("price", as_index=False)[["volume_1", "volume_2"]].sum().sort_values("price")
        profile["combined"] = profile.volume_1 + profile.volume_2
        frame["combined"] = frame.volume_1 + frame.volume_2
        frame["timestamp"] = pd.to_datetime(frame.timestamp, unit="ms", utc=True)
        window = "all" if bucket == "all" else pd.Timestamp(int(bucket), unit="ms", tz="UTC").strftime("%Y-%m-%d %H:%M")
        peak = profile.loc[profile.combined.idxmax()]
        cards = [("Profile buckets", selected.timestamp.nunique()), ("Price levels", len(profile)),
                 ("Volume 1", int(profile.volume_1.sum())), ("Volume 2", int(profile.volume_2.sum())),
                 ("Peak-volume price", f"{peak.price:g}" if peak.combined > 0 else "No volume")]
        return [html.Div([html.Div([html.Small(label), html.Div(str(value), style={"fontSize": "23px", "fontWeight": "600"})],
                          style={"background": "white", "padding": "12px 20px", "borderRadius": "8px"})
                          for label, value in cards], style=ROW),
            html.Div([dcc.Graph(figure=profile_figure(profile, instrument, window)),
                      dcc.Graph(figure=heatmap_figure(frame, metric, instrument))],
                      style={"display": "grid", "gridTemplateColumns": "repeat(auto-fit, minmax(min(100%, 440px), 1fr))", "gap": "16px"}),
            html.Details([html.Summary(f"Price ladder - {len(profile)} levels"),
                dash_table.DataTable(data=profile.sort_values("price", ascending=False).to_dict("records"),
                    columns=[{"name": label, "id": field, "type": "numeric"} for field, label in
                             (("price", "Price"), ("volume_1", "Volume 1"), ("volume_2", "Volume 2"), ("combined", "Combined"))],
                    page_size=15, sort_action="native", style_table={"overflowX": "auto"})]),
            html.Details([html.Summary("Raw response sample"), html.Pre(json.dumps(payload["raw_sample"], indent=2),
                         style={"whiteSpace": "pre-wrap", "maxHeight": "300px", "overflowY": "auto"})])]

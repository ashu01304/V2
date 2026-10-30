"""Independent SEAC experiment with a fixed today-relative calendar-day window."""

from functools import lru_cache
from math import isfinite
from html import escape
import logging
from uuid import uuid4

import duckdb
import pandas as pd
import plotly.graph_objects as go
from dash import ALL, Input, Output, State, ctx, dcc, html, no_update, set_props
from dash.exceptions import PreventUpdate

from analysis.contract_universe import load_expiries
from analysis.expression import format_contract_label, parse_expression
from analysis.expiry import OfficialExpiryLookup
from analysis.plotting import SeasonalityPlotter
from analysis.seasonality import Seasonality
from market_data import MarketData
from market_data.config import LIVE_PRODUCTS
from market_data.queries import database_signature
from market_data.workflow_client import WorkflowLiveClient, full_structure

def fetch_live_quote(product, expression):
    client=WorkflowLiveClient('seac-experiment')
    try:
        client.replace([full_structure(product,expression)])
        return client.expression(product,expression,include_alternatives=True)
    except Exception as error:
        logging.getLogger(__name__).exception('8057 live quote request failed')
        return dict(error=f'{type(error).__name__}: unable to read the live manager; see terminal details')
    finally:
        client.close()

MONTHS = "FGHJKMNQUVXZ"
PRODUCTS = (*LIVE_PRODUCTS, "CL-CO")
PRESETS = {"spread": (1, -1), "fly": (1, -2, 1), "defly": (1, -3, 3, -1), "outright": (1,)}
STRUCTURE_NAMES = {"outright": "Outright", "spread": "Spread", "fly": "Fly", "defly": "Defly"}


def selection(value):
    return value if isinstance(value, dict) else {"count": value, "structure": "outright"}


def month_cells(row):
    return [html.Div([
                        html.Button(code, id={"type": "seac-month-clear", "code": code}, n_clicks=0,
                                    className="seac-month-label", title=f"Clear {code}"),
                        html.Button(html.Strong(
                        f"{selection(value)['count']:g} {STRUCTURE_NAMES[selection(value)['structure']]}"
                        if selection(value)["count"] else "0"),
                        id={"type": "seac-month", "code": code}, n_clicks=0,
                        draggable="true" if selection(value)["count"] else "false",
                        className="seac-month-value", title=f"Edit {code}: count and structure")],
                        className="seac-month-cell")
            for code, value in row.items()]


def apply_structure(row, code, count, structure):
    if count is None or not isfinite(float(count)) or float(count) != int(count):
        raise ValueError("Count must be a whole number")
    if structure not in PRESETS:
        raise ValueError("Choose a structure")
    if code not in row:
        raise ValueError("Select a month")
    updated = dict(row)
    updated[code] = {"count": int(count), "structure": structure}
    return updated


def today():
    return pd.Timestamp.now(tz="Europe/London").tz_localize(None).normalize()


def contract_chain(product, count=48, skip=0):
    calendars = [dict(load_expiries(symbol)) for symbol in (("CL", "CO") if product == "CL-CO" else (product,))]
    active = [code for code in set.intersection(*(set(items) for items in calendars))
              if min(items[code] for items in calendars) >= today().date()]
    if not active:
        raise ValueError("No unexpired contracts found in the expiry calendar")
    first = min(active, key=lambda code: (int(code[1:]), MONTHS.index(code[0])))
    start = pd.Timestamp(2000 + int(first[1:]), MONTHS.index(first[0]) + 1, 1) + pd.DateOffset(months=skip)
    return [f"{MONTHS[stamp.month-1]}{stamp.year % 100:02d}" for stamp in pd.date_range(start, periods=count, freq="MS")]


def format_legs(legs):
    # Earliest delivery anchors expiry alignment, regardless of the entered order.
    totals = {}
    for weight, code in legs:
        if not isfinite(float(weight)):
            raise ValueError("Contract weights must be finite numbers")
        totals[code] = totals.get(code, 0) + float(weight)
    if not all(isfinite(weight) for weight in totals.values()):
        raise ValueError("Combined contract weights must be finite")
    items = sorted(((code, weight) for code, weight in totals.items() if weight),
                   key=lambda item: (int(item[0][1:]), MONTHS.index(item[0][0])))
    if not items:
        raise ValueError("Enter at least one nonzero contract weight")
    return "".join(("-" if weight < 0 else "+" if index else "")
                   + (f"{abs(weight):g}*" if abs(weight) != 1 else "") + code
                   for index, (code, weight) in enumerate(items))


def expression_from_weights(rows):
    legs = []
    for code, value in (rows[0] if rows else {}).items():
        item = selection(value)
        start = (2000 + int(code[1:])) * 12 + MONTHS.index(code[0])
        for offset, weight in enumerate(PRESETS[item["structure"]]):
            year, month = divmod(start + offset, 12)
            legs.append((float(item["count"] or 0) * weight, f"{MONTHS[month]}{year % 100:02d}"))
    return format_legs(legs)


def latest_years(count, expression):
    # Match the seasonality engine: the first net leg anchors the contract year.
    end = 2000 + int(parse_expression(expression)[0][1][1:])
    maximum = end - 1999
    if count is None or not isfinite(float(count)) or float(count) != int(count) or not 1 <= int(count) <= maximum:
        raise ValueError(f"Years must be a whole number from 1 to {maximum}")
    return set(range(end - int(count) + 1, end + 1))


@lru_cache(maxsize=6)
def history(product, expression, start_year, end_year, window, signature):
    reader = MarketData()
    engine = None
    try:
        engine = Seasonality(reader, include_live=False)
        result = engine.working_day_expression_seasonality(product, expression, start_year, end_year, window)
        month = parse_expression(expression)[0][1][0]
        expiries = {}
        for year in result.get("series", {}):
            code = f"{month}{year % 100:02d}"
            anchor_history = reader.get_contract_history(product, [code]).get(code)
            if anchor_history is not None and not anchor_history.empty:
                expiries[year] = engine.official_or_last_date(product, code, anchor_history)
        return {**result, "expiries": expiries}
    finally:
        if engine is not None:
            engine.close()
        reader.close()


def align_to_today(result, product, expression, reference_day):
    """Preserve expiry alignment, with the selected contract's today at zero."""
    anchor = parse_expression(expression)[0][1]
    symbol = "CL" if product == "CL-CO" else product
    lookup = OfficialExpiryLookup()
    selected_expiry = lookup.get(symbol, anchor)
    if selected_expiry is None:
        raise ValueError("An official anchor expiry is required for the today-relative experiment")
    distance = (pd.Timestamp(selected_expiry) - reference_day).days
    series = {}
    warnings = list(result.get("warnings", []))
    for year, frame in result.get("series", {}).items():
        expiry = lookup.get(symbol, f"{anchor[0]}{year % 100:02d}")
        if expiry is None:
            expiry = result.get("expiries", {}).get(year)
        if expiry is None:
            warnings.append(f"{year}: official expiry unavailable; skipped")
            continue
        frame = frame.copy()
        frame.index = (pd.to_datetime(frame["date"]).dt.normalize() - pd.Timestamp(expiry)).dt.days
        series[year] = frame
    return {**result, "series": series, "warnings": warnings, "window_bounds": [-distance - 90, -distance + 90]}


def prepare_result(result, requested, exclude, window, view, flags):
    selected = (set(result.get("series", {})) & requested) - set(exclude or [])
    low, high = result["window_bounds"]
    series = {}
    for year in sorted(selected):
        frame = result["series"][year].copy()
        frame = frame.loc[(frame.index >= low) & (frame.index <= high)]
        frame = frame.loc[~frame.index.duplicated(keep="last")].sort_index()
        if view == "change":
            frame["value"] = frame.value.diff()
        frame = frame.dropna(subset=["value"])
        if frame.empty:
            continue
        if "zero" in flags:
            frame["value"] -= frame.value.iloc[0]
        series[year] = frame
    if not series:
        raise ValueError("No matching SEAC history for these legs, years and window")
    combined = pd.concat([frame.value.rename(year) for year, frame in series.items()], axis=1).sort_index()
    return {"series": series, "combined": combined, "warnings": [],
            "average": combined.mean(axis=1) if "average" in flags and len(series) > 1 else None,
            "xaxis_title": "Days to expiry", "window_bounds": [low, high]}


def empty_figure(message="Set contract weights and press Plot"):
    figure = go.Figure()
    figure.update_layout(template="plotly_dark", paper_bgcolor="black", plot_bgcolor="black", height=650,
                         margin=dict(l=55, r=20, t=40, b=45), xaxis=dict(visible=False), yaxis=dict(visible=False))
    figure.add_annotation(text=message, x=.5, y=.5, xref="paper", yref="paper", showarrow=False)
    return figure


def rolling_statistics(source, index, days):
    means, deviations = [], []
    for day in index:
        values = source.loc[(source.index > day - days) & (source.index <= day)].to_numpy().ravel()
        values = pd.Series(values).dropna()
        means.append(values.mean())
        deviations.append(values.std(ddof=1))
    return pd.Series(means, index=index), pd.Series(deviations, index=index)


def statistics_source(base, result, flags):
    columns = []
    low, high = result["window_bounds"]
    for year, displayed in result["series"].items():
        frame = base["series"][year]
        values = frame.loc[~frame.index.duplicated(keep="last"), "value"].sort_index()
        if "zero" in flags:
            values = values - values.loc[displayed.index[0]]
        columns.append(values.loc[(values.index >= low - 365) & (values.index <= high)].rename(year))
    return pd.concat(columns, axis=1).sort_index()


def add_statistics(figure, combined, visible, multiplier, days, source,
                   current_year=None, current_index=None, current_multiplier=2):
    other_source = source.drop(columns=[current_year], errors="ignore")
    mean, deviation = rolling_statistics(other_source, combined.index, days)
    traces = []
    for index, (name, values, color, dash, show) in enumerate((
        ("Other years rolling mean", mean, "#facc15", "solid", "mean" in visible),
        (f"Other years mean + {multiplier:g} SD", mean + multiplier * deviation, "#38bdf8", "dash", "sd" in visible),
        (f"Other years mean − {multiplier:g} SD", mean - multiplier * deviation, "#38bdf8", "dash", "sd" in visible),
    )):
        traces.append(len(figure.data))
        figure.add_trace(go.Scatter(x=combined.index.tolist(), y=values.tolist(), name=name,
            mode="lines", visible=show, connectgaps=False, line=dict(color=color, dash="solid", width=2 if index == 0 else 0),
            fill="tonexty" if index == 2 else None, fillcolor="rgba(56, 189, 248, 0.24)",
            showlegend=index != 1, legendgroup="sd-band" if index else "rolling-mean",
            hovertemplate="%{fullData.name}<br>Days to expiry: %{x}<br>Value: %{y}<extra></extra>"))
    figure.data[traces[2]].name = f"Other years mean ± {multiplier:g} SD"
    current_x = list(current_index) if current_index is not None else []
    current_source = source[[current_year]] if current_year in source.columns else source.iloc[:, :0]
    current_mean, current_sd = rolling_statistics(current_source, current_x, days)
    current_traces = []
    for index, values in enumerate((current_mean + current_multiplier * current_sd,
                                    current_mean - current_multiplier * current_sd)):
        current_traces.append(len(figure.data))
        figure.add_trace(go.Scatter(x=current_x, y=values.tolist(), mode="lines",
            name=f"Latest {current_year} mean ± {current_multiplier:g} SD",
            line=dict(width=0), fill="tonexty" if index else None,
            fillcolor="rgba(251, 146, 60, 0.25)", visible="current" in visible,
            showlegend=bool(index), legendgroup="current-sd-band", connectgaps=False))
    def add_zone(name,group,hidden=False,legend=True):
        indices=[]
        for i in range(2):
            indices.append(len(figure.data))
            figure.add_trace(go.Scatter(x=[],y=[],mode='lines',name=name,
                line=dict(color='#a78bfa' if not hidden else '#a3a3a3',width=1,dash='dot'),
                fill='tonexty' if i else None,fillcolor='rgba(167,139,250,.12)' if not hidden else 'rgba(163,163,163,.04)',
                showlegend=bool(i) and legend,legendgroup=group,visible='legendonly' if hidden else True,
                hovertemplate=name+'<br>Boundary: %{y}<extra></extra>'))
        return indices
    zone_traces=add_zone(f'Rolling historical P5–P95 (excludes {current_year})','containment-zone')
    older_columns=[(i,year) for i,year in enumerate(source.columns) if year!=current_year]
    fold_zones=[dict(column=i,year=int(year),traces=add_zone(f'Held-out {year} rolling reference','held-out-zones',True,j==0))
                for j,(i,year) in enumerate(older_columns)]
    return {"x": combined.index.tolist(), "sourceX": source.index.tolist(),
            "sourceValues": [[float(v) if pd.notna(v) else None for v in row] for row in source.to_numpy()], "traces": traces,
            "currentX": current_x, "currentYear": current_year, "currentTraces": current_traces,
            "currentColumn": int(source.columns.get_loc(current_year)) if current_year in source.columns else -1,
            "zoneTraces": zone_traces, "foldZones": fold_zones,
            "currentValues": [[float(v) if pd.notna(v) else None for v in row] for row in current_source.to_numpy()]}


def live_coordinates(config, quote):
    """Map a fresh quote to the plotted expiry axis and price transformation."""
    if not config or not quote:
        return [], []
    try:
        timestamp = pd.Timestamp(quote["timestamp"])
        timestamp = timestamp.tz_localize("UTC") if timestamp.tzinfo is None else timestamp.tz_convert("UTC")
        age = (pd.Timestamp.now(tz="UTC") - timestamp).total_seconds()
        value = float(quote["value"])
        if not isfinite(value) or value == -100 or not -5 <= age <= 120:
            return [], []
        day = timestamp.tz_localize(None).normalize()
        expiry = pd.Timestamp(config["expiry"])
        offset = (day - expiry).days
        if day > expiry or not -90 <= (day - pd.Timestamp(config["reference_day"])).days <= 90:
            return [], []
        points = config["points"]
        if config["view"] == "change":
            earlier = [point for point in points if pd.Timestamp(point[0]) < day]
            if not earlier:
                return [], []
            value -= earlier[-1][1]
        if "zero" in config["flags"]:
            baseline = points[0][1]
            if config["view"] == "change":
                if len(points) < 2:
                    return [], []
                baseline = points[1][1] - points[0][1]
            value -= baseline
        return [offset], [value]
    except (KeyError, ValueError, TypeError, OverflowError):
        return [], []


def side_point(config, quote, side):
    """A narrow price band across fresh, available constructions for this side."""
    decimals = {"HO": 4, "NG": 3, "RB": 4}.get((config or {}).get("product"), 2)
    candidates=[]
    for alternative in (quote or {}).get('alternatives') or []:
        x,y=live_coordinates(config,dict(value=alternative.get(side),
            timestamp=alternative.get(side+'_timestamp') or alternative.get('timestamp')))
        if x:
            candidates.append((x[0],y[0],alternative))
    if not candidates:
        return dict(x=[],y=[],text=[],hover=[])
    day=max(item[0] for item in candidates)
    candidates=[item for item in candidates if item[0]==day]
    low,high=min(item[1] for item in candidates),max(item[1] for item in candidates)
    prices=[item[2][side] for item in candidates]
    label=f"{side.title()} {min(prices):.{decimals}f} – {max(prices):.{decimals}f}"
    details=[]
    for _,_,alternative in candidates[:12]:
        components='; '.join(f"{part['weight']:+g} × {escape(str(part.get('structure') or part['tt_id']))}"
                            for part in alternative.get('components',[]))
        details.append(f"{escape(alternative['construction'])}: {alternative[side]:.{decimals}f}<br>{components}")
    if len(candidates)>12:
        details.append(f"{len(candidates)-12} more constructions available from the manager")
    if (quote or {}).get('alternatives_truncated'):
        details.append('Construction search capped; this range is not exhaustive')
    hover=f"{label}<br>{len(candidates)} available constructions<br>"+'<br>'.join(details)
    center=day+(-.65 if side=='bid' else .65)
    return dict(x=[center-.3,center+.3,center+.3,center-.3,center-.3],
                y=[low,low,high,high,low],text=['','',label,'',''],hover=[hover]*5)


def live_status(config, quote):
    """Explain hidden points instead of silently leaving an empty live overlay."""
    if not config:
        return 'Live paused: the selected contract year is excluded or has no usable history/expiry in this window.'
    if not quote:
        return 'Live unavailable: check that the shared manager is running on 8071.'
    if quote.get('error'):
        return 'Live unavailable: '+quote['error']
    if all(quote.get(field) is None for field in ('value','bid','ask')) and not quote.get('alternatives'):
        return 'Live pending: '+(quote.get('message') or 'waiting for fresh subscribed quotes from 8071')
    try:
        stamp=pd.Timestamp(quote.get('timestamp'))
        stamp=stamp.tz_localize('UTC') if stamp.tzinfo is None else stamp.tz_convert('UTC')
        age=(pd.Timestamp.now(tz='UTC')-stamp).total_seconds()
        if pd.notna(stamp) and age>120 and not quote.get('alternatives'):
            return f'Live hidden: quote is {age:.0f}s old; maximum age is 120s.'
    except (ValueError,TypeError,OverflowError):
        pass
    x,_=live_coordinates(config,quote)
    bid,ask=side_point(config,quote,'bid'),side_point(config,quote,'ask')
    if not x and not bid['x'] and not ask['x']:
        return 'Live hidden: no fresh price inside the selected expiry window. '+(quote.get('message') or '')
    decimals={"HO":4,"NG":3,"RB":4}.get(config['product'],2)
    value=quote.get('value')
    reference=f'{value:.{decimals}f}' if value is not None else 'pending'
    return (f"Live · {config['product']} {config['expression']} · reference {reference} · "
            f"{bid['text'][2] if bid['text'] else 'Bid range unavailable'} · "
            f"{ask['text'][2] if ask['text'] else 'Ask range unavailable'}"+
            (' · alternatives not supplied: restart the live manager' if quote.get('alternatives') is None else ''))


def live_config(base, result, product, expression, window, view, flags):
    anchor = parse_expression(expression)[0][1]
    year = 2000 + int(anchor[1:])
    expiry = OfficialExpiryLookup().get("CL" if product == "CL-CO" else product, anchor)
    if year not in result["series"] or expiry is None:
        return None
    frame = base["series"][year].sort_index()
    low, high = result["window_bounds"]
    frame = frame.loc[(frame.index >= low) & (frame.index <= high)]
    frame = frame.loc[~frame.index.duplicated(keep="last")].dropna(subset=["value"])
    if frame.empty:
        return None
    return {"product": product, "expression": expression, "expiry": str(expiry),
            "window": 90, "view": view, "flags": flags, "reference_day": today().isoformat(),
            "points": [[pd.Timestamp(row.date).isoformat(), float(row.value)] for row in frame.itertuples()]}


def builder_layout():
    codes = contract_chain("CL")
    weights = {code: {"count": 0, "structure": "outright"} for code in codes}
    return html.Div([
        html.Div([
            html.Label([html.Span("Years"), dcc.Input(id="seac-years", type="number",
                min=1, max=100, step=1, value=8, debounce=True)],
                title="Latest N contract years, ending with the selected expression's anchor year"),
            html.Details([html.Summary("Exclude years"),
                dcc.Checklist(options=[],
                    value=[], id="seac-exclude", className="seac-exclude-options")], className="seac-exclude-menu"),
            dcc.Checklist([{"label": "Other mean", "value": "mean"}, {"label": "Other SD", "value": "sd"},
                {"label": "Latest SD", "value": "current"}],
                ["mean", "sd", "current"], id="seac-stat-visible", inline=True),
            html.Label([html.Span("Roll days"), dcc.Input(id="seac-rolling-days", type="number", min=1, max=365,
                step=1, value=20, debounce=True, style={"width": "50px"})]),
            html.Label([html.Span("Other SD ×"), dcc.Input(id="seac-sd-multiplier", type="number", min=0, step=.25,
                value=2, debounce=True, style={"width": "50px"})]),
            html.Label([html.Span("Latest SD ×"), dcc.Input(id="seac-current-sd", type="number", min=0, step=.25,
                value=2, debounce=True, style={"width": "50px"})]),
            dcc.Checklist([{"label": "Zero base", "value": "zero"}], [], id="seac-flags", inline=True),
            html.Label([html.Span("Product"), dcc.Dropdown(list(PRODUCTS), "CL", id="seac-product", clearable=False)]),
            html.Span("Experiment · Today −90 to +90 calendar days"),
            dcc.Input(id="seac-window", type="number", value=90, style={"display": "none"}),
            html.Button("Plot", id="seac-plot", n_clicks=0, className="seac-plot-button"),
        ], className="seac-toolbar seac-controls seac-control-bar"),
        html.Div([
            dcc.Store(id="seac-weights", data=[weights]),
            html.Div(month_cells(weights), id="seac-months", className="seac-months"),
            dcc.Store(id="seac-selected-month"),
            dcc.Store(id="seac-month-move"),
            dcc.Store(id="seac-structure-wheel"),
            html.Div([
                html.Strong(id="seac-editor-title"),
                html.Label(["Count", dcc.Input(id="seac-leg-count", type="number", value=1, step=1)]),
                html.Label(["St.", dcc.Dropdown([
                    {"label": "Outright", "value": "outright"},
                    {"label": "Spread", "value": "spread"},
                    {"label": "Fly", "value": "fly"},
                    {"label": "Defly", "value": "defly"}], "outright", id="seac-structure", clearable=False, searchable=False)]),
                html.Button("×", id="seac-close-month", n_clicks=0, title="Close", **{"aria-label": "Close month editor"}),
                html.Span(id="seac-editor-error", className="seac-error"),
            ], id="seac-month-editor", className="seac-month-editor", style={"display": "none"}),
            html.Div(id="seac-chain-note", className="seac-chain-note"),
        ], id="seac-chain"),
        html.Div(id="seac-expression", className="seac-expression"),
        dcc.Store(id="seac-live-config"),
        dcc.Store(id="seac-statistics"),
        dcc.Store(id="seac-stat-rendered"),
        dcc.Store(id="seac-live-point"),
        dcc.Store(id="seac-live-rendered"),
        dcc.Interval(id="seac-live-tick", interval=10_000),
        html.Div('Live: select a structure and press Plot.',id='seac-live-status',
            style={'color':'#d1d5db','fontSize':'12px','padding':'6px 0'}),
        html.Div([html.Div([html.Div(label,style={'color':'#aaa','fontSize':'12px'}),
                    html.Strong('N/A',id='seac-score-'+key,style={'fontSize':'20px'})],
                    style={'border':'1px solid #333','padding':'9px 12px','minWidth':'185px'})
                  for key,label in [('history-zone','Historical containment'),('latest-zone','Latest-year containment'),
                                    ('history-stability','Historical stability'),('latest-stability','Latest-year conformity')]],
                 style={'display':'flex','flexWrap':'wrap','gap':'8px','margin':'8px 0'}),
        html.Details([html.Summary('Score definitions and sample coverage'),
            html.Div(id='seac-score-details',children='Plot a structure to calculate scores.'),
            html.P('Containment uses rolling historical 5th–95th percentile bands at each expiry position, controlled by Roll days. '
                   'Reference observations are pooled from (position − N days, position], with earlier history warming up the visible window. '
                   'Historical coverage holds out each older year in turn; the latest year never enters the reference. '
                   'Historical stability = 100 / (1 + span of rolling means / historical SD + span of rolling SDs / historical SD). '
                   'Latest-year conformity averages 100 / (1 + absolute rolling-mean displacement in historical SD units '
                   '+ absolute log of the latest/historical rolling-SD ratio) over matching dates. '
                   'These stability formulas are diagnostic scoring choices, not probabilities. '
                   'Scores require at least 3 older years and 20 historical observations; held-out and latest-year coverage '
                   'require 5 observations, historical containment requires 3 valid folds, and rolling scores require 5 comparable dates. '
                   'Each percentile window requires 20 observations from at least 2 reference years; positions without sufficient data are not scored. '
                   'A wide zone can yield high containment, so check its width. Purple shows the rolling zone used for latest-year containment. '
                   'Enable Held-out historical zones in the legend to see the reference zones used for historical containment; '
                   'hover their boundaries to identify the held-out year. Zero base, excluded years and rolling days affect the scores.')],
            style={'fontSize':'12px','color':'#aaa','marginBottom':'8px'}),
        dcc.Loading([html.Div(id="seac-status", className="seac-status"),
                     dcc.Graph(id="seac-chart", figure=empty_figure(), config={"displaylogo": False, "scrollZoom": False,
                         "responsive": True, "toImageButtonOptions": {"filename": "seac_seasonality", "scale": 2}}, className="seac-chart")]),
        html.Details([html.Summary("Chart notes"), html.Div(id="seac-notes", children="Edit weights, then press Plot. "
            "All years align to expiry. White marks the latest displayed year. "
            "Volume and open interest are not stored in the settlement database.")], className="seac-notes"),
    ], className="seac-builder")


def register_seac_callbacks(app):
    app.clientside_callback(
        """function(visible, multiplier, days, currentMultiplier, stats) {
            const skip = window.dash_clientside.no_update;
            if (!stats) return [skip,'N/A','N/A','N/A','N/A','Plot a structure to calculate scores.'];
            if (multiplier === null || !Number.isFinite(multiplier) || multiplier < 0 ||
                !Number.isInteger(days) || days < 1 || days > 365 || currentMultiplier === null ||
                !Number.isFinite(currentMultiplier) || currentMultiplier < 0) return Array(6).fill(skip);
            const graph = document.querySelector('#seac-chart .js-plotly-plot');
            if (!graph || !graph.layout || !graph.layout.meta || graph.layout.meta.stats !== stats.revision) return Array(6).fill(skip);
            visible = visible || [];
            function calculate(xs, values) {
              const mean=[], sd=[];
              for (const x of xs) {
                let count=0, average=0, m2=0;
                for (let i=0; i<stats.sourceX.length; i++) {
                    if (stats.sourceX[i] <= x-days || stats.sourceX[i] > x) continue;
                    for (const value of values[i]) {
                        if (value === null || !Number.isFinite(value)) continue;
                        count++;
                        const delta=value-average;
                        average+=delta/count;
                        m2+=delta*(value-average);
                    }
                }
                mean.push(count ? average : null);
                sd.push(count>1 ? Math.sqrt(Math.max(0,m2/(count-1))) : null);
              }
              return {mean, sd};
            }
            const otherValues = stats.sourceValues.map(row => row.filter((_,i) => i !== stats.currentColumn));
            const {mean, sd} = calculate(stats.x, otherValues);
            const current = calculate(stats.currentX, stats.currentValues);
            const finite = value => value !== null && Number.isFinite(value);
            const visibleX = new Set(stats.x);
            const visibleRows = stats.sourceValues.filter((_,i) => visibleX.has(stats.sourceX[i]));
            const columnCount = stats.sourceValues.length ? stats.sourceValues[0].length : 0;
            const olderEntries = Array.from({length:columnCount},(_,i)=>i).filter(i=>i!==stats.currentColumn)
                .map(i=>({column:i,values:visibleRows.map(row=>row[i]).filter(finite)})).filter(entry=>entry.values.length);
            const older = olderEntries.map(entry=>entry.values);
            const historical = older.flat();
            const latest = stats.currentColumn < 0 ? [] : visibleRows.map(row=>row[stats.currentColumn]).filter(finite);
            const ready = older.length >= 3 && historical.length >= 20;
            function quantile(values,p) {
                const sorted=values.slice().sort((a,b)=>a-b), at=(sorted.length-1)*p;
                const low=Math.floor(at), high=Math.ceil(at);
                return sorted[low]+(sorted[high]-sorted[low])*(at-low);
            }
            const otherColumns=olderEntries.map(entry=>entry.column);
            const windowRows=stats.x.map(x=>stats.sourceValues.filter((_,i)=>stats.sourceX[i]>x-days && stats.sourceX[i]<=x));
            const rowByX=new Map(stats.sourceX.map((x,i)=>[x,stats.sourceValues[i]]));
            function rollingZone(columns) {
                const low=[],high=[],counts=[];
                for (const rows of windowRows) {
                    const values=[],years=new Set();
                    for (const row of rows) for (const column of columns) {
                        if (finite(row[column])) {values.push(row[column]);years.add(column);}
                    }
                    const valid=values.length>=20 && years.size>=2;
                    low.push(valid ? quantile(values,.05) : null);
                    high.push(valid ? quantile(values,.95) : null);
                    counts.push(values.length);
                }
                return {low,high,counts};
            }
            function coverage(column,band) {
                let count=0,inside=0;
                if (band && column>=0) stats.x.forEach((x,i)=>{
                    const value=(rowByX.get(x)||[])[column];
                    if (!finite(value) || !finite(band.low[i]) || !finite(band.high[i])) return;
                    count++;
                    if (value>=band.low[i] && value<=band.high[i]) inside++;
                });
                return {score:count>=5 ? 100*inside/count : null,count};
            }
            const zone=ready ? rollingZone(otherColumns) : null;
            const foldZones=ready ? olderEntries.filter(entry=>entry.values.length>=5).map(entry=>{
                const band=rollingZone(otherColumns.filter(column=>column!==entry.column));
                return {column:entry.column,...band,...coverage(entry.column,band)};
            }) : [];
            const folds=foldZones.map(fold=>fold.score).filter(finite);
            const average = values => values.reduce((sum,value)=>sum+value,0)/values.length;
            const historyZone = folds.length>=3 ? average(folds) : null;
            const latestCoverage=coverage(stats.currentColumn,zone);
            const latestZone=latestCoverage.score;
            const globalMean=historical.length ? average(historical) : null;
            const globalSD=historical.length>1 ? Math.sqrt(historical.reduce((sum,value)=>sum+(value-globalMean)**2,0)/(historical.length-1)) : null;
            const span = values => Math.max(...values)-Math.min(...values);
            const paired = stats.x.map((x,i)=>({x,m:mean[i],s:sd[i]})).filter(p=>finite(p.m)&&finite(p.s));
            const scaled = (value,scale) => scale===0 ? (value===0 ? 0 : Infinity) : value/scale;
            const historyStability = ready && paired.length>=5 ?
                100/(1+scaled(span(paired.map(p=>p.m)),globalSD)+scaled(span(paired.map(p=>p.s)),globalSD)) : null;
            const reference = new Map(paired.map(p=>[p.x,p]));
            const conformity=[];
            if (ready) stats.currentX.forEach((x,i)=>{
                const historicalPoint=reference.get(x), m=current.mean[i], s=current.sd[i];
                if (!historicalPoint || !finite(m) || !finite(s)) return;
                const displacement=scaled(Math.abs(m-historicalPoint.m),historicalPoint.s);
                const volatility=(s===0 && historicalPoint.s===0) ? 0 :
                    (s===0 || historicalPoint.s===0) ? Infinity : Math.abs(Math.log(s/historicalPoint.s));
                conformity.push(100/(1+displacement+volatility));
            });
            const latestStability = conformity.length>=5 ? average(conformity) : null;
            const score = value => finite(value) ? value.toFixed(1)+' / 100' : 'N/A';
            const format = value => Number(value.toPrecision(5)).toString();
            const validZone=zone ? stats.x.map((x,i)=>({x,i})).filter(p=>finite(zone.low[p.i]) && finite(zone.high[p.i])) : [];
            const latestZonePoints=validZone.filter(p=>finite((rowByX.get(p.x)||[])[stats.currentColumn]));
            const zonePoint=(latestZonePoints.length ? latestZonePoints : validZone).slice(-1)[0];
            const details = 'Latest contract year: '+stats.currentYear+'; older years: '+older.length+
                '; historical observations in visible window: '+historical.length+'; latest observations: '+latest.length+
                '; latest containment dates scored: '+latestCoverage.count+'; valid held-out years: '+folds.length+
                '; matched stability dates: '+conformity.length+'; rolling window: '+days+' calendar days'+
                (zonePoint ? '; reference zone at DTE '+zonePoint.x+': '+format(zone.low[zonePoint.i])+' to '+format(zone.high[zonePoint.i])+
                    '; width: '+format(zone.high[zonePoint.i]-zone.low[zonePoint.i])+'; reference observations: '+zone.counts[zonePoint.i] :
                    '; insufficient rolling percentile coverage (need 20 observations from at least 2 reference years per position)');
            const upper = mean.map((m,i) => sd[i] === null ? null : m + multiplier*sd[i]);
            const lower = mean.map((m,i) => sd[i] === null ? null : m - multiplier*sd[i]);
            const currentUpper = current.mean.map((m,i) => current.sd[i] === null ? null : m + currentMultiplier*current.sd[i]);
            const currentLower = current.mean.map((m,i) => current.sd[i] === null ? null : m - currentMultiplier*current.sd[i]);
            const update = {
                x: [stats.x, stats.x, stats.x, stats.currentX, stats.currentX], y: [mean, upper, lower, currentUpper, currentLower],
                visible: [visible.includes('mean'), visible.includes('sd'), visible.includes('sd'), visible.includes('current'), visible.includes('current')],
                name: ['Other years rolling mean', 'Other years mean + '+multiplier+' SD', 'Other years mean ± '+multiplier+' SD',
                    'Latest '+stats.currentYear+' mean + '+currentMultiplier+' SD', 'Latest '+stats.currentYear+' mean ± '+currentMultiplier+' SD']
            };
            const indices=stats.traces.concat(stats.currentTraces);
            function zoneUpdate(traces,bounds,name,hidden) {
                traces.forEach((trace,i)=>{
                    indices.push(trace);
                    const values=bounds ? (i ? bounds.high : bounds.low) : [];
                    const available=values.some(finite);
                    update.x.push(available ? stats.x : []);
                    update.y.push(available ? values : []);
                    const old=graph.data[trace].visible;
                    update.visible.push(available ? (old==='legendonly' || hidden && old===false ? 'legendonly' : true) : false);
                    update.name.push(name);
                });
            }
            zoneUpdate(stats.zoneTraces || [],zone,'Rolling historical P5–P95 (excludes '+stats.currentYear+')',false);
            (stats.foldZones || []).forEach(fold=>{
                const bounds=foldZones.find(item=>item.column===fold.column);
                zoneUpdate(fold.traces,bounds || null,'Held-out historical zones',true);
            });
            return Plotly.restyle(graph, update, indices).then(() => [stats.revision,
                score(historyZone),score(latestZone),score(historyStability),score(latestStability),details]);
        }""",
        Output("seac-stat-rendered", "data"),
        Output('seac-score-history-zone','children'),Output('seac-score-latest-zone','children'),
        Output('seac-score-history-stability','children'),Output('seac-score-latest-stability','children'),
        Output('seac-score-details','children'),Input("seac-stat-visible", "value"),
        Input("seac-sd-multiplier", "value"), Input("seac-rolling-days", "value"),
        Input("seac-current-sd", "value"), Input("seac-statistics", "data"))

    app.clientside_callback(
        """function(point, config) {
            const skip = window.dash_clientside.no_update;
            if (!point || !config || !point.bid || !point.ask || point.revision !== config.revision) return skip;
            const graph = document.querySelector('#seac-chart .js-plotly-plot');
            if (!graph || !graph.data || !graph.data[point.trace] ||
                graph.data[point.trace].name !== 'Live price') return skip;
            if (!graph.data[config.bidTrace] || graph.data[config.bidTrace].name !== 'Bid range' ||
                !graph.data[config.askTrace] || graph.data[config.askTrace].name !== 'Ask range') return skip;
            const line = config.baseLine.map(p => p.slice());
            const stats = JSON.parse(JSON.stringify(config.baseStats));
            if (point.x.length) {
                const x=point.x[0], y=point.y[0];
                const date=new Date(Date.parse(config.expiry.slice(0,10)+'T00:00:00Z')+x*86400000).toISOString().slice(0,10);
                const old=line.findIndex(p=>p[0]===x);
                if(old>=0) line[old]=[x,y,date]; else line.push([x,y,date]);
                line.sort((a,b)=>a[0]-b[0]);
                let i=stats.sourceX.indexOf(x);
                if(i<0) {
                    i=stats.sourceX.findIndex(value=>value>x);
                    if(i<0) i=stats.sourceX.length;
                    stats.sourceX.splice(i,0,x);
                    stats.sourceValues.splice(i,0,new Array(config.sourceColumns).fill(null));
                }
                stats.sourceValues[i][config.currentColumn]=y;
                stats.x=[...new Set(stats.x.concat(x))].sort((a,b)=>a-b);
                stats.currentX=[...new Set(stats.currentX.concat(x))].sort((a,b)=>a-b);
            }
            stats.currentValues=stats.sourceValues.map(row=>[row[config.currentColumn]]);
            return Plotly.restyle(graph, {
                x: [line.map(p=>p[0]),point.x,point.bid.x,point.ask.x],
                y: [line.map(p=>p[1]),point.y,point.bid.y,point.ask.y],
                text: [line.map(()=>null),point.x.map(()=>null),point.bid.text,point.ask.text],
                hovertext: [line.map(()=>null),point.x.map(()=>null),point.bid.hover,point.ask.hover],
                customdata: [line.map(p=>p[2]),point.x.map(()=>null),point.bid.x.map(()=>null),point.ask.x.map(()=>null)]
            }, [config.yearTrace,point.trace,config.bidTrace,config.askTrace]).then(function() {
                window.dash_clientside.set_props('seac-statistics',{data:stats});
                return point.revision;
            });
        }""",
        Output("seac-live-rendered", "data"), Input("seac-live-point", "data"),
        State("seac-live-config", "data"))

    app.clientside_callback(
        "function(value) { window.seacMonthStructure = value; return value; }",
        Output("seac-structure-wheel", "data"), Input("seac-structure", "value"))

    @app.callback(Output("seac-exclude", "options"), Output("seac-exclude", "value"),
                  Input("seac-years", "value"), Input("seac-weights", "data"), State("seac-exclude", "value"))
    def exclusion_years(count, rows, excluded):
        try:
            years = latest_years(count, expression_from_weights(rows))
        except (ValueError, TypeError):
            return [], []
        return ([{"label": str(year), "value": year} for year in sorted(years, reverse=True)],
                [year for year in (excluded or []) if year in years])

    @app.callback(Output("seac-months", "children"), Input("seac-weights", "data"))
    def render_months(rows):
        return month_cells((rows or [{}])[0])

    @app.callback(Output("seac-weights", "data"), Output("seac-chain-note", "children"),
                  Output("seac-editor-error", "children"),
                  Output("seac-selected-month", "data"), Output("seac-month-editor", "style"),
                  Output("seac-editor-title", "children"), Output("seac-leg-count", "value"),
                  Output("seac-structure", "value"),
                  Input({"type": "seac-month", "code": ALL}, "n_clicks"),
                  Input({"type": "seac-month-clear", "code": ALL}, "n_clicks"),
                  Input("seac-close-month", "n_clicks"), Input("seac-product", "value"),
                  Input("seac-month-move", "data"),
                  State("seac-weights", "data"), State("seac-selected-month", "data"),
                  State("seac-leg-count", "value"), State("seac-structure", "value"))
    def edit_month(clicks, clear_clicks, _, product, move, rows, selected, leg_count, structure):
        trigger = ctx.triggered_id
        if isinstance(trigger, dict) and not ctx.triggered[0].get("value"):
            return (no_update,) * 8
        row = (rows or [{}])[0]
        try:
            clearing = isinstance(trigger, dict) and trigger["type"] == "seac-month-clear"
            if selected and trigger != "seac-product" and not (clearing and trigger["code"] == selected):
                row = apply_structure(row, selected, leg_count, structure)
            if trigger == "seac-month-move":
                source, target = move["source"], move["target"]
                if source == target or source not in row or target not in row or not selection(row[source])["count"]:
                    return (no_update,) * 8
                row = {**row, target: dict(selection(row[source])), source: {"count": 0, "structure": "outright"}}
                return [row], f"Moved {source} to {target} · press Plot to update chart", "", None, {"display": "none"}, "", 0, "outright"
            if clearing:
                row = apply_structure(row, trigger["code"], 0, "outright")
                return [row], f"Cleared {trigger['code']} · press Plot to update chart", "", None, {"display": "none"}, "", 0, "outright"
            if isinstance(trigger, dict):
                code = trigger["code"]
                item = selection(row[code])
                # Do not rebuild buttons while opening: their reset click counts would close the popup.
                data = [row] if selected and selected != code else no_update
                return data, no_update, "", code, {"display": "flex"}, code, item["count"], item["structure"]
            if trigger == "seac-close-month":
                return [row], "Saved · press Plot to update chart", "", None, {"display": "none"}, "", 0, "outright"
            codes = contract_chain(product)
            row = {code: {"count": 0, "structure": "outright"} for code in codes}
            return [row], "Click a month · edits save when the popup closes · press Plot to update chart", "", None, {"display": "none"}, "", 0, "outright"
        except (ValueError, TypeError, KeyError, OSError) as error:
            return no_update, no_update, str(error), no_update, {"display": "flex"}, no_update, no_update, no_update

    @app.callback(Output("seac-expression", "children"),
                  Input("seac-weights", "data"), Input("seac-product", "value"))
    def expression_preview(rows, product):
        try:
            expression = f"{product} · {expression_from_weights(rows)}"
        except (ValueError, TypeError) as error:
            expression = str(error)
        return "Expression preview: " + expression

    @app.callback(Output("seac-chart", "figure"), Output("seac-status", "children"),
                  Output("seac-notes", "children"), Output("seac-live-config", "data"), Output("seac-statistics", "data"), Input("seac-plot", "n_clicks"),
                  *[State(name, "value") for name in ("seac-product", "seac-years", "seac-exclude",
                    "seac-window", "seac-flags")],
                  State("seac-weights", "data"), State("seac-selected-month", "data"),
                  State("seac-leg-count", "value"), State("seac-structure", "value"),
                  State("seac-stat-visible", "value"), State("seac-sd-multiplier", "value"),
                  State("seac-rolling-days", "value"), State("seac-current-sd", "value"), prevent_initial_call=True)
    def plot(_, product, years, exclude, window, flags, rows, selected, leg_count, structure, stat_visible, multiplier, days, current_multiplier):
        view = "overlay"
        try:
            if selected:
                rows = [apply_structure((rows or [{}])[0], selected, leg_count, structure)]
            expression = expression_from_weights(rows)
            if multiplier is None or not isfinite(float(multiplier)) or float(multiplier) < 0:
                raise ValueError("SD multiplier must be a nonnegative number")
            multiplier = float(multiplier)
            if current_multiplier is None or not isfinite(float(current_multiplier)) or float(current_multiplier) < 0:
                raise ValueError("Current-year SD multiplier must be a nonnegative number")
            current_multiplier = float(current_multiplier)
            if days is None or not isfinite(float(days)) or int(days) != float(days) or not 1 <= int(days) <= 365:
                raise ValueError("Rolling days must be a whole number from 1 to 365")
            days = int(days)
            requested = latest_years(years, expression)
            if window is None or not 1 <= float(window) <= 2000 or float(window) != int(window):
                raise ValueError("Lookback must be a whole number from 1 to 2000 working days")
            window, flags = int(window), flags or []
            if product not in PRODUCTS:
                raise ValueError("Select a supported product")
            signature = database_signature()
            if signature is None:
                raise ValueError("Master database unavailable")
            reference_day = today()
            anchor = parse_expression(expression)[0][1]
            expiry = OfficialExpiryLookup().get("CL" if product == "CL-CO" else product, anchor)
            if expiry is None:
                raise ValueError("An official anchor expiry is required for the today-relative experiment")
            # Calendar days bound the working-day history needed for this window.
            fetch_window = max(1, (pd.Timestamp(expiry) - reference_day).days + 90 + 365)
            base = history(product, expression, min(requested), max(requested), fetch_window, tuple(signature))
            base = align_to_today(base, product, expression, reference_day)
            result = prepare_result(base, requested, exclude, window, view, flags)
            source = statistics_source(base, result, flags)
            year = max(requested)
            config = live_config(base, result, product, expression, window, view, flags)
            quote = fetch_live_quote(product, expression) if config else None
            set_props('seac-live-status',{'children':live_status(config,quote)})
            x, y = live_coordinates(config, quote)
            original_stats = {"x": result["combined"].index.tolist(), "sourceX": source.index.tolist(),
                "sourceValues": [[float(v) if pd.notna(v) else None for v in row] for row in source.to_numpy()],
                "currentX": result["series"][year].index.tolist() if year in result["series"] else []}
            if config:
                config["baseLine"] = [[int(day), float(row.value), pd.Timestamp(row.date).strftime("%Y-%m-%d")]
                                      for day, row in result["series"][year].iterrows()]
                config["currentColumn"] = list(source.columns).index(year)
                config["sourceColumns"] = len(source.columns)
            if x:
                day = pd.Timestamp(expiry) + pd.Timedelta(days=x[0])
                frame = result["series"][year]
                frame.loc[x[0], ["value", "date", "is_live"]] = [y[0], day, False]
                result["series"][year] = frame.sort_index()
                result["combined"].loc[x[0], year] = y[0]
                result["combined"] = result["combined"].sort_index()
                source.loc[x[0], year] = y[0]
                source = source.sort_index()
            title = f"{product} · {format_contract_label(expression)}"
            figure = SeasonalityPlotter().build_seasonality_figure(result, title, height=690)
            statistics = add_statistics(figure, result["combined"], stat_visible or [], multiplier, days,
                                        source, max(requested),
                                        result["series"][max(requested)].index if max(requested) in result["series"] else [],
                                        current_multiplier)
            statistics["revision"] = uuid4().hex
            if config:
                config["baseStats"] = {**statistics, **original_stats}
                config["yearTrace"] = next(i for i, trace in enumerate(figure.data) if trace.name == str(year))
            figure.update_layout(meta={"stats": statistics["revision"]})
            figure.update_layout(font=dict(family="Arial, sans-serif", size=12, color="#e5e7eb"),
                margin=dict(l=60, r=22, t=100, b=48), title=dict(x=.5, y=.98, xanchor="center", yanchor="top", font=dict(size=14)),
                yaxis_title="Daily price change" if view == "change" else "Price change from first observation" if "zero" in flags else "Settlement price",
                uirevision=f"seac:{product}:{expression}:{sorted(requested)}:{window}:{view}:{flags}",
                legend=dict(orientation="h", x=.5, xanchor="center", y=1.03), hovermode="x unified")
            figure.update_xaxes(range=result["window_bounds"], autorange=False, zeroline=True, zerolinecolor="#737373", showspikes=True)
            figure.update_yaxes(zeroline=True, zerolinecolor="#d4d4d4", gridcolor="#232323")
            figure.add_trace(go.Scatter(x=x, y=y, mode="markers", name="Live price",
                marker=dict(size=7, color="#22c55e", line=dict(color="white", width=1)),
                hovertemplate="Live price<br>Value: %{y}<extra></extra>"))
            if config:
                config["trace"] = len(figure.data) - 1
                config["revision"] = uuid4().hex
            for side, color, fill, position in (("bid", "#60a5fa", "rgba(96,165,250,.3)", "top left"),
                                               ("ask", "#fbbf24", "rgba(251,191,36,.3)", "bottom right")):
                point = side_point(config, quote, side)
                figure.add_trace(go.Scatter(x=point["x"], y=point["y"], text=point["text"],
                    mode="lines+text", name=side.title() + " range", cliponaxis=False,
                    line=dict(color=color,width=2),fill="toself",fillcolor=fill,
                    hovertext=point["hover"],hoveron="points+fills",
                    textposition=position, textfont=dict(color=color, size=12),
                    hovertemplate="%{hovertext}<extra></extra>"))
                if config:
                    config[side + "Trace"] = len(figure.data) - 1
            observations = sum(len(frame) for frame in result["series"].values())
            status = f"{len(result['series'])} years · {observations:,} observations · Today −90 to +90 calendar days · Daily settlements · Plot to apply edits"
            notes = [html.P("White = latest displayed year. All years align to expiry. "
                            "The live quote replaces today's latest-year observation in the line and statistics. Gaps are not filled. The green dot uses the local live-price server "
                            "and refreshes every 10 seconds. Blue/yellow bands show the lowest-to-highest available bid/ask "
                            "across discovered constructions, with raw price labels. Hover for the components. "
                            "Each construction has its own paired sides; band endpoints can come from different constructions. "
                            "These are discovered alternatives, not an exhaustive market range. The green reference midpoint "
                            "continues to drive the current-year line and statistics. "
                            "Quotes older than 2 minutes are hidden. No volume/OI data is included.")]
            notes += [html.Div(warning) for warning in base.get("warnings", [])[:25]]
            notes.append(html.P("Other-years rolling mean and sample SD exclude the latest selected contract year. "
                                "The latest-year SD is calculated separately, including today's live observation. Both use "
                                "the trailing N calendar days, including the current point. Missing values are not filled. "
                                "SD requires at least two observations. Earlier history warms up the visible window."))
            return figure, status, notes, config, statistics
        except (ValueError, TypeError, KeyError, OSError, RuntimeError, duckdb.Error) as error:
            set_props('seac-live-status',{'children':'Live paused: correct the plot settings and press Plot.'})
            return empty_figure("Unable to plot these settings"), html.Span(str(error), className="seac-error"), str(error), None, None

    @app.callback(Output("seac-live-point", "data"), Output('seac-live-status','children'),
                  Input("seac-live-tick", "n_intervals"), State("seac-live-config", "data"), prevent_initial_call=True)
    def refresh_live_dot(_, config):
        if not config:
            raise PreventUpdate
        quote = fetch_live_quote(config["product"], config["expression"])
        x, y = live_coordinates(config, quote)
        return ({"revision": config["revision"], "trace": config["trace"], "x": x, "y": y,
                 "bid": side_point(config, quote, "bid"), "ask": side_point(config, quote, "ask")},live_status(config,quote))


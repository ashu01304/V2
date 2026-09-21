"""Plotly presentation for range-test results."""

import plotly.graph_objects as go
from plotly.subplots import make_subplots

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




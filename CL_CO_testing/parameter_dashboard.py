"""Dashboard for the Chart 2 hyperparameter report."""

from pathlib import Path

import pandas as pd
import plotly.express as px
from dash import Dash, Input, Output, ctx, dcc, html


REPORT = Path(__file__).with_name("chart_2_parameter_report.xlsx")
FILTERED_OUTPUT = Path(__file__).with_name("filtered_chart_2_settings.xlsx")
BEST_OUTPUT = Path(__file__).with_name("best_chart_2_settings.xlsx")
PARAMETERS = ["timeframe", "tp_sl_ticks", "days_before_expiry", "no_trade_days",
              "minimum_trade_gap", "max_parallel", "max_consecutive",
              "rank_lookback", "band_rank"]
RANKINGS = {
    "Robust win rate": "wilson_lower_95", "Win rate": "win_rate",
    "Total PnL": "total_pnl_ticks", "Contract consistency": "profitable_contract_rate",
}
INPUT_STYLE = {"width": "100%", "color": "#111827"}
PANEL = {"background": "#111827", "border": "1px solid #334155",
         "borderRadius": "8px", "padding": "16px", "margin": "12px auto",
         "maxWidth": "1400px"}


def choices(values):
    return [{"label": str(value), "value": value}
            for value in sorted(pd.Series(values).dropna().unique())]


def create_app(report=REPORT):
    raw = pd.read_excel(report, sheet_name="Raw_Results")
    app = Dash(__name__)
    controls = [html.Label([
        html.Span(name.replace("_", " ").title(),
                  style={"display": "block", "marginBottom": "4px"}),
        dcc.Dropdown(id=f"filter-{name}", options=choices(raw[name]), multi=True,
                     placeholder="All values", style=INPUT_STYLE),
    ]) for name in PARAMETERS]
    app.layout = html.Div([
        html.H2("Find the Best Chart 2 Settings", style={"textAlign": "center"}),
        html.P("Results combine all seven CL-CO 1MF contracts.",
               style={"textAlign": "center", "color": "#94a3b8"}),
        html.Div([html.H3("1. Narrow parameter values"), html.Div(
            controls, style={"display": "grid", "gridTemplateColumns":
                             "repeat(auto-fit, minmax(190px, 1fr))", "gap": "12px"})],
            style=PANEL),
        html.Div([html.H3("2. Choose what best means"), html.Div([
            html.Label(["Rank using", dcc.Dropdown(
                id="ranking", options=[{"label": key, "value": value}
                                        for key, value in RANKINGS.items()],
                value="wilson_lower_95", clearable=False, style=INPUT_STYLE)]),
            html.Label(["Minimum total trades", dcc.Input(
                id="minimum-trades", type="number", value=0, min=0, step=1,
                debounce=True, style={"width": "100%", "height": "36px"})]),
            html.Label(["Minimum win rate (%)", dcc.Input(
                id="minimum-win-rate", type="number", value=0, min=0, max=100,
                step=1, debounce=True, style={"width": "100%", "height": "36px"})]),
            html.Div([html.Button(
                "Save filtered settings", id="save-filtered", n_clicks=0,
                style={"padding": "10px 18px", "fontWeight": "bold"}),
                html.Div(id="save-status", style={"color": "#86efac", "marginTop": "6px"})]),
            html.Label(["Best settings per tick value", dcc.Input(
                id="best-count", type="number", value=8, min=1, step=1,
                style={"width": "100%", "height": "36px"})]),
            html.Label(["Paired TP/SL ticks", dcc.Checklist(
                id="best-ticks", options=[2, 3], value=[2, 3], inline=True)]),
            html.Button("Save best settings", id="save-best", n_clicks=0,
                        style={"padding": "10px 18px", "fontWeight": "bold"}),
        ], style={"display": "grid", "gridTemplateColumns": "repeat(4, 1fr)",
                  "gap": "16px"})], style=PANEL),
        html.Div(id="best", style={"display": "flex", "justifyContent": "center",
                                    "flexWrap": "wrap", "gap": "12px"}),
        html.Div([html.H3("3. Compare the top 100 settings"),
                  dcc.Graph(id="comparison")], style=PANEL),
        html.Div(id="results", style=PANEL),
    ], style={"background": "#020617", "color": "white", "minHeight": "100vh",
              "padding": "10px 20px 40px"})

    @app.callback(Output("best", "children"), Output("comparison", "figure"),
                  Output("results", "children"), Output("save-status", "children"),
                  Input("ranking", "value"),
                  Input("minimum-trades", "value"),
                  Input("minimum-win-rate", "value"),
                  Input("save-filtered", "n_clicks"),
                  Input("save-best", "n_clicks"),
                  Input("best-count", "value"),
                  Input("best-ticks", "value"),
                  *[Input(f"filter-{name}", "value") for name in PARAMETERS])
    def update(ranking, minimum_trades, minimum_win_rate, _filtered_clicks,
               _best_clicks, best_count, best_ticks, *filters):
        selected = raw
        for name, values in zip(PARAMETERS, filters):
            if values:
                selected = selected[selected[name].isin(values)]
        selected = selected[selected["trades"] >= (minimum_trades or 0)]
        selected = selected[selected["win_rate"] >= (minimum_win_rate or 0) / 100]
        selected = selected.sort_values(
            [ranking, "total_pnl_ticks", "trades"], ascending=False
        )
        save_status = ""
        if ctx.triggered_id == "save-filtered":
            selected.to_excel(FILTERED_OUTPUT, index=False)
            save_status = f"Saved {len(selected):,} settings to {FILTERED_OUTPUT.name}"
        elif ctx.triggered_id == "save-best":
            groups = [selected[selected["tp_sl_ticks"] == tick].head(
                max(1, int(best_count or 8))).assign(selected_tp_sl_ticks=tick)
                for tick in (best_ticks or [])]
            best_settings = pd.concat(groups, ignore_index=True) if groups else selected.head(0)
            best_settings.to_excel(BEST_OUTPUT, index=False)
            save_status = f"Saved {len(best_settings):,} settings to {BEST_OUTPUT.name}"
        selected = selected.head(100)
        if selected.empty:
            return "No matching results", {}, [], save_status
        best = selected.iloc[0]
        card = {**PANEL, "margin": "4px", "minWidth": "170px",
                "textAlign": "center"}
        message = [
            html.Div([html.Small("Best run"), html.H3(int(best.run_id))], style=card),
            html.Div([html.Small("Win rate"), html.H3(f"{best.win_rate:.1%}")], style=card),
            html.Div([html.Small("Total PnL"),
                      html.H3(f"{int(best.total_pnl_ticks):+d} ticks")], style=card),
            html.Div([html.Small("Trades"), html.H3(int(best.trades))], style=card),
            html.Div([html.Small("Profitable contracts"),
                      html.H3(f"{best.profitable_contract_rate:.0%}")], style=card),
        ]
        figure = px.scatter(selected, x="trades", y="total_pnl_ticks",
                            color="win_rate", hover_data=PARAMETERS,
                            template="plotly_dark")
        recommendations = []
        for position, (_, result) in enumerate(selected.head(10).iterrows(), 1):
            chips = [html.Span(
                f"{name.replace('_', ' ').title()}: {result[name]:g}",
                style={"background": "#1e293b", "border": "1px solid #475569",
                       "borderRadius": "14px", "padding": "4px 8px", "margin": "3px"})
                     for name in PARAMETERS]
            recommendations.append(html.Div([
                html.Div([
                    html.B(f"#{position}", style={"color": "#a78bfa", "fontSize": "18px"}),
                    html.Span(f"Win {result.win_rate:.1%}"),
                    html.Span(f"Robust {result.wilson_lower_95:.1%}"),
                    html.Span(f"Trades {result.trades:,.0f}"),
                    html.Span(f"PnL {result.total_pnl_ticks:+,.0f} ticks"),
                    html.Span(f"Profitable contracts {result.profitable_contract_rate:.0%}"),
                ], style={"display": "flex", "gap": "14px", "flexWrap": "wrap",
                          "marginBottom": "7px"}),
                html.Div(chips, style={"display": "flex", "flexWrap": "wrap"}),
            ], style={"background": "#0f172a", "border": "1px solid #334155",
                      "borderRadius": "7px", "padding": "10px"}))
        ranking_label = next(label for label, value in RANKINGS.items()
                             if value == ranking)
        recommendations.insert(0, html.H3(f"Top 10 settings by {ranking_label}"))
        return message, figure, recommendations, save_status
    return app


if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=8052, debug=False)

"""Live CL-CO monthly boxes and structures calculated from raw outright legs."""

import argparse

import plotly.graph_objects as go
from dash import Dash, Input, Output, dash_table, dcc, html

from analysis.contract_universe import load_universe
from analysis.expression import parse_expression
from market_data import LiveMarketDataClient


MONTH_ORDER = {month: index for index, month in enumerate("FGHJKMNQUVXZ", 1)}
UNIVERSE = load_universe()


def contract_order(contract):
    return 2000 + int(contract[1:]), MONTH_ORDER[contract[0]]


def live_boxes(client):
    rows = client.snapshot(["CL", "CO"], raw=True)
    prices = {(row["product"], row["contract"]): row
              for row in rows if row.get("price") is not None}
    contracts = sorted(
        {contract for product, contract in prices if product == "CL"}
        & {contract for product, contract in prices if product == "CO"},
        key=contract_order,
    )
    boxes, display = {}, []
    for contract in contracts:
        cl, co = prices[("CL", contract)], prices[("CO", contract)]
        value = float(cl["price"]) - float(co["price"])
        boxes[contract] = value
        display.append({
            "Contract": contract, "CL": round(float(cl["price"]), 2),
            "CO": round(float(co["price"]), 2), "Box": round(value, 2),
            "Updated": max(cl["received_at"], co["received_at"]),
        })
    return boxes, display


def structure_rows(boxes):
    rows = []
    for anchor, structures in UNIVERSE["contracts"].items():
        row, available = {"Anchor": anchor}, False
        for name in UNIVERSE["columns"]:
            expression = structures[name]
            legs = parse_expression(expression)
            if legs and all(contract in boxes for _, contract in legs):
                value = sum(coefficient * boxes[contract]
                            for coefficient, contract in legs)
                row[name] = round(value, 2)
                available = True
            else:
                row[name] = None
        if anchor in boxes:
            row["Box"] = round(boxes[anchor], 2)
            available = True
        if available:
            rows.append(row)
    return rows


def curve_figure(rows):
    figure = go.Figure()
    if rows:
        figure.add_trace(go.Scatter(
            x=[row["Contract"] for row in rows], y=[row["Box"] for row in rows],
            mode="lines+markers", name="CL − CO",
            line=dict(color="#38bdf8", width=2),
            marker=dict(color="#22c55e", size=7),
            customdata=[[row["CL"], row["CO"]] for row in rows],
            hovertemplate=("%{x}<br>CL: %{customdata[0]:.2f}"
                           "<br>CO: %{customdata[1]:.2f}"
                           "<br>Box: %{y:.2f}<extra></extra>"),
        ))
    else:
        figure.add_annotation(text="No matching live CL and CO contracts", showarrow=False)
    figure.update_layout(
        template="plotly_dark", paper_bgcolor="black", plot_bgcolor="black",
        title="Monthly CL − CO Box Curve", xaxis_title="Contract",
        yaxis_title="CL − CO", height=520, uirevision="live-box-curve",
        margin=dict(l=60, r=25, t=70, b=55),
    )
    return figure


TABLE_STYLE = {
    "style_table": {"overflowX": "auto", "width": "100%"},
    "style_header": {"backgroundColor": "#1e293b", "color": "white",
                     "fontWeight": "bold", "textAlign": "center"},
    "style_cell": {"backgroundColor": "#0f172a", "color": "white",
                   "border": "1px solid #334155", "padding": "7px",
                   "minWidth": "85px", "textAlign": "center"},
}


def create_app():
    app = Dash(__name__)
    app.title = "Box Prices"
    client = LiveMarketDataClient()
    app.layout = html.Div([
        html.H2("Live CL − CO Boxes", style={"textAlign": "center"}),
        html.Div(id="status", style={"textAlign": "center", "color": "#94a3b8"}),
        dcc.Graph(id="curve", style={"height": "520px"}),
        html.H3("Monthly boxes", style={"textAlign": "center"}),
        dash_table.DataTable(id="monthly", **TABLE_STYLE),
        html.H3("Box spreads, flies and configured structures",
                style={"textAlign": "center", "marginTop": "28px"}),
        dash_table.DataTable(id="structures", **TABLE_STYLE),
        dcc.Interval(id="refresh", interval=10_000, n_intervals=0),
    ], style={"background": "#020617", "color": "white", "minHeight": "100vh",
              "padding": "10px 20px 40px"})

    @app.callback(
        Output("curve", "figure"), Output("monthly", "columns"),
        Output("monthly", "data"), Output("structures", "columns"),
        Output("structures", "data"), Output("status", "children"),
        Input("refresh", "n_intervals"),
    )
    def update(_):
        try:
            boxes, monthly = live_boxes(client)
            structures = structure_rows(boxes)
            monthly_columns = ["Contract", "CL", "CO", "Box", "Updated"]
            structure_columns = ["Anchor", "Box", *UNIVERSE["columns"]]
            status = (f"{len(monthly)} matched live months | "
                      f"{len(structures)} anchors with available structures")
            return (
                curve_figure(monthly),
                [{"name": column, "id": column} for column in monthly_columns], monthly,
                [{"name": column, "id": column} for column in structure_columns], structures,
                status,
            )
        except Exception as error:
            empty = go.Figure().update_layout(template="plotly_dark")
            return empty, [], [], [], [], f"Live data unavailable: {error}"
    return app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8055)
    arguments = parser.parse_args()
    create_app().run(host="127.0.0.1", port=arguments.port, debug=False)


if __name__ == "__main__":
    main()

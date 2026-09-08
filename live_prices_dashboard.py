"""Simple live CL/NG contract-price frontend using the AdminPrice feed."""

import argparse

from dash import Dash, Input, Output, dash_table, dcc, html

from analysis.contract_universe import load_universe
from analysis.expression import parse_expression
from market_data import LiveMarketDataClient


UNIVERSE = load_universe()
STRUCTURES = UNIVERSE["columns"]


class CentralPriceFeed:
    def __init__(self, base_url):
        self.client = LiveMarketDataClient(base_url)

    def snapshot(self, products, raw=False):
        try:
            source = self.client.snapshot(products, raw=raw)
            status = self.client.health()["status"]
        except Exception as error:
            return [], f"SERVICE UNAVAILABLE: {error}"
        rows = [{
            "Product": row["product"], "Contract": row["contract"],
            "Instrument": row["instrument"], "Bid": row["bid"],
            "Bid size": row["bid_size"], "Ask": row["ask"],
            "Ask size": row["ask_size"], "Mid": row["mid"], "Last": row["last"],
            "Last size": row["last_size"], "Admin": row["admin"],
            "Current settlement": row["current_settlement"],
            "Previous settlement": row["previous_settlement"],
            "Volume": row["volume"], "WAP": row["wap"],
            "Updated UTC": row["received_at"],
        } for row in source]
        return rows, status

    def structure_snapshot(self, products, structures):
        rows, status = self.snapshot(products, raw=True)
        prices = {}
        for row in rows:
            price = row["Admin"]
            if price is None:
                price = row["Last"] if row["Last"] is not None else row["Mid"]
            if price is not None:
                prices[(row["Product"], row["Contract"])] = price
        calculated = []
        for product in products or []:
            for anchor, expressions in UNIVERSE["contracts"].items():
                for strategy, expression in expressions.items():
                    if structures and strategy not in structures:
                        continue
                    legs = parse_expression(expression)
                    if not all((product, contract) in prices for _, contract in legs):
                        continue
                    value = sum(coefficient * prices[(product, contract)]
                                for coefficient, contract in legs)
                    calculated.append({
                        "Product": product, "Anchor": anchor, "Structure": strategy,
                        "Expression": expression,
                        "Value": round(value, 2 if product in {"CL", "CO"} else 6),
                        "Legs": len(legs),
                    })
        return calculated, status

    def disconnect(self):
        self.client.close()

def create_app(feed, products):
    app = Dash(__name__)
    app.title = "Live Prices"
    columns = ["Product", "Contract", "Bid", "Bid size", "Ask", "Ask size", "Mid",
               "Last", "Last size", "Admin", "Current settlement", "Previous settlement",
               "Volume", "WAP", "Updated UTC", "Instrument"]
    app.layout = html.Div([
        html.H2("Contract & Structure Prices", style={"textAlign": "center"}),
        html.Div([
            html.Label(["Products", dcc.Checklist(
                id="products", options=[{"label": product, "value": product}
                                        for product in products],
                value=products, inline=True, labelStyle={"marginRight": "25px"})]),
            html.Label(["Structures", dcc.Dropdown(
                id="structures", options=[{"label": value, "value": value}
                                          for value in STRUCTURES],
                value=STRUCTURES, multi=True, clearable=False,
                style={"width": "420px", "color": "#111827"})]),
            html.Div(id="connection-status"), html.Div(id="update-count"),
        ], style={"display": "flex", "justifyContent": "center", "gap": "35px",
                  "alignItems": "center", "padding": "14px", "background": "#111827"}),
        dash_table.DataTable(
            id="price-table", columns=[{"name": column, "id": column} for column in columns],
            sort_action="native", filter_action="native", fixed_rows={"headers": True},
            style_table={"maxWidth": "calc(100vw - 30px)", "maxHeight": "80vh",
                         "overflow": "auto", "margin": "15px auto"},
            style_header={"backgroundColor": "#1e293b", "color": "white",
                          "fontWeight": "bold"},
            style_cell={"backgroundColor": "#0f172a", "color": "white",
                        "border": "1px solid #334155", "padding": "7px",
                        "minWidth": "100px", "textAlign": "center"},
            style_data_conditional=[
                {"if": {"column_id": "Bid"}, "color": "#4ade80"},
                {"if": {"column_id": "Ask"}, "color": "#f87171"},
                {"if": {"column_id": "Admin"}, "color": "#fbbf24"},
            ],
        ),
        html.H3("Spreads, flies and double flies", style={"textAlign": "center"}),
        dash_table.DataTable(
            id="structure-table",
            columns=[{"name": column, "id": column} for column in (
                "Product", "Anchor", "Structure", "Expression", "Value", "Legs")],
            sort_action="native", filter_action="native", fixed_rows={"headers": True},
            style_table={"maxWidth": "1200px", "maxHeight": "70vh", "overflow": "auto",
                         "margin": "15px auto"},
            style_header={"backgroundColor": "#1e293b", "color": "white",
                          "fontWeight": "bold"},
            style_cell={"backgroundColor": "#0f172a", "color": "white",
                        "border": "1px solid #334155", "padding": "7px",
                        "minWidth": "110px", "textAlign": "center"},
            style_cell_conditional=[
                {"if": {"column_id": "Expression"}, "minWidth": "260px",
                 "maxWidth": "360px"},
                {"if": {"column_id": "Value"}, "color": "#fbbf24",
                 "fontWeight": "bold"},
            ],
        ),
        dcc.Interval(id="refresh", interval=1000, n_intervals=0),
    ], style={"background": "#020617", "color": "white", "minHeight": "100vh",
              "padding": "10px"})

    @app.callback(Output("price-table", "data"), Output("structure-table", "data"),
                  Output("connection-status", "children"), Output("update-count", "children"),
                  Input("refresh", "n_intervals"), Input("products", "value"),
                  Input("structures", "value"))
    def refresh(_, selected_products, selected_structures):
        rows, status = feed.snapshot(selected_products)
        structure_rows, _ = feed.structure_snapshot(selected_products, selected_structures)
        color = "#4ade80" if status.startswith("CONNECTED") else "#fbbf24"
        return (rows, structure_rows,
                html.Span(status, style={"color": color, "fontWeight": "bold"}),
                f"Contracts: {len(rows)} | Structures: {len(structure_rows)}")
    return app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--products", nargs="+", default=["CL", "NG"])
    parser.add_argument("--port", type=int, default=8054)
    parser.add_argument("--service", default="http://127.0.0.1:8060")
    arguments = parser.parse_args()
    feed = CentralPriceFeed(arguments.service)
    try:
        create_app(feed, arguments.products).run(
            host="127.0.0.1", port=arguments.port, debug=False
        )
    finally:
        feed.disconnect()


if __name__ == "__main__":
    main()

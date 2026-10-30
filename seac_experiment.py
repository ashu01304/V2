"""Experimental SEAC dashboard: 90 calendar days either side of today."""

import argparse

from dash import Dash

from dashboard_components.seac_experiment import builder_layout, register_seac_callbacks

app = Dash(__name__, assets_folder="seac_experiment_assets", title="SEAC experiment | Today ±90 days")
app.layout = builder_layout
register_seac_callbacks(app)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8057)
    args = parser.parse_args()
    app.run(host="127.0.0.1", port=args.port, debug=False)

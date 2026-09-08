"""Plot an hourly CL minus CO outright-contract spread."""

import argparse
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go

from analysis.contract_universe import load_universe
from market_data import MarketData


def hourly_prices(database, product, expression):
    minute = database.synthetic(product, expression).dropna(
        subset=["timestamp", "price"]
    )
    if minute.empty:
        raise ValueError(f"No minute data available for {product} {expression}")
    minute["timestamp"] = pd.to_datetime(minute["timestamp"], utc=True)
    return (minute.sort_values("timestamp").set_index("timestamp")["price"]
            .resample("1h").last().dropna())


def cl_co_hourly(expression):
    database = MarketData()
    try:
        cl = hourly_prices(database, "CL", expression)
        co = hourly_prices(database, "LCO", expression)
    finally:
        database.close()
    aligned = pd.concat({"CL": cl, "CO": co}, axis=1, join="inner").dropna()
    if aligned.empty:
        raise ValueError(f"CL and CO have no common hourly data for {expression}")
    aligned["CL - CO"] = aligned["CL"] - aligned["CO"]
    return aligned


def make_figure(frame, expression):
    figure = go.Figure(go.Scatter(
        x=frame.index, y=frame["CL - CO"], mode="lines",
        name="CL - CO", line={"color": "#38bdf8", "width": 1.5},
        customdata=frame[["CL", "CO"]],
        hovertemplate=("%{x}<br>CL: %{customdata[0]:.4f}"
                       "<br>CO: %{customdata[1]:.4f}"
                       "<br>CL - CO: %{y:.4f}<extra></extra>"),
    ))
    figure.add_hline(y=0, line_color="#94a3b8", line_dash="dot")
    figure.update_layout(
        title=f"Hourly CL - CO | {expression}",
        xaxis_title="Time", yaxis_title="CL - CO price spread",
        template="plotly_dark", paper_bgcolor="#020617", plot_bgcolor="#020617",
        hovermode="x unified", height=750,
    )
    return figure


def main():
    universe = load_universe()
    first_contract = next(iter(universe["contracts"]))
    default_expression = next(iter(universe["contracts"][first_contract].values()))
    parser = argparse.ArgumentParser(description="Plot the hourly CL minus CO spread")
    parser.add_argument("--expression", default=default_expression)
    parser.add_argument("--output-dir", default="data/plots")
    arguments = parser.parse_args()

    frame = cl_co_hourly(arguments.expression)
    figure = make_figure(frame, arguments.expression)
    output_dir = Path(arguments.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    safe_expression = (arguments.expression.replace("*", "x").replace("+", "p")
                       .replace("-", "m"))
    output = output_dir / f"CL_CO_{safe_expression}_hourly.html"
    figure.write_html(output, include_plotlyjs="cdn")
    print(f"Saved {len(frame):,} aligned hourly observations to {output}")
    figure.show()


if __name__ == "__main__":
    main()

"""Print the best unique settings per symbol and bucket."""

import argparse
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from testing_parameters_dashboard import PARAMETERS, RANKINGS, aggregate_bucket, load_results


MINIMUM_TRADES = (500, 1000)
MINIMUM_SWING_TICKS = ( 4, 6)
# BUCKETS = ("1MDF", "1MDDF")
BUCKETS = ("1MDDF",)
BEST_PER_SEARCH = 3


def describe(row):
    settings = " | ".join(
        f"{parameter}={row[parameter]:g}" for parameter in PARAMETERS
    )
    performance = (
        f"win={row['win_rate']:.1%} | robust={row['wilson_lower_95']:.1%} | "
        f"trades={row['trades']:,.0f} | pnl={row['total_pnl_ticks']:+,.0f} ticks | "
        f"profitable_years={row['profitable_year_rate']:.1%}"
    )
    return f"{settings} | {performance}"


def print_bucket(raw, symbol, bucket):
    results = aggregate_bucket(raw, bucket, symbol)
    selected_rows = []
    searches = len(MINIMUM_TRADES) * len(MINIMUM_SWING_TICKS) * len(RANKINGS)
    print(f"\n{'=' * 110}\n{symbol} {bucket}: best {BEST_PER_SEARCH} from "
          f"each of {searches} searches\n{'=' * 110}")
    number = 0
    for minimum_trades in MINIMUM_TRADES:
        for minimum_swing in MINIMUM_SWING_TICKS:
            eligible = results[
                (results["trades"] >= minimum_trades)
                & (results["minimum_swing_ticks"] == minimum_swing)
            ]
            for ranking_label, ranking_column in RANKINGS.items():
                number += 1
                heading = (f"[{number:02d}/{searches}] minimum trades={minimum_trades:,} | "
                           f"minimum swing ticks={minimum_swing} | rank={ranking_label}")
                if eligible.empty:
                    print(f"\n{heading}\n  NO QUALIFYING SETTING")
                    continue
                best_rows = eligible.sort_values(
                    [ranking_column, "win_rate", "total_pnl_ticks", "trades"],
                    ascending=False,
                ).drop_duplicates(PARAMETERS).head(BEST_PER_SEARCH)
                print(f"\n{heading}")
                for position, (_, best) in enumerate(best_rows.iterrows(), 1):
                    print(f"  #{position} {describe(best)}")
                    selected_rows.append({
                        "symbol": symbol, "bucket": bucket,
                        "trade_threshold": minimum_trades,
                        "ranking": ranking_label, **best.to_dict(),
                    })
    unique = (pd.DataFrame(selected_rows).drop_duplicates(
        ["symbol", "bucket", *PARAMETERS]
    ) if selected_rows else pd.DataFrame())
    print(f"\n{symbol} {bucket}: {len(unique)} unique settings retained")
    return unique.to_dict("records")


def plot_shortlist(rows, output):
    selected = pd.DataFrame(rows)
    if selected.empty:
        print("\nNo qualifying settings exist, so no plot was created.")
        return
    unique = selected.groupby(["symbol", "bucket", *PARAMETERS], as_index=False).agg(
        selected_count=("ranking", "size"), win_rate=("win_rate", "first"),
        robust_win_rate=("wilson_lower_95", "first"), trades=("trades", "first"),
        total_pnl_ticks=("total_pnl_ticks", "first"),
    ).sort_values(["selected_count", "robust_win_rate"], ascending=False).reset_index(drop=True)
    unique["setting"] = [f"{symbol}-{bucket}-S{number:02d}"
                         for number, (symbol, bucket) in enumerate(
                             zip(unique["symbol"], unique["bucket"]), 1)]
    top = unique.head(20)
    common = []
    for (symbol, bucket), bucket_rows in selected.groupby(["symbol", "bucket"]):
        for parameter in PARAMETERS:
            for value, count in bucket_rows[parameter].value_counts().items():
                common.append({"symbol": symbol, "bucket": bucket,
                               "choice": f"{parameter.replace('_', ' ').title()} = {value:g}",
                               "count": count})
    common = pd.DataFrame(common)
    figure = make_subplots(
        rows=4, cols=1, vertical_spacing=0.07,
        specs=[[{"type": "xy"}], [{"type": "xy"}], [{"type": "xy"}],
               [{"type": "table"}]],
        row_heights=[0.2, 0.28, 0.22, 0.3],
        subplot_titles=("Most repeatedly selected settings",
                        "Parameter choices appearing most often among winners",
                        "Win rate versus total PnL", "Top 20 settings — full details"),
    )
    colors = {("CL", "1MDF"): "#8b5cf6", ("CL", "1MDDF"): "#06b6d4",
              ("CO", "1MDF"): "#f97316", ("CO", "1MDDF"): "#22c55e"}
    for (symbol, bucket), color in colors.items():
        name = f"{symbol} {bucket}"
        bucket_rows = top[(top["symbol"] == symbol) & (top["bucket"] == bucket)]
        figure.add_trace(go.Bar(
            x=bucket_rows["setting"], y=bucket_rows["selected_count"], name=name,
            marker_color=color,
            customdata=bucket_rows[["win_rate", "robust_win_rate", "trades",
                                    "total_pnl_ticks"]],
            hovertemplate=("%{x}<br>Selected: %{y} times<br>Win: %{customdata[0]:.1%}"
                           "<br>Robust: %{customdata[1]:.1%}<br>Trades: %{customdata[2]:,.0f}"
                           "<br>PnL: %{customdata[3]:+,.0f} ticks<extra></extra>"),
        ), row=1, col=1)
        common_rows = common[(common["symbol"] == symbol) &
                             (common["bucket"] == bucket)]
        figure.add_trace(go.Bar(
            x=common_rows["choice"], y=common_rows["count"], name=name,
            marker_color=color, opacity=0.8, showlegend=False,
            hovertemplate="%{x}<br>Appeared in %{y} winning searches<extra></extra>",
        ), row=2, col=1)
        points = unique[(unique["symbol"] == symbol) & (unique["bucket"] == bucket)]
        details = points[PARAMETERS].astype(str).agg(" | ".join, axis=1)
        figure.add_trace(go.Scatter(
            x=points["win_rate"], y=points["total_pnl_ticks"], mode="markers+text",
            text=points["setting"], textposition="top center", name=name,
            marker={"color": color, "size": 8 + points["selected_count"] * 3,
                    "opacity": 0.75, "line": {"color": "white", "width": 1}},
            customdata=list(zip(points["selected_count"], points["trades"], details)),
            hovertemplate=("%{text}<br>Win rate: %{x:.1%}<br>PnL: %{y:+,.0f} ticks"
                           "<br>Selected: %{customdata[0]} times"
                           "<br>Trades: %{customdata[1]:,.0f}<br>%{customdata[2]}<extra></extra>"),
        ), row=3, col=1)
    table_columns = ["setting", "symbol", "bucket", "selected_count", *PARAMETERS, "win_rate",
                     "robust_win_rate", "trades", "total_pnl_ticks"]
    display = top[table_columns].copy()
    display["win_rate"] = display["win_rate"].map(lambda value: f"{value:.1%}")
    display["robust_win_rate"] = display["robust_win_rate"].map(lambda value: f"{value:.1%}")
    figure.add_trace(go.Table(
        header={"values": [column.replace("_", " ").title() for column in table_columns],
                "fill_color": "#1e293b", "font": {"color": "white", "size": 11},
                "align": "center"},
        cells={"values": [display[column] for column in table_columns],
               "fill_color": "#0f172a", "font": {"color": "white", "size": 10},
               "align": "center", "height": 24},
    ), row=4, col=1)
    figure.update_layout(
        title=("Shortlisted CL Settings — bar height and marker size show how often a "
               "setting was selected"), template="plotly_dark", height=1900,
        paper_bgcolor="#020617", plot_bgcolor="#020617", hovermode="closest",
        legend={"orientation": "h", "x": 0.5, "xanchor": "center"},
    )
    figure.update_xaxes(title_text="Setting ID", row=1, col=1)
    figure.update_yaxes(title_text="Times selected", row=1, col=1)
    figure.update_xaxes(title_text="Parameter choice", tickangle=45, row=2, col=1)
    figure.update_yaxes(title_text="Winning searches", row=2, col=1)
    figure.update_xaxes(title_text="Win rate", tickformat=".0%", row=3, col=1)
    figure.update_yaxes(title_text="Total PnL (ticks)", row=3, col=1)
    figure.write_html(output, include_plotlyjs=True)
    print(f"\nSaved interactive shortlist plot to {output}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="data/testing_parameters")
    parser.add_argument("--output-dir", "--output", dest="output_dir",
                        default="data/best_parameters")
    arguments = parser.parse_args()
    raw = load_results(arguments.input)
    selected = []
    symbols = sorted(raw["symbol"].dropna().unique()) if "symbol" in raw else ["CL"]
    for symbol in symbols:
        for bucket in BUCKETS:
            selected.extend(print_bucket(raw, symbol, bucket))
    frame = pd.DataFrame(selected)
    if frame.empty:
        print("\nNo qualifying settings exist; no Excel file was created.")
        return
    frame = frame.rename(columns={"ranking": "ranking_method"})
    frame["required_minimum_swing_ticks"] = frame["minimum_swing_ticks"]
    frame["selected_win_rate"] = frame["win_rate"]
    frame["selected_robust_win_rate"] = frame["wilson_lower_95"]
    frame["selected_trades"] = frame["trades"]
    frame["selected_total_pnl_ticks"] = frame["total_pnl_ticks"]
    frame["status"] = "READY"
    frame["use_currently"] = 0
    frame["shortlist_case"] = frame.groupby(["symbol", "bucket"]).cumcount() + 1
    first = ["symbol", "bucket", "shortlist_case", "trade_threshold",
             "required_minimum_swing_ticks", "ranking_method"]
    frame = frame[first + [column for column in frame if column not in first]]
    destination = Path(arguments.output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    for (symbol, bucket), rows in frame.groupby(["symbol", "bucket"]):
        output = destination / f"{symbol}_{bucket}.xlsx"
        with pd.ExcelWriter(output, engine="openpyxl") as writer:
            rows.to_excel(writer, sheet_name="All_Settings", index=False)
            rows.to_excel(writer, sheet_name=f"{symbol}_{bucket}", index=False)
        print(f"\nSaved {len(rows)} unique settings to {output}")


if __name__ == "__main__":
    main()

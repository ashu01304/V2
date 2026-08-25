import argparse
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from analysis.seac_patterns import scan_trade_suggestions


MODELS = ["LightGBM", "Extra Trees", "Logistic", "HDBSCAN", "Ensemble"]


def build_report(product, years, horizon, stop_before_expiry,
                 rollover_weight, workers, output=None):
    suggestions = scan_trade_suggestions(
        product=product, years=years, horizon=horizon,
        stop_before_expiry=stop_before_expiry,
        max_rollover_weight=rollover_weight / 100,
        workers=workers,
        progress=lambda done, total, contract, _: print(
            f"Processed {done}/{total} contract families: {contract}", flush=True
        ),
    )
    if suggestions.empty:
        raise RuntimeError("No expressions produced enough history for a report")

    prediction_rows = []
    for _, suggestion in suggestions.iterrows():
        for detail in suggestion.get("_Backtest_Details", []):
            prediction_rows.append({
                "Product": product, "Contract": suggestion["Contract"],
                "Structure": suggestion["Structure"],
                "Expression": suggestion["Expression"], **detail,
            })
    predictions = pd.DataFrame(prediction_rows)
    if predictions.empty:
        raise RuntimeError("No unseen-year predictions were available")

    model_summary = _model_summary(predictions)
    by_year = _grouped_accuracy(predictions, "Test_Year")
    by_expression = _grouped_accuracy(predictions, "Expression")
    calibration = _calibration(predictions)
    current = suggestions.drop(columns=["_Backtest_Details"], errors="ignore")
    configuration = pd.DataFrame({
        "Parameter": ["Product", "History years", "Prediction horizon",
                      "Stop before expiry", "Maximum rollover influence",
                      "Parallel workers", "Generated"],
        "Value": [product, years, horizon, stop_before_expiry,
                  f"{rollover_weight}%", workers,
                  datetime.now().isoformat(timespec="seconds")],
    })
    methodology = pd.DataFrame({"Notes": [
        "Models train only on years earlier than each test year.",
        "The last three eligible years use expanding historical training.",
        "Accuracy measures direction over the selected forward horizon.",
        "Brier score measures probability quality; lower is better.",
        "Model Move applies the predicted direction to the actual price move.",
        "Predictions overlap, so this is a forecast backtest, not position-level PnL.",
        "Transaction costs and execution slippage are not included.",
        "Rollover affects current suggestions but is not independently backtested yet.",
    ]})

    path = Path(output) if output else Path("backtest_reports") / (
        f"seac_model_backtest_{product}_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        model_summary.to_excel(writer, sheet_name="Model_Summary", index=False)
        by_year.to_excel(writer, sheet_name="By_Year", index=False)
        by_expression.to_excel(writer, sheet_name="By_Expression", index=False)
        calibration.to_excel(writer, sheet_name="Calibration", index=False)
        predictions.to_excel(writer, sheet_name="Predictions", index=False)
        current.to_excel(writer, sheet_name="Current_Suggestions", index=False)
        configuration.to_excel(writer, sheet_name="Configuration", index=False)
        methodology.to_excel(writer, sheet_name="Methodology", index=False)
    return path.resolve()


def _model_summary(predictions):
    rows = []
    actual_up = predictions["Actual_Direction"].eq("UP")
    for model in MODELS:
        probability = predictions[f"{model}_Probability_Up"] / 100
        predicted_up = probability >= 0.5
        correct = predicted_up.eq(actual_up)
        signed_move = np.where(predicted_up, predictions["Actual_Move"],
                               -predictions["Actual_Move"])
        cumulative = pd.Series(signed_move).cumsum()
        drawdown = cumulative - cumulative.cummax()
        rows.append({
            "Model": model, "Predictions": len(predictions),
            "Accuracy": correct.mean() * 100,
            "Long_Precision": actual_up[predicted_up].mean() * 100,
            "Short_Precision": (~actual_up[~predicted_up]).mean() * 100,
            "Brier_Score": ((probability - actual_up.astype(int)) ** 2).mean(),
            "Average_Model_Move": np.mean(signed_move),
            "Total_Model_Move": np.sum(signed_move),
            "Maximum_Drawdown": drawdown.min(),
        })
    return pd.DataFrame(rows)


def _grouped_accuracy(predictions, group):
    rows = []
    for value, frame in predictions.groupby(group):
        row = {group: value, "Predictions": len(frame)}
        for model in MODELS:
            row[f"{model}_Accuracy"] = frame[f"{model}_Correct"].mean() * 100
        rows.append(row)
    return pd.DataFrame(rows)


def _calibration(predictions):
    rows = []
    actual = predictions["Actual_Direction"].eq("UP")
    bins = [0, 40, 50, 60, 70, 80, 90, 101]
    for model in MODELS:
        probability = predictions[f"{model}_Probability_Up"]
        groups = pd.cut(probability, bins=bins, right=False)
        for bucket, indexes in predictions.groupby(groups, observed=True).groups.items():
            rows.append({"Model": model, "Probability_Bucket": str(bucket),
                         "Predictions": len(indexes),
                         "Actual_Up_Rate": actual.loc[indexes].mean() * 100})
    return pd.DataFrame(rows)


def main():
    parser = argparse.ArgumentParser(description="Create a detailed SEAC ML backtest report")
    parser.add_argument("--product", default="CL")
    parser.add_argument("--years", type=int, default=10)
    parser.add_argument("--horizon", type=int, default=10, choices=range(1, 16))
    parser.add_argument("--stop-before-expiry", type=int, default=42)
    parser.add_argument("--rollover-weight", type=float, default=30)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--output")
    args = parser.parse_args()
    path = build_report(args.product, args.years, args.horizon,
                        args.stop_before_expiry, args.rollover_weight,
                        args.workers, args.output)
    print(f"Report saved: {path}")


if __name__ == "__main__":
    main()

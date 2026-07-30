from pathlib import Path

import pandas as pd


def seasonality_coverage(result):
    return pd.DataFrame({
        year: {
            "observations": len(series),
            "first_date": series["date"].min(),
            "last_date": series["date"].max(),
            "first_offset": series.index.min(),
            "last_offset": series.index.max(),
        }
        for year, series in result["series"].items()
    }).T


def export_seasonality_result(result, output_file, coverage=None):
    output_file = Path(output_file)
    coverage = seasonality_coverage(result) if coverage is None else coverage

    with pd.ExcelWriter(output_file) as writer:
        result["combined"].to_excel(writer, sheet_name="combined_interpolated")
        result.get("raw_combined", result["combined"]).to_excel(writer, sheet_name="combined_raw")
        result["average"].rename("average").to_excel(writer, sheet_name="average")
        coverage.to_excel(writer, sheet_name="coverage")

    return output_file

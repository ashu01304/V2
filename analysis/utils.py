import numpy as np


BRACKETS = {
    "5Y": {"window": 5, "stats_min": 0, "anomaly_min": 4, "anomaly_budget": 1},
    "10Y": {"window": 10, "stats_min": 6, "anomaly_min": 6, "anomaly_budget": 2},
    "15Y": {"window": 15, "stats_min": 11, "anomaly_min": 11, "anomaly_budget": 3},
}


def year_columns(df, last_n=None):
    years = [
        column for column in df.columns
        if isinstance(column, (int, np.integer))
        or isinstance(column, str) and len(column) == 4 and column.isdigit()
    ]
    years.sort(key=int)
    return years[-last_n:] if last_n is not None else years

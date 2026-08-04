import json
import pandas as pd
import numpy as np
import math

def _calculate_weighted_value(yearly_map):
    years = sorted(yearly_map.keys())
    N = len(years)
    if N == 0: return 0

    K = math.ceil(0.2 * N)
    base_weight = 1.0 / (N + K)

    weighted_val = 0
    recent_start_idx = N - K
    for i, year in enumerate(years):
        weight = (2 * base_weight) if i >= recent_start_idx else base_weight
        weighted_val += yearly_map[year] * weight
    return weighted_val

def get_stability_metrics(sznlty, symbol, expression, start_year, end_year, window_days, 
                           win_len=20, step=10, cutoff=63, threshold=None):
    result = sznlty.working_day_expression_seasonality(
        symbol, expression, start_year=start_year, end_year=end_year, window_days=window_days
    )

    df_data = result['combined']
    year_cols = [c for c in df_data.columns if str(c).isdigit() and len(str(c)) == 4]
    df_clean = df_data[year_cols][df_data.index <= -cutoff].sort_index()

    windowed_changes = []
    yearly_raw_changes = {year: [] for year in year_cols}
    all_indices = df_clean.index.tolist()
    current_start = min(all_indices)

    while current_start + win_len <= max(all_indices):
        current_end = current_start + win_len
        slice_df = df_clean.loc[current_start:current_end]
        label = f"{int(current_start)} to {int(current_end)}"
        
        for year in year_cols:
            s = slice_df[year].dropna()
            if len(s) >= 2:
                change = abs(s.iloc[-1] - s.iloc[0])
                windowed_changes.append({'Year': year, 'Window': label, 'Change': change})
                yearly_raw_changes[year].append(change)
        current_start += step

    if threshold is None:
        user_vals, mad_vals = {}, {}
        for year, changes in yearly_raw_changes.items():
            if not changes: continue

            limit = np.percentile(changes, 85)
            filtered = [x for x in changes if x <= limit]
            user_vals[year] = np.std(filtered) if filtered else 0
            
            med = np.median(changes)
            mad_vals[year] = np.median([abs(x - med) for x in changes])
        weighted_user = _calculate_weighted_value(user_vals)
        weighted_mad = _calculate_weighted_value(mad_vals)
        threshold = max(2 * min(weighted_user, weighted_mad), 0.055)

    change_table = pd.DataFrame(windowed_changes).pivot(index='Window', columns='Year', values='Change')
    change_table = change_table.sort_index(key=lambda x: [int(i.split(' ')[0]) for i in x])
    stability_scores = ((change_table < threshold).sum() / change_table.count()) * 100
    scores_asc = stability_scores.sort_index()
    latest_score = scores_asc.iloc[-1]
    trend_score = _calculate_weighted_value(scores_asc.iloc[:-1].to_dict())

    stable = change_table < threshold
    valid = change_table.notna() & change_table.shift(1).notna() & change_table.shift(-1).notna()
    neighboring_scores = ((stable.shift(1) & stable & stable.shift(-1)).where(valid).sum()
                          / valid.sum().replace(0, np.nan)) * 100
    neighboring_score = _calculate_weighted_value(neighboring_scores.iloc[:-1].dropna().to_dict())

    return {
        "raw_result": result,
        "change_table": change_table,
        "stability_scores": stability_scores,
        "neighboring_scores": neighboring_scores,
        "latest_score": round(latest_score, 1),
        "trend_score": round(trend_score, 1),
        "neighboring_score": round(neighboring_score, 1),
        "calculated_threshold": round(threshold, 4)
    }
def score_expression_universe(sznlty, symbol, start_year, end_year, window_days,
                              score_function=get_stability_metrics,
                              expressions_file="contracts_list.json", output_file="temp.xlsx"):
    with open(expressions_file, encoding="utf-8") as file:
        expressions = json.load(file)

    scores, errors = [], []
    for expression in expressions:
        try:
            metrics = score_function(sznlty, symbol, expression, start_year, end_year, window_days)
            scores.append({"Expression": expression,
                           **metrics["stability_scores"].round(1).to_dict(),
                           "Latest_Score": metrics["latest_score"],
                           "Trend_Score": metrics["trend_score"],
                           "Neighboring_Buckets_Score": metrics["neighboring_score"],
                           "Calculated_Threshold": metrics["calculated_threshold"]})
        except Exception as error:
            errors.append({"Expression": expression, "Error": str(error)})

    score_table = pd.DataFrame(scores)
    with pd.ExcelWriter(output_file, engine="openpyxl") as writer:
        score_table.to_excel(writer, index=False)
        column = score_table.columns.get_loc("Calculated_Threshold") + 1
        for cell in writer.sheets["Sheet1"].iter_cols(min_col=column, max_col=column, min_row=2):
            cell[0].number_format = "0.000"
    return score_table, pd.DataFrame(errors)

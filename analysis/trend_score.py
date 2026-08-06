import json
import pandas as pd
import numpy as np
import math
import re


def _validate_expression_history(sznlty, symbol, expression, start_year, end_year):
    matches = re.findall(r"([FGHJKMNQUVXZ])(\d{2})", expression)
    if not matches:
        return False, "could not parse expression"

    ref_month, ref_yy = matches[0]
    ref_year_in_expr = int(ref_yy)
    years = sznlty._contract_years(symbol, ref_month, start_year, end_year)
    if not years:
        return False, "no anchor contracts found in the requested year range"

    for year in years:
        leg_codes = {
            f"{month}{(year + int(yy) - ref_year_in_expr) % 100:02d}"
            for month, yy in matches
        }
        histories = sznlty.db.get_contract_history(symbol, sorted(leg_codes))
        missing = sorted(
            code for code in leg_codes
            if code not in histories or histories[code].empty
        )
        if missing:
            return False, f"{year}: missing data for leg(s) {', '.join(missing)}"

        common_dates = None
        for code in leg_codes:
            dates = histories[code].index
            common_dates = dates if common_dates is None else common_dates.intersection(dates)
        if common_dates is None or common_dates.empty:
            return False, f"{year}: no overlapping dates across legs"

    return True, None

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

    if df_clean.empty or not year_cols:
        raise ValueError("no usable complete-leg history for scoring")

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
        expression_data = json.load(file)

    expressions = [
        {"Contract": contract, "Strategy": strategy, "Expression": expression}
        for contract, strategies in expression_data["contracts"].items()
        for strategy in expression_data["columns"]
        for expression in [strategies[strategy]]
    ]

    scores, errors = [], []
    for item in expressions:
        contract = item["Contract"]
        strategy = item["Strategy"]
        expression = item["Expression"]
        try:
            valid, reason = _validate_expression_history(
                sznlty, symbol, expression, start_year, end_year
            )
            if not valid:
                errors.append({**item, "Error": reason})
                continue

            metrics = score_function(sznlty, symbol, expression, start_year, end_year, window_days)
            scores.append({"Contract": contract,
                           "Strategy": strategy,
                           "Expression": expression,
                           **metrics["stability_scores"].round(1).to_dict(),
                           "Latest_Score": metrics["latest_score"],
                           "Trend_Score": metrics["trend_score"],
                           "Neighboring_Buckets_Score": metrics["neighboring_score"],
                           "Calculated_Threshold": metrics["calculated_threshold"]})
        except Exception as error:
            errors.append({**item, "Error": str(error)})

    score_table = pd.DataFrame(scores)
    with pd.ExcelWriter(output_file, engine="openpyxl") as writer:
        score_table.to_excel(writer, index=False)
        if "Calculated_Threshold" in score_table.columns:
            column = score_table.columns.get_loc("Calculated_Threshold") + 1
            for cell in writer.sheets["Sheet1"].iter_cols(min_col=column, max_col=column, min_row=2):
                cell[0].number_format = "0.000"
    return score_table, pd.DataFrame(errors)

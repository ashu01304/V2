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
    # 1. Fetch Seasonality Data
    result = sznlty.working_day_expression_seasonality(
        symbol, expression, start_year=start_year, end_year=end_year, window_days=window_days
    )
    
    # 2. Extract Data
    df_data = result['combined']
    year_cols = [c for c in df_data.columns if str(c).isdigit() and len(str(c)) == 4]
    df_clean = df_data[year_cols][df_data.index <= -cutoff].sort_index()

    # 3. Window Calculations
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

    # 4. Final Dynamic Threshold Logic
    if threshold is None:
        user_vals, mad_vals = {}, {}
        for year, changes in yearly_raw_changes.items():
            if not changes: continue
            
            # User Method Component (85%ile filter -> SD)
            limit = np.percentile(changes, 85)
            filtered = [x for x in changes if x <= limit]
            user_vals[year] = np.std(filtered) if filtered else 0
            
            # MAD Method Component (Median Absolute Deviation)
            med = np.median(changes)
            mad_vals[year] = np.median([abs(x - med) for x in changes])
        
        weighted_user = _calculate_weighted_value(user_vals)
        weighted_mad = _calculate_weighted_value(mad_vals)
        
        threshold = max(2 * min(weighted_user, weighted_mad), 0.055)
        # print(f"ℹ️ Dynamic threshold calculated: max(2 * min({weighted_user:.4f}, {weighted_mad:.4f}), 0.055) = {threshold:.4f}")

    # 5. Stability & Scores
    change_table = pd.DataFrame(windowed_changes).pivot(index='Window', columns='Year', values='Change')
    change_table = change_table.sort_index(key=lambda x: [int(i.split(' ')[0]) for i in x])
    
    stability_scores = ((change_table < threshold).sum() / change_table.count()) * 100
    
    # 6. Trend Score (Excluding Latest Year)
    scores_asc = stability_scores.sort_index()
    latest_score = scores_asc.iloc[-1]
    
    # Historical trend uses the same 20% weighting logic on all years EXCEPT the latest
    historical_dict = scores_asc.iloc[:-1].to_dict()
    trend_score = _calculate_weighted_value(historical_dict)

    return {
        "raw_result": result,
        "change_table": change_table,
        "stability_scores": stability_scores,
        "latest_score": round(latest_score, 1),
        "trend_score": round(trend_score, 1),
        "calculated_threshold": round(threshold, 4)
    }
import pandas as pd

def export_to_excel(result, df_enhanced, filename="seasonality_analysis.xlsx"):
    try:
        df_export = df_enhanced.copy()
        df_export.index.name = "Days to Expiry"

        series_data = result.get('series', {})
        if series_data:
            current_year = max(series_data.keys())
            current_date_map = series_data[current_year].get('date')
            
            if current_date_map is not None:
                df_export.insert(0, 'Current_Year_Date', current_date_map)
                df_export['Current_Year_Date'] = pd.to_datetime(df_export['Current_Year_Date']).dt.strftime('%Y-%m-%d')

        insert_at = 1 if 'Current_Year_Date' in df_export.columns else 0
        df_export.insert(insert_at, 'STAT_Average', result.get('average', pd.Series(dtype=float)))
        df_export.insert(insert_at + 1, 'STAT_Std_Dev', result.get('std', pd.Series(dtype=float)))
        df_export.insert(insert_at + 2, 'STAT_Rolling_2Sigma_Path', result.get('rolling_std_path', pd.Series(dtype=float)))

        df_export = df_export.sort_index()

        with pd.ExcelWriter(filename, engine='openpyxl') as writer:
            df_export.to_excel(writer, sheet_name='Price Alignment')
            
            warnings = result.get('warnings', [])
            if warnings:
                pd.DataFrame(warnings, columns=['Warnings']).to_excel(writer, sheet_name='Metadata', index=False)
        
        print(f"✅ Data successfully exported to {filename}")

    except Exception as e:
        print(f"❌ Failed to export Excel: {e}")

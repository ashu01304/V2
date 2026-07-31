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

        stat_cols = ['STAT_Average', 'STAT_Std_Dev', 'STAT_Rolling_2S']
        year_cols = [col for col in df_export.columns
            if isinstance(col, int)
        ]
        other_cols = [ col for col in df_export.columns
            if col not in year_cols + stat_cols
            and col != 'Current_Year_Date'
        ]
        column_order = []

        if 'Current_Year_Date' in df_export.columns:
            column_order.append('Current_Year_Date')

        column_order += year_cols + stat_cols + other_cols
        df_export = df_export[column_order]

        df_export = df_export.sort_index()

        with pd.ExcelWriter(filename, engine='openpyxl') as writer:
            df_export.to_excel(writer, sheet_name='Price Alignment')
            
            warnings = result.get('warnings', [])
            if warnings:
                pd.DataFrame(warnings, columns=['Warnings']).to_excel(writer, sheet_name='Metadata', index=False)
        
        print(f"✅ Data successfully exported to {filename}")

    except Exception as e:
        print(f"❌ Failed to export Excel: {e}")

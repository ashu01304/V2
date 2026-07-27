import plotly.graph_objects as go

class SeasonalityPlotter:
    def plot(self, data_dict, title="Seasonality"):
        fig = go.Figure()
        colors = ['#636EFA', '#EF553B', '#00CC96', '#AB63FA', '#FFA15A', '#19D3F3']
        
        for i, (label, df) in enumerate(data_dict.items()):
            is_avg = str(label).upper() == "AVERAGE"
            fig.add_trace(go.Scatter(
                x=df.index, y=df['Close'], name=str(label),
                line=dict(width=4 if is_avg else 2, color='white' if is_avg else colors[i%len(colors)]),
                hovertemplate=f"{label}<br>Date: %{{x|%b %d}}<br>Price: %{{y:.2f}}<extra></extra>"
            ))

        fig.update_layout(
            template="plotly_dark", paper_bgcolor='black', plot_bgcolor='black',
            xaxis=dict(tickformat='%b %d', gridcolor='#333'),
            yaxis=dict(title="Price", gridcolor='#333'),
            hovermode="x unified", title=title
        )
        fig.show()

    def plot_seasonality(self, result, title="Seasonality"):
        warnings = result.get('warnings', [])
        for w in warnings:
            print(f"⚠️ {w}")

        series, average = result['series'], result['average']
        if not series:
            print("Nothing to plot.")
            return

        tickvals, ticktext = result['ticks']
        colors = ['#636EFA', '#EF553B', '#00CC96', '#AB63FA', '#FFA15A',
                  '#19D3F3', '#FF6692', '#B6E880', '#FF97FF', '#FECB52']

        current_year = max(series.keys())

        fig = go.Figure()
        color_i = 0
        for year, df in sorted(series.items()):
            is_current = (year == current_year)
            fig.add_trace(go.Scatter(
                x=df.index, y=df['value'], name=str(year),
                mode='lines',
                line=dict(width=1.5, color='white' if is_current else colors[color_i % len(colors)]),
                customdata=df['date'].dt.strftime('%Y-%m-%d'),
                hovertemplate=f"{year}<br>Date: %{{customdata}}<br>Value: %{{y:.2f}}<extra></extra>"
            ))
            if not is_current:
                color_i += 1

        if not average.empty:
            fig.add_trace(go.Scatter(
                x=average.index, y=average.values, name='AVERAGE',
                mode='lines',
                line=dict(width=1.5, color='#00CC96'),
                hovertemplate="AVERAGE<br>Days to expiry: %{x}<br>Value: %{y:.2f}<extra></extra>"
            ))

        fig.update_layout(
            template="plotly_dark", paper_bgcolor='black', plot_bgcolor='black',
            title=title,
            xaxis=dict(title="Month before expiry", tickvals=tickvals, ticktext=ticktext, gridcolor='#333'),
            yaxis=dict(title="Value", gridcolor='#333'),
            hovermode="x unified",
        )
        fig.show()
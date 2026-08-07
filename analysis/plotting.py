import plotly.graph_objects as go

class SeasonalityPlotter:
    def plot(self, data_dict, title="Seasonality"):
        fig = go.Figure()
        colors = ['#636EFA', '#EF553B', '#00CC96', '#AB63FA', '#FFA15A', '#19D3F3']
        
        for i, (label, df) in enumerate(data_dict.items()):
            is_avg = str(label).upper() == "AVERAGE"
            fig.add_trace(go.Scatter(
                x=df.index, y=df['Close'], name=str(label),
                line=dict(width=4 if is_avg else 2, color='white' if is_avg else colors[i % len(colors)]),
                hovertemplate=f"{label}<br>Date: %{{x|%b %d}}<br>Price: %{{y:.2f}}<extra></extra>"
            ))

        fig.update_layout(
            template="plotly_dark", paper_bgcolor='black', plot_bgcolor='black',
            xaxis=dict(tickformat='%b %d', gridcolor='#333'),
            yaxis=dict(title="Price", gridcolor='#333'),
            hovermode="x unified", title=title,
            height=900,
            width=900,
        )
        fig.show()

    def plot_seasonality(self, result, title="Seasonality"):
        fig = self.build_seasonality_figure(result, title)
        if fig is None:
            print("Nothing to plot.")
            return
        fig.show()

    def build_seasonality_figure(self, result, title="Seasonality"):
        for warning in result.get('warnings', []):
            print(f"⚠️ {warning}")

        series = result.get('series', {})
        average = result.get('average')
        std = result.get('std')
        rolling_2s = result.get('rolling_std_path')
        if not series and average is None:
            return None

        fig = go.Figure()
        def add_band(x, upper, lower, name, color):
            mask = upper.notna() & lower.notna()
            if not mask.any():
                return
            clean_x = x[mask]
            fig.add_trace(go.Scatter(
                x=list(clean_x) + list(clean_x)[::-1],
                y=list(upper[mask]) + list(lower[mask])[::-1],
                fill='toself',
                fillcolor=color,
                line=dict(color='rgba(255,255,255,0)'),
                hoverinfo="skip",
                showlegend=True,
                name=name
            ))

        if average is not None and not average.empty:
            if std is not None and not std.empty:
                add_band(average.index, average + std, average - std, '1σ Year Spread', 'rgba(255, 255, 255, 0.25)')
            if rolling_2s is not None and not rolling_2s.empty:
                add_band(average.index, average + rolling_2s, average - rolling_2s, '2σ Path Vol (30d)', 'rgba(0, 204, 150, 0.2)')

        colors = ['#636EFA', '#EF553B', '#00CC96', '#AB63FA', '#FFA15A', '#19D3F3', '#FF6692', '#B6E880', '#FF97FF', '#FECB52']
        current_year = max(series.keys()) if series else None
        color_i = 0
        for year, df in sorted(series.items()):
            is_current = year == current_year
            customdata = (
                df['date'].dt.strftime('%Y-%m-%d')
                if 'date' in df.columns
                else None
            )
            fig.add_trace(go.Scatter(
                x=df.index, y=df['value'], name=str(year),
                mode='lines',
                line=dict(width=2 if is_current else 1, color='white' if is_current else colors[color_i % len(colors)]),
                customdata=customdata,
                hovertemplate=(f"{year}<br>Date: %{{customdata}}""<br>Value: %{y:.2f}<extra></extra>"
                    if customdata is not None
                    else f"{year}<br>Value: %{{y:.2f}}<extra></extra>")
            ))
            if not is_current: color_i += 1

        if average is not None and not average.empty:
            fig.add_trace(go.Scatter(
                x=average.index, y=average.values, name='AVERAGE',
                mode='lines',
                line=dict(width=3, color='#00CC96'),
                hovertemplate=("AVERAGE<br>Days to expiry: %{x}""<br>Value: %{y:.2f}<extra></extra>")
            ))

        fig.update_layout(
            template="plotly_dark", paper_bgcolor='black', plot_bgcolor='black',
            title=title,
            xaxis=dict(title=result.get("xaxis_title", "Month before expiry"),
                       tickmode="auto", nticks=6, gridcolor='#333'),
            yaxis=dict(title="Value", gridcolor='#333'),
            hovermode="x unified",
            height=900, width=1600,
        )
        return fig

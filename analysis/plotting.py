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
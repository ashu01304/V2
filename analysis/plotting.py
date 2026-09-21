import plotly.graph_objects as go

from analysis.expression import parse_expression


class OHLCPlotter:
    def plot(self, data, chart_type="candlestick", title=None, height=800,
             x_axis="candle_number", show=True, bollinger_window=20,
             bollinger_std=2, bollinger_method="simple", levels=None):
        if not data:
            print("No OHLC data available to plot.")
            return

        fig = go.Figure()
        chart_type = chart_type.lower()
        for instrument, df in data.items():
            x = list(range(1 - len(df), 1)) if x_axis == "candle_number" else df.index
            timestamps = df.index.strftime("%Y-%m-%d %H:%M:%S")
            if chart_type == "candlestick":
                fig.add_trace(go.Candlestick(
                    x=x, open=df["Open"], high=df["High"],
                    low=df["Low"], close=df["Close"], name=instrument,
                    text=timestamps, hoverinfo="name+x+y+text",
                ))
            elif chart_type == "line":
                fig.add_trace(go.Scatter(
                    x=x, y=df["Close"], mode="lines", name=instrument,
                    customdata=timestamps,
                    hovertemplate=(f"{instrument}<br>Candle: %{{x}}<br>Time: %{{customdata}}"
                                   "<br>Close: %{y}<extra></extra>"),
                ))
            else:
                raise ValueError("chart_type must be 'candlestick' or 'line'")

            close = df["Close"]
            if bollinger_method == "ewm":
                weighted = close.ewm(span=bollinger_window, adjust=False,
                                     min_periods=bollinger_window)
                middle, deviation = weighted.mean(), weighted.std() * bollinger_std
            else:
                middle = close.rolling(bollinger_window).mean()
                deviation = close.rolling(bollinger_window).std() * bollinger_std
            for values, label, dash in (
                (middle + deviation, "Upper", "dot"),
                (middle, "Average", "dash"),
                (middle - deviation, "Lower", "dot"),
            ):
                fig.add_trace(go.Scatter(
                    x=x, y=values, mode="lines", name=f"{instrument} BB {label}",
                    line=dict(width=1, dash=dash), hoverinfo="skip",
                ))

            for label, price in (levels or {}).get(instrument, {}).items():
                fig.add_trace(go.Scatter(
                    x=x[-100:], y=[price] * min(100, len(x)), mode="lines",
                    name=f"{instrument} {label}", hoverinfo="skip",
                    line=dict(color="#00CC96" if label == "Support" else "#EF553B",
                              width=2, dash="dash"),
                ))

        fig.update_layout(
            template="plotly_dark", paper_bgcolor="black", plot_bgcolor="black",
            title=title or "OHLC Price Chart",
            xaxis=dict(title="Candle number from latest" if x_axis == "candle_number" else "Date",
                       gridcolor="#333", rangeslider_visible=False, showspikes=True,
                       spikemode="across", spikesnap="cursor",
                       spikecolor="white", spikethickness=1),
            yaxis=dict(title="Price", gridcolor="#333", showspikes=True,
                       spikemode="across", spikesnap="cursor",
                       spikecolor="white", spikethickness=1),
            hovermode="closest",
            height=height,
        )
        if show:
            fig.show()
        return fig

class SeasonalityPlotter:
    @staticmethod
    def _add_live_marker(fig, data):
        if "is_live" not in data:
            return
        live = data[data["is_live"]]
        if live.empty:
            return
        fig.add_trace(go.Scatter(
            x=[live.index[-1]], y=[live["value"].iloc[-1]], mode="markers",
            marker=dict(size=7, color="#22c55e",
                        line=dict(color="white", width=1)),
            name="Live price",
            hovertemplate="Live price<br>Value: %{y}<extra></extra>",
        ))

    def plot(self, data_dict, title="Seasonality"):
        fig = go.Figure()
        colors = ['#636EFA', '#EF553B', '#00CC96', '#AB63FA', '#FFA15A', '#19D3F3']
        
        for i, (label, df) in enumerate(data_dict.items()):
            is_avg = str(label).upper() == "AVERAGE"
            fig.add_trace(go.Scatter(
                x=df.index, y=df['Close'], name=str(label),
                line=dict(width=4 if is_avg else 2, color='white' if is_avg else colors[i % len(colors)]),
                hovertemplate=f"{label}<br>Date: %{{x|%b %d}}<br>Price: %{{y}}<extra></extra>"
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

    def build_seasonality_figure(self, result, title="Seasonality", height=800):
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
                hovertemplate=(f"{year}<br>Date: %{{customdata}}""<br>Value: %{y}<extra></extra>"
                    if customdata is not None
                    else f"{year}<br>Value: %{{y}}<extra></extra>")
            ))
            if is_current:
                self._add_live_marker(fig, df)
            if not is_current: color_i += 1

        if average is not None and not average.empty:
            fig.add_trace(go.Scatter(
                x=average.index, y=average.values, name='AVERAGE',
                mode='lines',
                line=dict(width=3, color='#00CC96'),
                hovertemplate=("AVERAGE<br>Days to expiry: %{x}""<br>Value: %{y}<extra></extra>")
            ))

        fig.update_layout(
            template="plotly_dark", paper_bgcolor='black', plot_bgcolor='black',
            title=title,
            xaxis=dict(title=result.get("xaxis_title", "Month before expiry"),
                       tickmode="auto", nticks=6, gridcolor='#333'),
            yaxis=dict(title="Value", gridcolor='#333'),
            hovermode="x unified",
            uirevision=f"seasonality:{title}",
            legend=dict(orientation="h", yanchor="bottom", y=1.02,
                        xanchor="left", x=0),
            margin=dict(l=55, r=20, t=95, b=50),
            height=height,
        )
        return fig

    def build_rollover_figure(self, result, title="Strategy Rollover", height=800):
        fig = go.Figure()
        colors = ['#636EFA', '#EF553B', '#00CC96', '#AB63FA', '#FFA15A', '#19D3F3']
        current_expiry_days = []
        for index, (contract, item) in enumerate(result.get("series", {}).items()):
            data = item["data"]
            active = item["active"]
            current_expiry_days.append(item["current_expiry_day"])
            fig.add_trace(go.Scatter(
                x=data.index, y=data["value"], mode="lines",
                name=parse_expression(contract)[0][1],
                line=dict(width=3 if active else 1.5,
                          color="white" if active else colors[index % len(colors)]),
                customdata=list(zip(data["date"].dt.strftime("%Y-%m-%d"), data["side"])),
                hovertemplate=("Year: %{fullData.name}<br>%{customdata[1]}"
                               "<br>Date: %{customdata[0]}"
                               "<br>Change: %{y}<extra></extra>"),
            ))
            if active:
                self._add_live_marker(fig, data)
        fig.add_vline(x=0, line_color="#94a3b8", annotation_text="Previous expiry")
        if current_expiry_days:
            fig.add_vline(x=max(current_expiry_days), line_color="#94a3b8",
                          annotation_text="Current expiry")
        fig.add_hline(y=0, line_color="#64748b", line_width=1)
        fig.update_layout(
            template="plotly_dark", paper_bgcolor="black", plot_bgcolor="black",
            title=title, xaxis=dict(title="Previous strategy ← rollover → Next strategy",
                                   gridcolor="#333"),
            yaxis=dict(title="Change from rollover", gridcolor="#333"),
            hovermode="x unified", uirevision=f"rollover:{title}",
            legend=dict(orientation="h", yanchor="bottom", y=1.02,
                        xanchor="left", x=0),
            margin=dict(l=55, r=20, t=100, b=55), height=height,
        )
        return fig

    def build_forward_curve_figure(self, result, symbol, expression, height=400):
        fig = go.Figure()
        points = result.get("live", [])
        labels = [point["label"] for point in points]
        selected = parse_expression(expression)[0][1]
        history_colors = ["rgba(148,163,184,0.70)", "rgba(203,213,225,0.35)"]
        for settlement, color in reversed(list(zip(
                result.get("settlements", []), history_colors))):
            history = settlement["points"]
            fig.add_trace(go.Scatter(
                x=[point["label"] for point in history],
                y=[point["price"] for point in history],
                mode="lines+markers", name=settlement["date"],
                line=dict(color=color, width=1.5), marker=dict(color=color, size=4),
                customdata=[point["contract"] for point in history],
                hovertemplate="%{customdata}<br>Settlement: %{y}<extra></extra>",
            ))
        if points:
            fig.add_trace(go.Scatter(
                x=labels,
                y=[point["price"] for point in points],
                mode="lines+markers", name="Live",
                line=dict(color="#38bdf8", width=2),
                marker=dict(
                    color=["#22c55e" if point["label"] == selected else "white"
                           for point in points],
                    size=[9 if point["label"] == selected else 6 for point in points],
                    line=dict(color="#0f172a", width=1),
                ),
                customdata=[point["contract"] for point in points],
                hovertemplate="%{customdata}<br>Price: %{y}<extra></extra>",
            ))
        else:
            fig.add_annotation(text="Forward prices unavailable", showarrow=False)
        fig.update_layout(
            template="plotly_dark", paper_bgcolor="black", plot_bgcolor="black",
            title=f"{symbol} {expression} Forward Curve",
            xaxis=dict(title="Shifted expression", gridcolor="#333",
                       type="category", categoryorder="array", categoryarray=labels,
                       tickmode="array", tickvals=labels[::2], tickangle=-35),
            yaxis=dict(title="Price", gridcolor="#333"), hovermode="x unified",
            uirevision=f"forward-curve:{symbol}:{expression}",
            legend=dict(orientation="h", yanchor="bottom", y=1.02,
                        xanchor="left", x=0),
            margin=dict(l=55, r=20, t=95, b=75), height=height,
        )
        return fig

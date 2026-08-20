import plotly.graph_objects as go


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
                hovertemplate=(f"{year}<br>Date: %{{customdata}}""<br>Value: %{y}<extra></extra>"
                    if customdata is not None
                    else f"{year}<br>Value: %{{y}}<extra></extra>")
            ))
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
            height=800,
        )
        return fig

    def build_rollover_figure(self, result, title="Strategy Rollover"):
        fig = go.Figure()
        colors = ['#636EFA', '#EF553B', '#00CC96', '#AB63FA', '#FFA15A', '#19D3F3']
        current_expiry_days = []
        for index, (contract, item) in enumerate(result.get("series", {}).items()):
            data = item["data"]
            active = item["active"]
            current_expiry_days.append(item["current_expiry_day"])
            fig.add_trace(go.Scatter(
                x=data.index, y=data["value"], mode="lines",
                name=str(contract),
                line=dict(width=3 if active else 1.5,
                          color="white" if active else colors[index % len(colors)]),
                customdata=list(zip(data["date"].dt.strftime("%Y-%m-%d"), data["side"])),
                hovertemplate=("Year: %{fullData.name}<br>%{customdata[1]}"
                               "<br>Date: %{customdata[0]}"
                               "<br>Change: %{y}<extra></extra>"),
            ))
            if active:
                latest = data.iloc[-1]
                fig.add_trace(go.Scatter(
                    x=[data.index[-1]], y=[latest["value"]], mode="markers",
                    marker=dict(size=8, color="white"), name="Latest available",
                    showlegend=False, hoverinfo="skip",
                ))
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
            hovermode="x unified", height=800,
        )
        return fig

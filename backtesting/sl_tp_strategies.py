import pandas as pd


class SLTPStrategies:
    @staticmethod
    def fixed(features, stop_loss=0.03, take_profit=0.03):
        return stop_loss, take_profit

    @staticmethod
    def feature_based(features, feature="LIVE_STD", stop_multiplier=1.0,
                      take_profit_multiplier=1.0):
        value = features.get(feature)
        if pd.isna(value):
            raise ValueError(f"Missing SL/TP feature: {feature}")
        return abs(value) * stop_multiplier, abs(value) * take_profit_multiplier

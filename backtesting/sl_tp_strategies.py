import pandas as pd


class SLTPStrategies:
    @staticmethod
    def time_only(features):
        return float("inf"), float("inf")

    @staticmethod
    def fixed(features, stop_loss=0.03, take_profit=0.03):
        return stop_loss, take_profit

    @staticmethod
    def feature_based(features, feature="LIVE_STD", stop_multiplier=1.0,
                      take_profit_multiplier=1.0, max_stop=None, max_target=None,
                      stop_feature=None, target_feature=None):
        stop_value = features.get(stop_feature or feature)
        target_value = features.get(target_feature or feature)
        if pd.isna(stop_value) or pd.isna(target_value):
            raise ValueError("Missing SL/TP feature")
        stop = abs(stop_value) * stop_multiplier
        target = abs(target_value) * take_profit_multiplier
        return (min(stop, max_stop) if max_stop is not None else stop,
                min(target, max_target) if max_target is not None else target)

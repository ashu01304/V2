def get_trade_rules(symbol, strategy, exit_method="reward2"):
    """Return entry and exit parameters without performing any calculations."""
    symbol = symbol.upper()
    time_only = exit_method == "time"
    target_multiplier = 1.0 if exit_method == "symmetric" else 2.0
    minimum_expected_move = 0.0
    if symbol in ("CL", "CO"):
        if strategy.endswith("MDDF"):
            minimum_expected_move = 0.03
        elif strategy.endswith(("MDF", "MF")):
            minimum_expected_move = 0.02

    return {
        "entry_zscore": 1.6,
        "minimum_expected_move": minimum_expected_move,
        "max_stop": 0.09 if symbol in ("CL", "CO") else None,
        "max_target": 0.1 if symbol in ("CL", "CO") else None,
        "stop_slippage": 0.01 if symbol in ("CL", "CO") else 0.0,
        "stop_multiplier": 1.0,
        "target_multiplier": target_multiplier,
        "minimum_reward_risk": (0.0 if time_only else 1.0
                                if symbol in ("CL", "CO") or exit_method == "symmetric"
                                else 1.5),
        "breakeven_after_r": None,
        "not_working_days": None,
        "max_hold_days": 10,
        "time_only": time_only,
        "exact_stop_fill": symbol in ("CL", "CO"),
    }

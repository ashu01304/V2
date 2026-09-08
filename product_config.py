"""Product metadata and bucket-specific parameter-search configuration."""

PRODUCT_CONFIG = {
    "CL": {
        "tick_size": 0.01, "price_decimals": 2,
        "buckets": {
            "1MDF": {
                "lookbacks": (10, 15, 20), "ranks": (2, 3),
                "max_parallel": (4, 6), "max_consecutive": (2, 3),
                "to_from": ((100, 50), (120, 65), (140, 80),
                            (140, 50), (120, 50), (140, 65)),
                "swing_lookbacks": (15, 20, 25), "swing_top_counts": (4, 6, 8),
                "minimum_swing_ticks": (4, 5),
                "minimum_trade_thresholds": (500, 1000),
            },
            "1MDDF": {
                "lookbacks": (10, 15, 20), "ranks": (2, 3),
                "max_parallel": (4, 6), "max_consecutive": (2, 3),
                "to_from": ((100, 50), (120, 65), (140, 80),
                            (140, 50), (120, 50), (140, 65)),
                "swing_lookbacks": (15, 20, 25), "swing_top_counts": (4, 6, 8),
                "minimum_swing_ticks": (4, 6),
                "minimum_trade_thresholds": (500, 1000),
            },
        },
    },
    "CO": {
        "tick_size": 0.01, "price_decimals": 2,
        "buckets": {
              "1MDF": {
                     "lookbacks": (10, 15, 20), "ranks": (2, 3),
                     "max_parallel": (4, 6), "max_consecutive": (2, 3),
                     "to_from": ((100, 50), (120, 65), (140, 80),
                            (140, 50), (120, 50), (140, 65)),
                     "swing_lookbacks": (15, 20, 25), "swing_top_counts": (4, 6, 8),
                     "minimum_swing_ticks": (4, 5),
                     "minimum_trade_thresholds": (500, 1000),
              },
              "1MDDF": {
                     "lookbacks": (10, 15, 20), "ranks": (2, 3),
                     "max_parallel": (4, 6), "max_consecutive": (2, 3),
                     "to_from": ((100, 50), (120, 65), (140, 80),
                            (140, 50), (120, 50), (140, 65)),
                     "swing_lookbacks": (15, 20, 25), "swing_top_counts": (4, 6, 8),
                     "minimum_swing_ticks": (4, 6),
                     "minimum_trade_thresholds": (500, 1000),
              },
        },
    },
}

# Choose which products testing_parameters.py should calculate.
TEST_SYMBOLS = ("CO",)

if isinstance(TEST_SYMBOLS, str):
    TEST_SYMBOLS = (TEST_SYMBOLS,)
unknown = set(TEST_SYMBOLS) - set(PRODUCT_CONFIG)
if unknown:
    raise ValueError(f"Missing product configuration for: {sorted(unknown)}")

SYMBOLS = TEST_SYMBOLS
TICK_SIZES = {symbol: settings["tick_size"]
              for symbol, settings in PRODUCT_CONFIG.items()}

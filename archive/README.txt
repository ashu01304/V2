Retired code retained for reference and recovery
=============================================

structure_lab/ is the original experiment superseded by market_data/.
legacy_live/ contains the old fixed-subscription bridge, monitor, recording
helpers, configuration, related tests, and the previous market-data README.

These are historical snapshots, not maintained runnable packages. Their old
imports and launch commands refer to their previous locations. Do not add this
folder to PYTHONPATH or start its old servers alongside the production manager.
The lab's private runtime files are preserved here with its source; the master
database, live production registry and saved catalogue remain in place.

Current entry points, ports and manual checks: market_data/README.md.
The current 8070 watchlist is python -B -m market_data.live_dashboard.
Optional direct-feed and API testers are in diagnostics/.

No archived source, historical tests, credentials or market data were deleted.

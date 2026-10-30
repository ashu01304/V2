"""Optional manual diagnostics, separate from production dashboard startup.

Run from the project root with -m diagnostics.api_dashboard or
-m diagnostics.standalone_live_quote_tester. The latter opens independent
streamer connections only when explicitly requested in its UI.
Use -m diagnostics.manual_check for the read-only market-data checks.
The explicit maintenance command -m diagnostics.cleanup_database backs up
and rewrites the master database; it is never part of normal dashboard startup.
"""

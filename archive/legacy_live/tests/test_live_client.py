"""Optional live quotes share one client lifecycle and offline fallback."""

import unittest
from unittest.mock import Mock, patch

import requests

from market_data.live_client import fetch_live_quote


class OptionalLiveQuoteTests(unittest.TestCase):
    def test_success_and_failure_always_close_the_owned_client(self):
        quote = {"timestamp": "2026-10-15T10:00:00Z", "value": 1.23456}
        for outcome in [quote, requests.ConnectionError("offline"), KeyError("missing price")]:
            client = Mock()
            if isinstance(outcome, Exception):
                client.expression.side_effect = outcome
            else:
                client.expression.return_value = outcome
            with patch("market_data.live_client.LiveMarketDataClient", return_value=client):
                self.assertEqual(fetch_live_quote("CL-CO", "Z26-F27"),
                                 None if isinstance(outcome, Exception) else quote)
            client.expression.assert_called_once_with("CL-CO", "Z26-F27")
            client.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()

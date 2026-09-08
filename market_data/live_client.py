"""Small HTTP client for the centralized live_market_service process."""

import requests


class LiveMarketDataClient:
    def __init__(self, base_url="http://127.0.0.1:8060", timeout=3):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()

    def _get(self, path, **parameters):
        response = self.session.get(
            self.base_url + path, params=parameters, timeout=self.timeout
        )
        response.raise_for_status()
        return response.json()

    def health(self):
        return self._get("/health")

    def snapshot(self, products=None, raw=False):
        parameters = [("product", product) for product in products or []]
        if raw:
            parameters.append(("raw", "1"))
        response = self.session.get(
            self.base_url + "/snapshot", params=parameters, timeout=self.timeout
        )
        response.raise_for_status()
        return response.json()

    def latest(self, product, contract):
        return self._get("/latest", product=product, contract=contract)

    def history(self, product, contract):
        return self._get("/history", product=product, contract=contract)

    def expression(self, product, expression):
        return self._get("/expression/latest", product=product, expression=expression)

    def expression_history(self, product, expression):
        return self._get("/expression/history", product=product, expression=expression)

    def close(self):
        self.session.close()

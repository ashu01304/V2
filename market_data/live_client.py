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
        rows = response.json()
        # Forward curves consume price; the live bridge calls it value.
        for row in rows:
            if row.get("price") is None:
                row["price"] = row.get("value")
        return rows

    def latest(self, product, contract):
        return self._get("/latest", product=product, contract=contract)

    def history(self, product, contract, start=None, end=None):
        return self._get("/history", product=product, contract=contract,
                         **{key: value for key, value in
                            (("start", start), ("end", end)) if value is not None})

    def expression(self, product, expression):
        return self._get("/expression/latest", product=product, expression=expression)

    def expression_history(self, product, expression, start=None, end=None):
        return self._get("/expression/history", product=product, expression=expression,
                         **{key: value for key, value in
                            (("start", start), ("end", end)) if value is not None})

    def settlement_history(self, product, contract):
        return self._get("/settlement/history", product=product, contract=contract)

    def close(self):
        self.session.close()

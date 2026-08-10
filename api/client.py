import time

import requests

from .auth import get_access_token


class APIClient:
    def __init__(self):
        self.session = requests.Session()

    def get(self, endpoint, params=None, retries=3, timeout=30):
        for attempt in range(retries):
            response = self.session.get(
                endpoint,
                headers={
                    "Authorization": f"Bearer {get_access_token()}",
                    "Accept": "application/json",
                },
                params=params,
                timeout=timeout,
            )

            if response.status_code == 200:
                return response.json()
            if response.status_code == 401 and attempt < retries - 1:
                continue
            if response.status_code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            response.raise_for_status()

        raise RuntimeError("Maximum API retries exceeded")

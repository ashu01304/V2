"""Bearer requests, bounded retries, endpoint budgets, and a shared disk cache."""

import threading
import time

import requests

from .auth import load_token
from .cache import ResponseCache
from .rate_limit import STATE_DIR, endpoint_state, reserve

BASE_URL = "https://qh-api.corp.hertshtengroup.com/apis"
LIMITS = {"/ohlc/": (30, 1800, 43200), "/tas/": (10, 600, 14400)}


class QHAPIError(RuntimeError):
    pass


class QHClient:
    def __init__(self, token=None, *, timeout=60, max_attempts=5,
                 cache_ttl=60, state_dir=STATE_DIR):
        if not 1 <= max_attempts <= 5:
            raise ValueError("max_attempts must be between 1 and 5")
        if timeout <= 0 or cache_ttl < 0:
            raise ValueError("timeout must be positive and cache_ttl nonnegative")
        self._token = load_token(token)
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.cache_ttl = cache_ttl
        self.state_dir = state_dir
        self._session = requests.Session()
        self._session.headers.update(Authorization="Bearer " + self._token,
                                     Accept="application/json")
        self._lock = threading.RLock()
        self._cache = ResponseCache(state_dir)
        self.last_response = None

    def call(self, method, path, *, params=None, json_body=None, cache_ttl=None):
        method = method.upper()
        allowed = {("GET", "/fairvalue/"), ("GET", "/fairvalue/historical/"),
                   ("GET", "/ohlc/"), ("GET", "/vap/"),
                   ("POST", "/tas/")}
        if (method, path) not in allowed:
            raise ValueError("Unsupported method/path; register documented read endpoints first")
        ttl = self.cache_ttl if cache_ttl is None else cache_ttl
        if ttl < 0:
            raise ValueError("cache_ttl must be nonnegative")
        key = self._cache.key(self._token, BASE_URL, method, path, params, json_body)
        with self._lock:
            cached = self._cache.get(key, ttl)
            if cached is not None:
                self.last_response = {"path": path, "cached": True, "cache": "disk",
                                      "cache_age_seconds": round(cached["age_seconds"], 3),
                                      "cache_expires_at": cached["expires_at"]}
                return cached["payload"]
            with endpoint_state(self._token, path, self.state_dir) as (state, save):
                # Another process may have filled the cache while we waited.
                cached = self._cache.get(key, ttl)
                if cached is not None:
                    self.last_response = {"path": path, "cached": True, "cache": "disk",
                                          "cache_age_seconds": round(cached["age_seconds"], 3),
                                          "cache_expires_at": cached["expires_at"]}
                    return cached["payload"]
                for attempt in range(self.max_attempts):
                    reserve(state, save, LIMITS.get(path, (7, 420, 10080)))
                    try:
                        response = self._session.request(
                            method, BASE_URL + path, params=params, json=json_body,
                            timeout=self.timeout, allow_redirects=False)
                    except (requests.Timeout, requests.ConnectionError):
                        state["next_allowed"] = max(state.get("next_allowed", 0),
                                                    time.time() + 2 ** attempt)
                        save()
                        if attempt + 1 == self.max_attempts:
                            raise QHAPIError(f"{method} {path}: network failure after {self.max_attempts} attempts") from None
                        continue
                    headers = response.headers
                    self.last_response = {
                        "path": path, "status": response.status_code, "cached": False,
                        "rate_limit": {k: v for k, v in headers.items()
                                       if k.lower().startswith("x-ratelimit-") or k.lower() == "retry-after"}}
                    delay_until = state.get("next_allowed", 0)
                    if headers.get("X-RateLimit-Remaining") == "0":
                        try:
                            delay_until = max(delay_until, float(headers["X-RateLimit-Reset"]) + 0.05)
                        except (KeyError, ValueError):
                            window = headers.get("X-RateLimit-Window", "day")
                            delay_until = max(delay_until, time.time() + {"minute": 60, "hour": 3600}.get(window, 86400))
                    retry = response.status_code == 429 or 500 <= response.status_code < 600
                    if retry:
                        delay = 2 ** attempt
                        if response.status_code == 429:
                            try:
                                delay = max(1, float(headers.get("Retry-After", 60)))
                            except ValueError:
                                delay = 60
                        delay_until = max(delay_until, time.time() + delay)
                    state["next_allowed"] = delay_until
                    save()
                    if retry:
                        response.close()
                        if attempt + 1 == self.max_attempts:
                            raise QHAPIError(f"{method} {path}: HTTP {response.status_code} after {self.max_attempts} attempts")
                        continue
                    if not 200 <= response.status_code < 300:
                        status = response.status_code
                        response.close()
                        hint = " Obtain a fresh token via /apis/auth/." if status in (401, 403) else ""
                        raise QHAPIError(f"{method} {path}: HTTP {status}.{hint}")
                    try:
                        payload = response.json()
                    except ValueError:
                        raise QHAPIError(f"{method} {path}: expected JSON; check authentication and response format") from None
                    finally:
                        response.close()
                    self._cache.put(key, payload, ttl)
                    return payload

    def close(self):
        with self._lock:
            self._session.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

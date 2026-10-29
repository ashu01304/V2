from ..results import APIResult
from ._parameters import csv, options


def fetch(client, instruments, *, interval="5M", count=50, end=None, cache_ttl=None):
    if interval not in {"5M", "1D"}:
        raise ValueError("interval must be 5M or 1D")
    params = options(instruments=csv(instruments, maximum=50), interval=interval, count=count, end=end)
    return APIResult(client.call("GET", "/vap/", params=params, cache_ttl=cache_ttl))

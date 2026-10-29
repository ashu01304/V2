from ..results import APIResult
from ._parameters import csv, options


def fetch(client, instruments=None, *, hg_instrument_ids=None, interval="1H",
          count=50, start=None, end=None, extra_fields=None, cache_ttl=None):
    if (instruments is None) == (hg_instrument_ids is None):
        raise ValueError("Provide either instruments or hg_instrument_ids")
    if interval not in {"1M", "5M", "1H", "1D"}:
        raise ValueError("interval must be 1M, 5M, 1H, or 1D")
    params = options(instruments=csv(instruments, maximum=50) if instruments is not None else None,
                     hg_instrument_ids=csv(hg_instrument_ids, maximum=50) if hg_instrument_ids is not None else None,
                     interval=interval, count=count, start=start, end=end,
                     extraFields=csv(extra_fields) if extra_fields is not None else None)
    return APIResult(client.call("GET", "/ohlc/", params=params, cache_ttl=cache_ttl))

from ..results import APIResult
from ._parameters import csv, options


def fetch(client, instruments, *, timeinterval="1m", count=50, start=None,
          end=None, fields=None, cache_ttl=None):
    params = options(instruments=csv(instruments, maximum=50), timeinterval=timeinterval,
                     count=count, start=start, end=end,
                     fields=csv(fields) if fields is not None else None)
    return APIResult(client.call("GET", "/fairvalue/historical/", params=params, cache_ttl=cache_ttl))

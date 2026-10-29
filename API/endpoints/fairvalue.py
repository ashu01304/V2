from ..results import APIResult
from ._parameters import csv


def fetch(client, products, *, cache_ttl=None):
    return APIResult(client.call("GET", "/fairvalue/",
                                params={"products": csv(products)}, cache_ttl=cache_ttl))

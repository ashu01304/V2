from ..results import APIResult


def fetch(client, products, *, cache_ttl=None):
    """products: dictionaries with id, dates (YYYY-MM-DD), start/end (HH:MM:SS)."""
    products = list(products)
    if not products or any(not isinstance(product, dict) or
                           not all(product.get(key) for key in ("id", "dates", "start", "end"))
                           for product in products):
        raise ValueError("Each product requires id, dates, start, and end")
    return APIResult(client.call("POST", "/tas/", json_body={"products": products}, cache_ttl=cache_ttl))

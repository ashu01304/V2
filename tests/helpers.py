"""In-memory reader fixtures; no production database or reader snapshot is opened."""

from market_data.queries import MarketData


def memory_reader(connection):
    reader = MarketData.__new__(MarketData)
    reader._connection = connection
    reader._snapshot_key = None
    reader._closed = False
    return reader

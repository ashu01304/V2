"""Central access to spread prices and SEAC settlements."""

from .queries import MarketData
from .sync import update

__all__ = ["MarketData", "update"]

from .client import APIClient
from .config import API_V2


class OHLC(APIClient):
    VALID_INTERVALS = {"1M", "5M", "1H", "1D"}

    def get(
        self,
        instruments,
        interval="1D",
        count=None,
        start=None,
        end=None,
        extra_fields=None,
    ):
        interval = interval.upper()
        if interval not in self.VALID_INTERVALS:
            raise ValueError(
                f"Invalid interval {interval!r}; expected one of {sorted(self.VALID_INTERVALS)}"
            )
        if isinstance(instruments, (list, tuple)):
            instruments = ",".join(instruments)
        if isinstance(extra_fields, (list, tuple)):
            extra_fields = ",".join(extra_fields)

        params = {"instruments": instruments, "interval": interval}
        for key, value in {
            "count": count,
            "start": start,
            "end": end,
            "extraFields": extra_fields,
        }.items():
            if value is not None:
                params[key] = value

        candles = super().get(f"{API_V2}/ohlc/", params=params)
        grouped = {}
        for candle in candles:
            grouped.setdefault(candle["product"], []).append(candle)
        return grouped

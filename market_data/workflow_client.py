"""Shared dashboard clients, batching and session helpers backed by DataClient."""
import atexit
import math
import threading
import pandas as pd
from .data_client import DataClient
from .structure_parser import from_legacy,parse_structure,canonical_structure
from .queries import TIMEFRAMES, MAX_CANDLES
from .config import LIVE_MANAGER_URL

SESSION_TIMEZONE = "America/Chicago"
SESSION_MINUTES = 23 * 60


def session_window(past_days, now=None):
    if past_days is None or not float(past_days).is_integer() or past_days < 1:
        raise ValueError("Past N days must be a positive whole number.")
    end = pd.Timestamp.now(tz="UTC") if now is None else pd.Timestamp(now)
    if end.tzinfo is None:
        raise ValueError("The reference time must include a timezone.")
    end = end.tz_convert("UTC")
    local = end.tz_convert(SESSION_TIMEZONE).tz_localize(None)
    opening = (local - pd.Timedelta(hours=17)).normalize() + pd.Timedelta(hours=17)
    start = opening - pd.Timedelta(days=int(past_days) - 1)
    return start.tz_localize(SESSION_TIMEZONE).tz_convert("UTC"), end


_clients={}
_lock=threading.RLock()


def shared_client(scope,live_url=LIVE_MANAGER_URL):
    with _lock:
        key=(scope,live_url)
        if key not in _clients:
            _clients[key]=DataClient(live_url=live_url)
        return _clients[key]


@atexit.register
def close_clients():
    for client in list(_clients.values()):
        client.close()  # Leases expire; process exit does not block on network calls.


def full_structure(product,expression):
    try:
        return parse_structure(expression).text
    except ValueError:
        return from_legacy(product,expression)


class WorkflowLiveClient:
    def __init__(self,scope='table',base_url=LIVE_MANAGER_URL,timeout=5):
        self.client=shared_client(scope,base_url)
        self.scope=scope

    def quotes(self,product,expressions,best_sides=True,*,include_recipes=False,include_alternatives=False):
        expressions=list(dict.fromkeys(expressions))
        results={}
        for offset in range(0,len(expressions),64):
            chunk=expressions[offset:offset+64]
            valid=[]
            for expression in chunk:
                try:
                    text=full_structure(product,expression)
                    self.client.plan(text,'live')
                    valid.append((expression,text))
                except ValueError as error:
                    results[expression]=dict(expression=expression,value=None,timestamp=None,error=str(error))
            if valid:
                rows=self.client.live_many([text for _,text in valid],include_recipes=include_recipes,
                    include_alternatives=include_alternatives)
                for (expression,_),row in zip(valid,rows):
                    results[expression]=dict(**(row.get('data') or {}),expression=expression,
                        status=row['status'],source='shared_structure_manager',message=row.get('message'))
        return results

    def expression(self,product,expression,best_sides=True,*,include_alternatives=False):
        return self.quotes(product,[expression],best_sides,include_alternatives=include_alternatives)[expression]

    def health(self):
        status=self.client.live_resolver._call('GET','/status')
        return {**status,'latest_price_received_at':status.get('latest_price_received_at')}

    def replace(self,structures):
        """Forget local selections; shared structures expire only through inactivity."""
        wanted={canonical_structure(text) for text in structures}
        resolver=self.client.live_resolver
        with resolver.lock:
            removed=[text for text in resolver.active if text not in wanted]
        for text in removed:
            try:
                self.client.disconnect([text])
            except Exception:
                pass  # Outstanding demand still expires after 8 hours.

    def close(self):
        pass  # Shared per-role client survives short-lived chart callbacks.


def fetch_live_quote(product,expression,scope='popup',*,include_alternatives=False):
    live=WorkflowLiveClient(scope)
    try:
        text=full_structure(product,expression)
        live.replace([text])
        return live.expression(product,expression,include_alternatives=include_alternatives)
    except Exception:
        return None


def load_session_minutes(product,expression,past_days=16,now=None,database=None):
    start,end=session_window(past_days,now)
    client=DataClient(database=database)
    try:
        result=client.minute(full_structure(product,expression),start=start,end=end,interval=None)
    finally:
        client.close()
    if result['data'].empty:
        raise ValueError(result['message'])
    return result['data'].set_index('timestamp')


def load_candles(product,expression,timeframe=15,max_candles=1500,database=None):
    if not isinstance(timeframe,int) or not 1<=timeframe<=1440:
        raise ValueError('Choose a valid candle interval')
    days=max(16,math.ceil(max_candles*timeframe/1380*1.6)+7)
    start,end=session_window(days)
    client=DataClient(database=database)
    try:
        result=client.minute(full_structure(product,expression),start=start,end=end,interval=timeframe)
    finally:
        client.close()
    if result['data'].empty:
        raise ValueError(result['message'])
    return result['data'].tail(max_candles).set_index('timestamp').rename_axis('candle_time')


def load_snapshot_history(selections, start, end, database=None):
    """Fetch a bounded 15-minute grid through the reader, preserving missing prices."""
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    if start.tzinfo is None or end.tzinfo is None or start > end:
        raise ValueError("Snapshot bounds must be ordered and timezone-aware")
    targets = pd.date_range(start.tz_convert("UTC").ceil("15min"), end.tz_convert("UTC").floor("15min"), freq="15min")
    columns = [key for key, _product, _expression in selections]
    if not selections or not len(targets):
        return pd.DataFrame(index=targets, columns=columns, dtype=float)
    client=DataClient(database=database)
    try:
        snapshots = client.minute_snapshots([(key,full_structure(product,expression))
                                             for key,product,expression in selections],targets)
    finally:
        client.close()
    return pd.DataFrame([snapshots[target] for target in targets], index=targets).reindex(columns=columns).astype(float)


class LiveMarketDataClient(WorkflowLiveClient):
    def __init__(self,base_url=LIVE_MANAGER_URL,timeout=3):
        super().__init__('compatibility',base_url=base_url,timeout=timeout)
        self.base_url=base_url.rstrip('/')
        self.timeout=timeout

    def latest(self,product,contract):
        return self.expression(product,contract)

    def snapshot(self,products=None,raw=False,include_structures=False):
        # Outright snapshots are intentionally unavailable: consumers must request
        # whole structures rather than synthesize prices from outright books.
        if not include_structures:
            return []
        rows=self.client.live_resolver._call('GET','/contracts')['contracts']
        return [row for row in rows if row['fresh'] and (not products or row['product'] in products)]

    def curve(self,product,expression):
        from analysis.forward_curve import ForwardCurve
        curve=ForwardCurve(live=self)
        try:
            return curve.calculate(product,expression)
        finally:
            curve.close()

    def history(self,product,contract):
        raise ValueError('Live tick history is not recorded by the request manager; use the minute resolver for stored history')

    def expression_history(self,product,expression):
        return self.history(product,expression)



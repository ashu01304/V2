"""Accept complete requests, maintain demand leases, publish resolved prices."""
import threading
import time
from .live_request_store import RequestStore, LiveMinuteRecorder
from .structure_parser import parse_structure, canonical_structure
from .live_resolver import plan
from .quote_catalogue import names
from .live_feed import LiveFeed, selection
from .config import LIVE_SUBSCRIPTION_TTL_SECONDS


def resolve_requirements(expression):
    expression=canonical_structure(expression)
    request=parse_structure(expression)
    components=plan(expression)
    try:
        _,target,contracts=selection(expression)
        warning=''
    except ValueError as error:
        if str(error)!='No saved quoted IDs match the requested months':
            raise
        target={p+':'+month:weight for (p,month),weight in request.settlement_legs().items()}
        contracts={}
        warning='No saved quoted IDs cover the requested months'
    return dict(expression=expression,target=target,contracts=contracts,components=components,warning=warning)


class SubscriptionService:
    def __init__(self,store,feed=None):
        self.store=store
        self.feed=feed or LiveFeed()
        self.desired={}
        self.last_attempt=0

    def reconcile(self):
        desired=self.store.desired()
        if desired==self.desired:
            with self.feed.lock:
                missing=bool(desired.keys()-self.feed.accepted)
                if not missing or time.monotonic()-self.last_attempt<15 or self.feed.update_lock.locked():
                    return False
        self.feed.update_metadata(desired)
        self.desired=desired
        self.last_attempt=time.monotonic()
        with self.feed.lock:
            self.feed.errors=self.feed.errors[-50:]
        return True

    def status(self):
        try:
            labels=names()
        except (OSError,ValueError,KeyError,TypeError):
            labels={}
        with self.feed.lock:
            return dict(status=self.feed.status,subscribed=len(self.feed.accepted),
                        latest_price_received_at=max((row['received_at'] for tt,row in self.feed.books.items()
                            if tt in self.feed.accepted and self.feed.groups.get(row['group'],'').startswith('CONNECTED:')),default=None),
                        required=len(self.desired),received=len(self.feed.books.keys() & self.desired.keys()),
                        contracts=[dict(tt_id=tt,product=meta['product'],structure=labels.get(tt,'Name unavailable'),
                            accepted=tt in self.feed.accepted,received=tt in self.feed.books)
                            for tt,meta in sorted(self.desired.items())],
                        errors=list(dict.fromkeys(self.feed.errors))[-5:])

    def stop(self):
        self.feed.stop()
        self.desired={}


class PriceManager:
    def __init__(self,store=None,subscriptions=None,recorder=None):
        self.store=store or RequestStore()
        self.subscriptions=subscriptions or SubscriptionService(self.store)
        self.recorder=recorder or LiveMinuteRecorder()
        self.subscriptions.feed.recorder=self.recorder
        self.halt=threading.Event()
        self.thread=None
        self.error=None
        self.cursor=0

    def register(self,owner=None,expressions=None,ttl=LIVE_SUBSCRIPTION_TTL_SECONDS):
        if not isinstance(expressions,list) or not 1<=len(expressions)<=64:
            raise ValueError('Send between 1 and 64 complete structure strings')
        # Resolve the whole batch first: invalid input does not partly subscribe.
        return self.store.register(owner,[resolve_requirements(text) for text in expressions],ttl)

    def cycle(self):
        self.subscriptions.reconcile()
        active=self.store.active()
        for row in active:
            if self.halt.is_set():
                break
            self.publish_request(row,optimize=False)
        # Keep publication moving even with many requested curves and table cells.
        deadline=time.monotonic()+1
        for _ in range(len(active)):
            if self.halt.is_set() or time.monotonic()>=deadline:
                break
            row=active[self.cursor%len(active)]
            self.cursor+=1
            self.publish_request(row,optimize=True)

    def publish_request(self,row,optimize):
        try:
            quote=self.subscriptions.feed.snapshot(row['expression'],allowed_ids=row['contracts'],optimize=optimize)
            if row.get('warning'):
                quote['price_reason']=row['warning']
        except Exception:
            import logging
            logging.getLogger(__name__).exception('Unable to resolve request %s',row['request_id'])
            quote=dict(status='Calculation error',price_status='error',
                       price_reason='Unable to calculate this structure; retrying next cycle',overall=None,
                       errors=['This request failed; other structures continue updating'])
        quote['expression']=row['expression']
        quote['request_id']=row['request_id']
        quote['computed_at']=self.store.clock()
        try:
            self.store.publish(row['request_id'],quote)
        except Exception:
            import logging
            logging.getLogger(__name__).exception('Unable to publish request %s',row['request_id'])

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.recorder.start()
        self.store.clear_quotes()
        self.halt.clear()
        def work():
            while not self.halt.is_set():
                try:
                    self.cycle()
                    self.error=None
                except Exception:
                    # SDK messages are sanitized by the feed. Do not expose
                    # arbitrary exception text or credentials over HTTP.
                    self.error='Manager cycle failed; check local service logs'
                    import logging
                    logging.getLogger(__name__).exception('Live manager cycle failed')
                self.halt.wait(1)
        self.thread=threading.Thread(target=work,daemon=True,name='shared-price-manager')
        self.thread.start()

    def stop(self):
        self.halt.set()
        if self.thread:
            self.thread.join()
        try:
            self.subscriptions.stop()
        finally:
            self.recorder.close()

    def read(self,key):
        # Reading a quote is demand; monitor/status reads do not renew anything.
        result=self.store.read(key)
        if result and result['quote'] and self.store.clock()-result['computed_at']>10:
            result['quote']=None
            result['status']='Published result is stale; waiting for the manager'
        return result

    def status(self):
        rows=self.store.active()
        return dict(**self.subscriptions.status(),active_requests=len(rows),
                    requests=[dict(request_id=r['request_id'],expression=r['expression'],
                                   required_ids=len(r['contracts']),expires_at=r['expires_at']) for r in rows],
                    manager_error=self.error,server_time=self.store.clock(),minute_recording=self.recorder.health())

    def contracts(self):
        """Current subscribed quoted books for diagnostics; never adds demand."""
        from .quote_catalogue import names
        labels=names()
        feed=self.subscriptions.feed
        now=time.time()
        result=[]
        with feed.lock:
            for tt,metadata in feed.metadata.items():
                row=feed.books.get(tt,{})
                fresh=(tt in feed.accepted and feed.groups.get(row.get('group'),'').startswith('CONNECTED:')
                       and 0<=now-row.get('received',0)<=120)
                bid=row.get('bid') if fresh else None
                ask=row.get('ask') if fresh else None
                mid=(bid+ask)/2 if bid is not None and ask is not None else None
                result.append(dict(tt_id=tt,product=metadata['product'],structure=labels.get(tt,'Name unavailable'),
                    bid=bid,ask=ask,mid=mid,value=mid,price=mid,timestamp=row.get('received_at'),fresh=fresh,connected=fresh))
        return result


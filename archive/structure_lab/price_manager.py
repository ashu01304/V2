"""Accept complete requests, maintain demand leases, publish resolved prices."""
import threading
from .request_store import RequestStore
from .structure_interpreter import resolve
from .subscription_service import SubscriptionService


class PriceManager:
    def __init__(self,store=None,subscriptions=None):
        self.store=store or RequestStore()
        self.subscriptions=subscriptions or SubscriptionService(self.store)
        self.halt=threading.Event()
        self.thread=None
        self.error=None

    def register(self,owner,expressions,ttl=1800):
        if not isinstance(expressions,list) or not 1<=len(expressions)<=64:
            raise ValueError('Send between 1 and 64 complete structure strings')
        # Resolve the whole batch first: invalid input does not partly subscribe.
        return self.store.register(owner,[resolve(text) for text in expressions],ttl)

    def cycle(self):
        self.subscriptions.reconcile()
        for row in self.store.active():
            if self.halt.is_set():
                break
            try:
                quote=self.subscriptions.feed.snapshot(row['expression'],allowed_ids=row['contracts'])
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
                    logging.getLogger(__name__).exception('Lab manager cycle failed')
                self.halt.wait(1)
        self.thread=threading.Thread(target=work,daemon=True,name='lab-price-manager')
        self.thread.start()

    def stop(self):
        self.halt.set()
        if self.thread:
            self.thread.join()
        self.subscriptions.stop()

    def read(self,key):
        result=self.store.read(key)
        if result and result['quote'] and self.store.clock()-result['computed_at']>10:
            result['quote']=None
            result['status']='Published result is stale; waiting for the manager'
        return result

    def status(self):
        rows=self.store.active()
        return dict(**self.subscriptions.status(),active_requests=len(rows),
                    requests=[dict(request_id=r['request_id'],owner=r['owner'],expression=r['expression'],
                                   required_ids=len(r['contracts']),expires_at=r['expires_at']) for r in rows],
                    manager_error=self.error,server_time=self.store.clock())

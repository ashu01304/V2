"""The only lab component deciding which TT IDs the feed subscribes to."""
from .live_feed import LiveFeed
from .catalogue import names
import time


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
                        required=len(self.desired),received=len(self.feed.books.keys() & self.desired.keys()),
                        contracts=[dict(tt_id=tt,product=meta['product'],structure=labels.get(tt,'Name unavailable'),
                            accepted=tt in self.feed.accepted,received=tt in self.feed.books)
                            for tt,meta in sorted(self.desired.items())],
                        errors=list(dict.fromkeys(self.feed.errors))[-5:])

    def stop(self):
        self.feed.stop()
        self.desired={}

"""Dashboard helper: send whole expressions; never parse legs or contact MDS."""
import time
import uuid
from urllib.parse import urlparse
import requests
from market_data.config import LIVE_MANAGER_URL


class LivePriceClient:
    def __init__(self,url=LIVE_MANAGER_URL,owner=None):
        parsed=urlparse(url)
        if parsed.scheme!='http' or parsed.hostname not in ('127.0.0.1','localhost'):
            raise ValueError('Use the local lab manager URL')
        self.url=url.rstrip('/')
        self.owner=owner or 'dashboard-'+uuid.uuid4().hex[:12]
        self.session=requests.Session()
        self.expression=None
        self.request_id=None
        self.last_heartbeat=0

    def _call(self,method,path,**kwargs):
        try:
            response=self.session.request(method,self.url+path,timeout=(2,5),**kwargs)
        except requests.RequestException:
            raise ValueError('Lab live manager unavailable. Start python -B -m structure_lab.manager_server') from None
        payload=response.json()
        if not response.ok:
            raise ValueError(payload.get('error','Lab manager request failed'))
        return payload

    def register(self,structures,ttl=1800):
        return self._call('POST','/requests',json=dict(owner=self.owner,structures=structures,ttl=ttl))['requests']

    def renew(self,ids,ttl=1800):
        return self._call('POST','/requests/heartbeat',json=dict(owner=self.owner,request_ids=ids,ttl=ttl))['renewed']

    def quotes(self,ids,include_details=True):
        return self._call('POST','/quotes',json=dict(request_ids=ids,include_details=include_details))['quotes']

    def release(self,key):
        return self._call('DELETE','/requests/'+key,params=dict(owner=self.owner))['released']

    # Compatibility with the lab page's Subscribe / Stop controls.
    def start(self,expression):
        row=self.register([expression])[0]
        old=self.request_id
        self.request_id=row['request_id']
        self.expression=expression
        self.last_heartbeat=time.monotonic()
        if old and old!=self.request_id:
            self.release(old)
        return len(row['required_ids'])

    def stop(self):
        key=self.request_id
        self.request_id=None
        self.expression=None
        if key:
            try:
                self.release(key)
            except ValueError:
                pass  # A disconnected dashboard's lease still expires automatically.

    def snapshot(self,expression):
        empty=dict(status='Not subscribed',matched=False,requested=0,accepted=0,received=0,books={},overall=None,errors=[])
        if not self.request_id or expression!=self.expression:
            return empty
        try:
            if time.monotonic()-self.last_heartbeat>=15:
                if not self.renew([self.request_id]):
                    self.start(expression)
                self.last_heartbeat=time.monotonic()
            row=self._call('GET','/quotes/'+self.request_id)
            return row['quote'] or {**empty,'status':row.get('status','Waiting for shared manager'),'matched':True}
        except ValueError as error:
            return {**empty,'status':'Manager unavailable','errors':[str(error)]}

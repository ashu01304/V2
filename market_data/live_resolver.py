"""Whole-structure live adapter. The shared manager owns quotes and subscriptions."""
import threading
import time
from urllib.parse import urlparse
import requests
from .structure_parser import parse_structure, canonical_structure, NamedStructure
from collections import defaultdict
from .config import LIVE_MANAGER_URL, LIVE_SUBSCRIPTION_TTL_SECONDS


def quoted_components(request):
    components={}
    outright=defaultdict(lambda:defaultdict(float))
    def add(product,anchor,kind,gap,count):
        base=NamedStructure(1,product,anchor,kind,gap)
        prior=components.get(base.label,(base,0))
        components[base.label]=(base,prior[1]+count)
    for term in request.terms:
        if term.kind=='OUT':
            outright[term.product][term.anchor]+=term.count
            continue
        factors=(1,-1) if term.kind=='DF' else (1,-2,1) if term.kind=='DDF' else (1,)
        for i,factor in enumerate(factors):
            add(term.product,term.anchor+i*term.gap,'MF' if term.kind in {'DF','DDF'} else term.kind,
                term.gap,term.count*factor)
    for product,weights in outright.items():
        legs=sorted((month,weight) for month,weight in weights.items() if abs(weight)>1e-10)
        if abs(sum(weight for _,weight in legs))>1e-9:
            raise ValueError('Live structures need zero net exposure per product; outrights are not subscribed')
        cumulative=0
        for i,(month,weight) in enumerate(legs[:-1]):
            cumulative+=weight
            if abs(cumulative)<1e-10:
                continue
            while month<legs[i+1][0]:
                gap=min(24,legs[i+1][0]-month)
                add(product,month,'MS',gap,cumulative)
                month+=gap
    return [value for value in components.values() if abs(value[1])>1e-10]


def plan(structure):
    request=parse_structure(structure)
    return [dict(structure=term.label,weight=weight) for term,weight in quoted_components(request)]


class LiveResolver:
    def __init__(self,url=LIVE_MANAGER_URL,owner=None,timeout=5):
        parsed=urlparse(url)
        if parsed.scheme!='http' or parsed.hostname not in {'localhost','127.0.0.1','::1'} or parsed.username or parsed.password:
            raise ValueError('Use a local live manager HTTP URL')
        self.url=url.rstrip('/')
        self.timeout=timeout
        self.session=requests.Session()
        self.active={}
        self.full_recipe_response=False
        self.lock=threading.RLock()

    def _call(self,method,path,**kwargs):
        response=self.session.request(method,self.url+path,timeout=(2,self.timeout),**kwargs)
        try:
            payload=response.json()
        except ValueError:
            raise requests.RequestException('Manager returned an invalid JSON response') from None
        if not isinstance(payload,dict):
            raise requests.RequestException('Manager returned an unexpected response')
        if not response.ok:
            if response.status_code>=500:
                response.raise_for_status()
            raise ValueError(payload.get('error','Live manager rejected the request'))
        return payload

    def _register(self,structures):
        rows=self._call('POST','/requests',json=dict(structures=structures,ttl=LIVE_SUBSCRIPTION_TTL_SECONDS))['requests']
        for row in rows:
            self.active[row['expression']]={**row,'renewed_at':time.time()}

    def resolve(self,structure,*,include_alternatives=False):
        return self.resolve_many([structure],include_alternatives=include_alternatives)[0]

    def resolve_many(self,structures,*,include_recipes=False,include_alternatives=False):
        if not isinstance(structures,(list,tuple)) or not 1<=len(structures)<=64:
            raise ValueError('Request between 1 and 64 complete live structures')
        structures=[canonical_structure(text) for text in structures]
        recipes=[plan(text) for text in structures]
        unique=list(dict.fromkeys(structures))
        with self.lock:
            try:
                now=time.time()
                missing=[text for text in unique if text not in self.active or self.active[text]['expires_at']<=now]
                if missing:
                    self._register(missing)
                ids=[self.active[text]['request_id'] for text in unique]
                rows=self._call('POST','/quotes',json=dict(request_ids=ids,include_details=include_recipes and self.full_recipe_response,
                    include_recipes=include_recipes,include_alternatives=include_alternatives))['quotes']
                if not isinstance(rows,list) or len(rows)!=len(unique):
                    raise requests.RequestException('Manager returned incomplete batch results')
                if include_recipes and not self.full_recipe_response and any(
                    overall.get(side) is not None and not overall.get(side+'_recipe')
                    for row in rows for overall in [((row or {}).get('quote') or {}).get('overall') or {}]
                    for side in ('bid','ask')):
                    # Older running managers ignore include_recipes and strip recipes
                    # from compact responses. Read their existing publication in full.
                    self.full_recipe_response=True
                    rows=self._call('POST','/quotes',json=dict(request_ids=ids,include_details=True))['quotes']
                    if not isinstance(rows,list) or len(rows)!=len(unique):
                        raise requests.RequestException('Manager returned incomplete recipe results')
                received=dict(zip(unique,rows))
                for text,row in received.items():
                    if row:
                        self.active[text].update(expires_at=row['expires_at'],renewed_at=now)
            except requests.RequestException:
                return [dict(structure=text,source='live',status='unavailable',data=None,recipe=recipe,
                    message='Live manager unavailable; retry on the next poll') for text,recipe in zip(structures,recipes)]
            results=[]
            for text,recipe in zip(structures,recipes):
                row=received[text]
                quote=(row or {}).get('quote') or {}
                overall=quote.get('overall') or {}
                if include_recipes:
                    for side in ('bid','ask'):
                        parts=[]
                        for part in overall.get(side+'_recipe') or []:
                            weight=part['weight']
                            book_side=('bid' if weight>0 else 'ask') if side=='bid' else ('ask' if weight>0 else 'bid')
                            label=part.get('structure')
                            if not label:
                                from .quote_catalogue import names
                                try:
                                    label=names().get(part['tt_id'],part['tt_id'])
                                except (OSError,ValueError,KeyError,TypeError):
                                    label=part['tt_id']
                            parts.append({**part,'structure':label,'book_side':book_side,
                                'price':part.get('price',(quote.get('books') or {}).get(part['tt_id'],{}).get(book_side))})
                        overall[side+'_recipe']=parts
                present=[side for side in ('bid','ask') if overall.get(side) is not None]
                status=quote.get('price_status') or ('available' if len(present)==2 else 'partial' if present else 'pending')
                if row is None:
                    self.active.pop(text,None)
                results.append(dict(structure=text,source='live',status=status,recipe=recipe,
                    request_id=(row or {}).get('request_id'),
                    data=dict(bid=overall.get('bid'),ask=overall.get('ask'),mid=overall.get('mid'),
                              value=overall.get('mid'),timestamp=overall.get('timestamp'),
                              bid_optimal=overall.get('bid_optimal',False),ask_optimal=overall.get('ask_optimal',False),
                              **({side+'_recipe':overall.get(side+'_recipe',[]) for side in ('bid','ask')} if include_recipes else {}),
                              **({key:quote.get(key) for key in ('alternatives','alternatives_truncated','alternatives_scope')}
                                 if include_alternatives else {})),
                    message=quote.get('price_reason') or (row or {}).get('status') or quote.get('status','Waiting for manager')))
            return results

    def disconnect(self,structures=None):
        with self.lock:
            selected=list(self.active) if structures is None else [canonical_structure(text) for text in structures]
            for text in selected:
                if text in self.active:
                    del self.active[text]

    def close(self):
        """Close HTTP resources; shared demand expires after 8 hours of inactivity."""
        self.session.close()

"""Named live structures: independent demo books or queries to the existing API."""
from collections import defaultdict
from datetime import datetime, timezone
from urllib.parse import urlparse
import pandas as pd
import requests
from ..expression import NamedStructure
from ..sources import align_demo


def quoted_components(request):
    components={}
    for term in request.terms:
        if term.kind=='OUT':
            raise ValueError('Live resolver does not use outrights')
        if term.kind in {'DF','DDF'}:
            weights=(1,-1) if term.kind=='DF' else (1,-2,1)
            pieces=[NamedStructure(term.count*w,term.product,term.anchor+i*term.gap,'MF',term.gap)
                    for i,w in enumerate(weights)]
        else:
            pieces=[term]
        for piece in pieces:
            base=NamedStructure(1,piece.product,piece.anchor,piece.kind,piece.gap)
            prior=components.get(base.label,(base,0))
            components[base.label]=(base,prior[1]+piece.count)
    return [item for item in components.values() if abs(item[1])>1e-10]


def resolve(request,profile='demo',service_url='http://127.0.0.1:8060',best_sides=False):
    if any(term.kind=='OUT' for term in request.terms):
        raise ValueError('Live resolver does not use outrights')
    totals=defaultdict(float)
    for (product,_),weight in request.exposures().items():
        totals[product]+=weight
    if any(abs(w)>1e-9 for w in totals.values()):
        raise ValueError('Live requests must have zero net exposure per product; no outright fallback')
    components=[]
    now=datetime.now(timezone.utc).isoformat()
    if profile=='demo':
        from ..expression import StructureRequest
        for term,count in quoted_components(request):
            legs=StructureRequest(term.label,(term,)).settlement_legs()
            mid=float(align_demo(legs,pd.DatetimeIndex([pd.Timestamp(now)]))['price'].iloc[0])
            tick=.0001 if term.product in {'HO','NG','RB'} else .01
            quote=dict(value=mid,bid=mid-tick/2,ask=mid+tick/2,timestamp=now,source='demo_quoted_structure')
            components.append((count,quote,term.label))
    else:
        url=urlparse(service_url)
        if url.scheme!='http' or url.hostname not in {'127.0.0.1','localhost','::1'} or url.username or url.password:
            raise ValueError('Use a local HTTP live service, e.g. http://127.0.0.1:8060')
        for product in sorted(totals):
            expression=request.expression(product)
            if not expression:
                continue
            response=requests.post(service_url.rstrip('/')+'/quotes',json={
                'product':product,'expressions':[expression],'best_sides':bool(best_sides)},timeout=120 if best_sides else 15)
            response.raise_for_status()
            quote=response.json()['quotes'][0]
            if quote.get('error'):
                raise ValueError(quote['error'])
            components.append((1,quote,product+' '+expression))
    if not components:
        raise ValueError('The live request has no nonzero quoted components')
    def total(field,opposite=None):
        values=[(count,q.get(opposite if count<0 and opposite else field)) for count,q,_ in components]
        return sum(count*value for count,value in values) if all(value is not None for _,value in values) else None
    bid,ask=total('bid','ask'),total('ask','bid')
    quote=dict(bid=bid,ask=ask,mid=(bid+ask)/2 if bid is not None and ask is not None else None,
               reference=total('value'),asof=min(q['timestamp'] for _,q,_ in components),
               source='demo_named_components' if profile=='demo' else 'shared_service_adapter',
               status='available' if bid is not None or ask is not None else 'no_bid_ask')
    recipe=[dict(structure=label,weight=count,source=q.get('source'),bid=q.get('bid'),ask=q.get('ask'))
            for count,q,label in components]
    return quote,recipe

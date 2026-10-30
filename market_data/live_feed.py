"""Shared demand-driven MDS subscriptions. Received state stays in memory."""
from collections import defaultdict
from datetime import datetime,timezone
import json
import math
import ssl
import threading
import time
from urllib.request import urlopen
from .quote_catalogue import index, names
from .structure_parser import parse,ALIASES
from .live_pricing import optimize_side, available_quote, available_constructions

SERVER='https://ls.prod-live.hertshtengroup.com'
AUTH='https://insight.corp.hertshtengroup.com/test/amrit/externalapi.php?request=gettmsaltokenwithuser'
FIELDS=['bidPrice','askPrice','bidQuantity','askQuantity']


def number(value):
    try:
        value=float(value)
        return value if math.isfinite(value) else None
    except (TypeError,ValueError):
        return None


def exposure(product,legs):
    result=defaultdict(float)
    for month,weight in legs:
        for p,sign in ([('CL',1),('CO',-1)] if product=='CL-CO' else [(product,1)]):
            result[p+':'+month]+=sign*weight
    return {key:value for key,value in result.items() if abs(value)>1e-10}


def selection(expression,lookup=None):
    request=parse(expression)
    totals=defaultdict(float)
    for (product,_),weight in request.exposures().items():
        totals[product]+=weight
    if any(abs(weight)>1e-9 for weight in totals.values()):
        raise ValueError('Live subscriptions require zero net exposure per product; no outrights')
    target={p+':'+c:w for (p,c),w in request.settlement_legs().items()}
    required_months={key for term in request.terms for key in exposure(term.product,term.legs())}
    candidates={}
    for (product,legs),ids in (index() if lookup is None else lookup).items():
        weights=exposure(product,legs)
        # Subscribe to primary components and alternative structures entirely
        # within the requested months; do not subscribe the entire catalogue.
        if not set(weights)<=required_months:
            continue
        for tt in ids:
            candidates[tt]=dict(product=product,route='+1XCL' if product=='CL-CO' else product,weights=weights)
    if not candidates:
        raise ValueError('No saved quoted IDs match the requested months')
    return request,target,candidates


def credentials():
    with urlopen(AUTH,timeout=30,context=ssl.create_default_context()) as response:
        records=json.load(response)
    if not isinstance(records,list):
        raise ValueError('Authentication service returned an unexpected response')
    result={}
    for record in records:
        if not record.get('email') or not record.get('token'):
            continue
        for product in record.get('product','').split(','):
            product=product.strip().upper()
            if product:
                result[ALIASES.get(product,product)]=(record['email'].split('@')[0],record['token'])
    return result


class LiveFeed:
    def __init__(self):
        self.lock=threading.RLock()
        self.clients=[]
        self.generation=0
        self.expression=None
        self.target={}
        self.metadata={}
        self.books={}
        self.groups={}
        self.accepted=set()
        self.errors=[]
        self.status='Not subscribed'
        self.cache={}
        self.recipes={}
        self.update_lock=threading.Lock()
        self.auth={}
        self.connections={}
        self.subscriptions=[]
        self.recorder=None

    def stop(self):
        with self.lock:
            self.generation+=1
            clients,self.clients=self.clients,[]
            self.status='Stopped'
            self.groups={}
            self.accepted.clear()
            self.cache={}
            self.recipes={}
            self.auth={}
            self.connections={}
            for sub in self.subscriptions:
                sub['active']=False
            self.subscriptions=[]
        for client in clients:
            client.disconnect()

    def start(self,expression):
        request,target,metadata=selection(expression)
        return self.start_metadata(metadata,request.text,target)

    def start_metadata(self,metadata,expression=None,target=None):
        """Subscribe the manager's deduplicated demand pool, not a catalogue."""
        self.stop()
        with self.lock:
            generation=self.generation
            self.expression=expression
            self.target=target or {}
            self.metadata=dict(metadata)
            self.books={}
            self.errors=[]
            self.status='Authenticating'
        threading.Thread(target=self._connect,args=(generation,metadata),daemon=True,name='shared-live-feed').start()
        return len(metadata)

    def update_metadata(self,metadata):
        """Change subscriptions on existing sessions, keeping other prices live."""
        with self.lock:
            self.metadata=dict(metadata)
            self.accepted.intersection_update(metadata)
            self.books={tt:row for tt,row in self.books.items() if tt in metadata}
            self.cache={}
            generation=self.generation
        threading.Thread(target=self._connect,args=(generation,metadata),daemon=True,name='shared-subscription-update').start()

    def _connect(self,generation,metadata):
        with self.update_lock:
            self._synchronize(generation)

    def _synchronize(self,generation):
        tokens=set()
        def safe(error):
            message=str(error)
            for token in tokens:
                message=message.replace(token,'[redacted]')
            return message
        try:
            from lightstreamer.client import LightstreamerClient,Subscription,ClientListener,SubscriptionListener
            with self.lock:
                if generation!=self.generation:
                    return
                metadata=dict(self.metadata)
                auth=dict(self.auth)
            if metadata and any(meta['route'] not in auth for meta in metadata.values()):
                auth.update(credentials())
            tokens={token for _,token in auth.values()}
            grouped={}
            with self.lock:
                if generation!=self.generation:
                    return
                self.auth=auth
                metadata=dict(self.metadata)
                for sub in self.subscriptions:
                    pending=not set(sub['entries'])<=self.accepted and time.monotonic()-sub['created']>30
                    if sub['active'] and (sub.get('failed') or pending or not set(sub['entries'])<=metadata.keys()):
                        sub['active']=False
                        self.accepted.difference_update(sub['entries'])
                        sub['client'].unsubscribe(sub['subscription'])
                self.subscriptions=[s for s in self.subscriptions if s['active']]
                scheduled={tt for sub in self.subscriptions for tt in sub['entries']}
                for tt,meta in metadata.items():
                    if tt in scheduled:
                        continue
                    if meta['route'] not in auth:
                        self.errors.append('No credentials for '+meta['route'])
                        continue
                    grouped.setdefault(auth[meta['route']],[]).append(tt)
                if not self.clients:
                    self.status='Connecting' if grouped else ('No mapped credentials' if metadata else 'Idle')
            owner=self
            def build(user,token,ids,group):
                class Connection(ClientListener):
                    def onStatusChange(self,status):
                        with owner.lock:
                            if generation==owner.generation:
                                owner.groups[group]=status
                                owner.status='Connected' if any(s.startswith('CONNECTED:') for s in owner.groups.values()) else status
                    def onServerError(self,code,message):
                        with owner.lock:
                            if generation==owner.generation:
                                owner.errors.append(f'Server {code}: {safe(message)}')
                class Prices(SubscriptionListener):
                    def __init__(self,entries,state):
                        self.entries=entries
                        self.state=state
                    def onSubscription(self):
                        with owner.lock:
                            if generation==owner.generation and self.state['active']:
                                self.state['failed']=False
                                owner.accepted.update(set(self.entries) & owner.metadata.keys())
                    def onUnsubscription(self):
                        with owner.lock:
                            if generation==owner.generation and self.state['active']:
                                owner.accepted.difference_update(self.entries)
                    def onSubscriptionError(self,code,message):
                        with owner.lock:
                            if generation==owner.generation and self.state['active']:
                                owner.accepted.difference_update(self.entries)
                                self.state['failed']=True
                                owner.errors.append(f'Subscription {code}: {safe(message)}')
                    def onItemUpdate(self,update):
                        tt='TT-'+update.getItemName().removesuffix('-2')
                        now=datetime.now(timezone.utc)
                        with owner.lock:
                            if generation!=owner.generation or not self.state['active'] or tt not in owner.metadata:
                                return
                            old=owner.books.get(tt,{})
                            owner.books[tt]=dict(bid=number(update.getValue('bidPrice')),ask=number(update.getValue('askPrice')),
                                bid_size=number(update.getValue('bidQuantity')),ask_size=number(update.getValue('askQuantity')),
                                received_at=now.isoformat(),received=now.timestamp(),group=group,
                                snapshot=update.isSnapshot(),live_updates=old.get('live_updates',0)+int(not update.isSnapshot()))
                            book=owner.books[tt]
                            if (owner.recorder is not None and not book['snapshot'] and tt in owner.accepted
                                and owner.groups.get(group,'').startswith('CONNECTED:')
                                and all(book.get(side+'_size') is None or book[side+'_size']>0 for side in ('bid','ask'))):
                                owner.recorder.record(owner.metadata[tt],book['bid'],book['ask'],now)
                connection=owner.connections.get((user,token))
                client=connection['client'] if connection else LightstreamerClient(SERVER,'MARKET_DATA')
                if connection:
                    group=connection['group']
                else:
                    client.connectionDetails.setUser(user)
                    client.connectionDetails.setPassword(token)
                    client.connectionOptions.setReconnectTimeout(2500)
                    client.addListener(Connection())
                with owner.lock:
                    if generation!=owner.generation:
                        client.disconnect()
                        return
                    if not connection:
                        owner.clients.append(client)
                        owner.connections[(user,token)]=dict(client=client,group=group)
                    for offset in range(0,len(ids),250):
                        entries=ids[offset:offset+250]
                        sub=Subscription('MERGE',[tt.removeprefix('TT-')+'-2' for tt in entries],FIELDS)
                        sub.setDataAdapter('MDS')
                        sub.setRequestedSnapshot('yes')
                        sub.setRequestedMaxFrequency('1')
                        state=dict(client=client,subscription=sub,entries=entries,active=True,failed=False,created=time.monotonic())
                        sub.addListener(Prices(entries,state))
                        owner.subscriptions.append(state)
                        client.subscribe(sub)
                    if not connection:
                        client.connect()
            for (user,token),ids in grouped.items():
                build(user,token,ids,str(len(owner.connections)))
        except Exception as error:
            with self.lock:
                if generation==self.generation:
                    self.status='Connection error'
                    self.errors.append(safe(error))

    def snapshot(self,expression,allowed_ids=None,optimize=True):
        request=parse(expression)
        target={p+':'+c:w for (p,c),w in request.settlement_legs().items()}
        now=time.time()
        with self.lock:
            allowed=None if allowed_ids is None else set(allowed_ids)
            matched=target==self.target if allowed is None else allowed<=self.metadata.keys()
            books={}
            for tt,row in self.books.items():
                if allowed is not None and tt not in allowed:
                    continue
                fresh=tt in self.accepted and self.groups.get(row['group'],'').startswith('CONNECTED:') and 0<=now-row['received']<=120
                books[tt]={**row,'fresh':fresh,'bid':row['bid'] if fresh else None,'ask':row['ask'] if fresh else None}
                books[tt]['mid']=(row['bid']+row['ask'])/2 if fresh and row['bid'] is not None and row['ask'] is not None else None
            state=dict(status=self.status,matched=matched,requested=len(self.metadata) if allowed is None else len(allowed),
                       accepted=len(self.accepted if allowed is None else self.accepted & allowed),
                       received=len(books),errors=list(dict.fromkeys(self.errors))[-5:],books=books if matched else {},overall=None)
            entries=[(tt,self.metadata[tt]['weights'],row) for tt,row in books.items() if row['fresh'] and (row['bid'] is not None or row['ask'] is not None)]
            fingerprint=(self.generation,tuple(sorted(target.items())),tuple((tt,row['bid'],row['ask'],row['bid_size'],row['ask_size']) for tt,_,row in entries))
            cached=self.cache.get(fingerprint)
            recipe_key=(tuple(sorted(target.items())),tuple(sorted(allowed if allowed is not None else self.metadata)))
            previous=self.recipes.get(recipe_key,{})
        if matched and entries:
            if cached and time.monotonic()-cached[0]<2 and (not optimize or cached[1].get('optimization_attempted')):
                state['overall']=cached[1]
            else:
                def resolve_side(side):
                    seed=available_quote(request,target,entries,side,previous.get(side))
                    if not optimize:
                        return seed,False
                    try:
                        return optimize_side(target,entries,side,seed=seed)
                    except Exception:
                        state['errors'].append(side.capitalize()+' optimization failed; using available component quotes where possible')
                        return seed,False
                bid,bid_optimal=resolve_side('bid')
                ask,ask_optimal=resolve_side('ask')
                b=None if bid is None else bid['value']
                a=None if ask is None else ask['value']
                overall=dict(bid=b,ask=a,mid=(b+a)/2 if b is not None and a is not None else None,
                    bid_optimal=bid_optimal,ask_optimal=ask_optimal,
                    bid_components=0 if bid is None else bid['count'],ask_components=0 if ask is None else ask['count'],
                    bid_recipe=[] if bid is None else bid.get('recipe',[]),ask_recipe=[] if ask is None else ask.get('recipe',[]))
                overall['timestamp']=min(q['timestamp'] for q in (bid,ask) if q) if bid or ask else None
                overall['optimization_attempted']=optimize
                state['overall']=overall
                with self.lock:
                    if fingerprint[0]==self.generation:
                        self.cache[fingerprint]=(time.monotonic(),overall)
                        self.recipes[recipe_key]={side:q['recipe'] for side,q in (('bid',bid),('ask',ask)) if q}
                        while len(self.cache)>256:
                            self.cache.pop(next(iter(self.cache)))
                        while len(self.recipes)>256:
                            self.recipes.pop(next(iter(self.recipes)))
        overall=state['overall'] or {}
        alternatives,truncated=available_constructions(request,target,entries,
            [overall.get(side+'_recipe') for side in ('bid','ask')]) if matched and entries else ([],False)
        try:
            labels=names() if alternatives else {}
        except (OSError,ValueError,KeyError,TypeError):
            labels={}
        for alternative in alternatives:
            for part in alternative['components']:
                book=books[part['tt_id']]
                part.update(structure=labels.get(part['tt_id'],part['tt_id']),
                    bid=book['bid'],ask=book['ask'],timestamp=book['received_at'])
        state['alternatives']=alternatives
        state['alternatives_truncated']=truncated
        state['alternatives_scope']='Direct, named and spread constructions; up to 64 combinations per route, plus discovered mixes'
        available=[side for side in ('bid','ask') if overall.get(side) is not None]
        if len(available)==2:
            state['price_status']='available'
            state['price_reason']='' if all(overall.get(side+'_optimal') for side in available) else 'Best available price; optimum unconfirmed'
        elif available:
            state['price_status']='partial'
            state['price_reason']=available[0].capitalize()+' available; no complete fresh quote for the other side'
        else:
            state['price_status']='unavailable'
            state['price_reason']='No complete fresh bid/ask recipe available' if matched else 'Waiting for required subscriptions'
        return state



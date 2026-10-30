"""Forward curves: each shifted complete structure uses the common data client."""
import pandas as pd
import threading
import time
from analysis.expression import parse_expression
from market_data.queries import MarketData,database_signature
from market_data.data_client import DataClient
from market_data.structure_parser import MONTHS,month_code
from market_data.workflow_client import WorkflowLiveClient,full_structure

_settlement_cache={}
_cache_lock=threading.Lock()


class ForwardCurve:
    def __init__(self,live=None,db=None):
        self.signature=database_signature()
        self.live=live or WorkflowLiveClient('curve')
        self.db=db or MarketData()
        self.owned=db is None
        self.data=DataClient(database=self.db)

    @staticmethod
    def _format(legs):
        return ''.join(('-' if weight<0 else '+' if i else '')+
            (f'{abs(weight):g}*' if abs(weight)!=1 else '')+code for i,(weight,code) in enumerate(legs))

    def calculate(self,symbol,expression):
        legs=parse_expression(expression)
        if not legs:
            return dict(live=[],settlements=[])
        ordinal=lambda code:(2000+int(code[1:]))*12+MONTHS.index(code[0])
        first=min(ordinal(code) for _,code in legs)
        offsets=[(weight,ordinal(code)-first) for weight,code in legs]
        months=self.live.client.live_resolver._call('GET','/universe',params={'product':symbol})['months']
        available={ordinal(code) for code in months}
        today=pd.Timestamp.now(tz='UTC')
        first_active=today.year*12+today.month-1
        shifted=[]
        for anchor in sorted(available):
            if anchor<first_active or not all(anchor+offset in available for _,offset in offsets):
                continue
            raw=self._format([(weight,month_code(anchor+offset)) for weight,offset in offsets])
            shifted.append((anchor,raw,full_structure(symbol,raw)))
        self.live.replace([text for _,_,text in shifted])
        quotes=self.live.quotes(symbol,[raw for _,raw,_ in shifted])
        points=[dict(contract=raw,label=month_code(anchor),order=anchor,price=quotes.get(raw,{}).get('value'),
                     bid=quotes.get(raw,{}).get('bid'),ask=quotes.get(raw,{}).get('ask')) for anchor,raw,_ in shifted]
        signature=tuple(self.signature) if self.signature is not None else None
        cache_key=(symbol,tuple(shifted),signature)
        with _cache_lock:
            cached=_settlement_cache.get(cache_key)
        if cached and time.monotonic()-cached[0]<60:
            return dict(live=points,settlements=cached[1])
        settlements=[]
        for day in self.db.settlement_dates(symbol,limit=2):
            history=[]
            for anchor,raw,text in shifted:
                frame=self.data.seac(text,start=day,end=day)['data']
                if not frame.empty:
                    history.append(dict(contract=raw,label=month_code(anchor),order=anchor,price=float(frame['price'].iloc[-1])))
            settlements.append(dict(date=str(day),points=history))
        with _cache_lock:
            _settlement_cache[cache_key]=(time.monotonic(),settlements)
            while len(_settlement_cache)>16:
                _settlement_cache.pop(next(iter(_settlement_cache)))
        return dict(live=points,settlements=settlements)

    def close(self):
        self.data.close()
        if self.owned:
            self.db.close()

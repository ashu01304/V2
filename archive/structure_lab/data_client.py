"""One experimental entry point; dashboards supply named structures only."""
from datetime import date
import hashlib
import json
import pandas as pd
from .expression import parse
from .live_manager import LiveManager
from .resolvers import live,minute,seac


class DataClient:
    def __init__(self,runtime_path=None):
        self.manager=LiveManager() if runtime_path is None else LiveManager(runtime_path)

    def resolve(self,expression,source='live',profile='demo',start=None,end=None,interval=15,
                service_url='http://127.0.0.1:8060',best_sides=False):
        if source not in {'live','seac','minute'} or profile not in {'demo','existing'}:
            raise ValueError('Choose live, seac or minute, and demo or existing')
        if len(expression or '')>2048:
            raise ValueError('Limit expressions to 2048 characters')
        request=parse(expression)
        result=dict(request=request.text,source=source,profile=profile,named=request.named_rows(),legs=request.leg_rows())
        if source=='live':
            quote,recipe=live.resolve(request,profile,service_url,best_sides)
            key=hashlib.sha256(json.dumps([request.named_rows(),profile,service_url,best_sides],sort_keys=True).encode()).hexdigest()
            result.update(quote=quote,recipe=recipe,data=self.manager.record(key,request,profile,quote,recipe))
        else:
            start=pd.Timestamp(start or date.today()).date()
            end=pd.Timestamp(end or date.today()).date()
            if start>end or (end-start).days>3660:
                raise ValueError('Choose an ordered date range of at most ten years')
            if source=='seac':
                frame,recipe=seac.resolve(request,start,end,profile)
            else:
                if interval not in {1,5,15,30,60,240,1440}:
                    raise ValueError('Unsupported candle interval')
                frame,recipe=minute.resolve(request,start,end,interval,profile)
            records=json.loads(frame.to_json(orient='records',date_format='iso'))
            result.update(data=records,recipe=recipe,rows=len(frame))
        return result

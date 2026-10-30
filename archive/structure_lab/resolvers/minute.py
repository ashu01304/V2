"""Resolve from stored spread observations, align minutes, then form candles."""
from collections import defaultdict
import pandas as pd
from ..expression import MONTHS
from ..sources import align_demo, read_database


def recipe_for(request,stored=True):
    products=defaultdict(list)
    for (product,contract),weight in request.settlement_legs().items():
        products[product].append((contract,weight))
    recipe=defaultdict(float)
    for product,legs in products.items():
        legs.sort(key=lambda p:(int(p[0][1:]),MONTHS.index(p[0][0])))
        if abs(sum(w for _,w in legs))>1e-9:
            raise ValueError('Minute data requires zero net exposure per product; outright series are not stored')
        prefix={'CO':'LCO','G':'LGO'}.get(product,product) if stored else product
        stored=lambda c:c[0]+c[-1] if prefix in {'LCO','LGO'} else c
        cumulative=0
        for i,(contract,weight) in enumerate(legs[:-1]):
            cumulative+=weight
            if abs(cumulative)>1e-9:
                recipe[prefix+stored(contract)+'-'+stored(legs[i+1][0])]+=cumulative
    return {instrument:weight for instrument,weight in recipe.items() if abs(weight)>1e-9}


def resolve(request,start,end,interval=15,profile='demo',database=None):
    recipe=recipe_for(request)
    start=pd.Timestamp(start,tz='UTC')
    end=pd.Timestamp(end,tz='UTC')+pd.Timedelta(days=1)
    if end-start>pd.Timedelta(days=31):
        raise ValueError('Limit minute tests to 31 days')
    if profile=='demo':
        frame=align_demo(request.settlement_legs(),pd.date_range(start,end,freq='min',inclusive='left'))
    else:
        values=','.join('(?,?)' for _ in recipe)
        parameters=[v for item,weight in recipe.items() for v in (item,weight)]
        sql=f'''WITH recipe(instrument,weight) AS (VALUES {values})
          SELECT p.timestamp AS time,SUM(p.price*r.weight) AS price
          FROM spread_prices p JOIN recipe r USING(instrument)
          WHERE p.timestamp>=? AND p.timestamp<? AND isfinite(p.price)
          GROUP BY p.timestamp HAVING COUNT(DISTINCT p.instrument)=? ORDER BY time'''
        with read_database() if database is None else read_database(database) as connection:
            frame=connection.execute(sql,[*parameters,start,end,len(recipe)]).fetchdf()
    if frame.empty:
        return pd.DataFrame(columns=['time','open','high','low','close']),recipe
    frame['time']=pd.to_datetime(frame['time'],utc=True)
    candles=frame.set_index('time')['price'].resample(f'{int(interval)}min').ohlc().dropna().reset_index()
    return candles,recipe

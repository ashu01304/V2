"""Resolve named structures against settlement observations."""
import pandas as pd
from ..sources import align_demo, read_database


def resolve(request,start,end,profile='demo',database=None):
    legs=request.settlement_legs()
    recipe=[dict(product=p,month=c,weight=w) for (p,c),w in legs.items()]
    if profile=='demo':
        frame=align_demo(legs,pd.date_range(start,end,freq='B',tz='UTC'))
    else:
        values=','.join('(?,?,?)' for _ in legs)
        parameters=[v for (p,c),w in legs.items() for v in (p,c,w)]
        sql=f'''WITH recipe(symbol,contract_code,weight) AS (VALUES {values})
          SELECT s.trading_date AS time,SUM(s.price*r.weight) AS price
          FROM seac_settlements s JOIN recipe r USING(symbol,contract_code)
          WHERE s.trading_date BETWEEN ? AND ? AND isfinite(s.price)
          GROUP BY s.trading_date HAVING COUNT(DISTINCT (s.symbol,s.contract_code))=? ORDER BY time'''
        with read_database() if database is None else read_database(database) as connection:
            frame=connection.execute(sql,[*parameters,start,end,len(legs)]).fetchdf()
    return frame,recipe

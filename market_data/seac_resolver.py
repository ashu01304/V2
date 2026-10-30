"""Complete structure -> aligned SEAC settlements, using read-only MarketData."""
import pandas as pd
from .structure_parser import parse_structure


def plan(structure):
    request=parse_structure(structure)
    return [dict(product=p,contract=c,weight=w) for (p,c),w in request.settlement_legs().items()]


def resolve(structure, database, *, start, end):
    request=parse_structure(structure)
    start,end=pd.Timestamp(start),pd.Timestamp(end)
    if pd.isna(start) or pd.isna(end) or start.date()>end.date():
        raise ValueError('Provide ordered settlement dates, inclusive of start and end')
    frame=database.structure_settlements(request.settlement_legs(),start.date(),end.date())
    frame['timestamp']=pd.to_datetime(frame['timestamp'])
    return dict(structure=request.text,source='seac',status='available' if not frame.empty else 'no_data',
                data=frame,recipe=plan(structure))

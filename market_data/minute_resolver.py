"""Complete structure -> aligned stored minute prices -> optional OHLC candles."""
from collections import defaultdict
from heapq import heappop, heappush
import re
import pandas as pd
from .contracts import price_recipe
from .structure_parser import parse_structure,MONTHS
from .minute_quality import normalize_minutes


def plan(structure):
    request=parse_structure(structure)
    recipe=defaultdict(float)
    for product in sorted({p for p,_ in request.exposures()}):
        expression=request.expression(product)
        for instrument,weight in price_recipe(product,expression).items():
            recipe[instrument]+=weight
    result={instrument:weight for instrument,weight in recipe.items() if abs(weight)>1e-10}
    if not result:
        raise ValueError('Structure has no stored minute exposure')
    return result


def interval_minutes(interval):
    if isinstance(interval,int) and not isinstance(interval,bool):
        minutes=interval
    else:
        match=re.fullmatch(r'([1-9]\d*)(min|m|h|d)',str(interval).strip().lower())
        if not match:
            raise ValueError('Use an interval such as 15min, 1h, 1d or integer minutes')
        minutes=int(match[1])*{'min':1,'m':1,'h':60,'d':1440}[match[2]]
    if not 1<=minutes<=1440:
        raise ValueError('Candle interval must be between 1 and 1440 minutes')
    return minutes


def stored_recipe(request,database,start,end,*,fetched_only=False,fill_live_gaps=False):
    """Use actual stored calendar edges; do not guess symbol formats or fill prices."""
    products=defaultdict(dict)
    for (product,month),weight in request.settlement_legs().items():
        products[product][(2000+int(month[1:]))*12+MONTHS.index(month[0])]=weight
    edges=[]
    for instrument in database.spreads():
        match=re.fullmatch(r'(CL|LCO|CO|LGO|GO|G|HO|NG|RB)([FGHJKMNQUVXZ])(\d{1,2})-([FGHJKMNQUVXZ])(\d{1,2})',instrument)
        if not match:
            continue
        prefix,first,year1,second,year2=match.groups()
        product={'LCO':'CO','LGO':'G','GO':'G'}.get(prefix,prefix)
        if product not in products:
            continue
        low,high=min(products[product])-6,max(products[product])+6
        def ordinal(month,year):
            years=[2000+int(year)] if len(year)==2 else [y for y in range(2000,2100) if y%10==int(year)]
            matches=[y*12+MONTHS.index(month) for y in years if low<=y*12+MONTHS.index(month)<=high]
            return matches[0] if len(matches)==1 else None
        a,b=ordinal(first,year1),ordinal(second,year2)
        if a is not None and b is not None and a!=b:
            edges.append((product,a,b,instrument))
    candidates=[edge[3] for edge in edges]
    source_options={'fetched_only':True} if fetched_only else {}
    if fill_live_gaps:
        source_options['fill_live_gaps']=True
    coverage=database.spread_coverage(candidates,start.to_pydatetime(),end.to_pydatetime(),**source_options)
    counts={row.instrument:row.rows for row in coverage.itertuples(index=False)}
    recipe=defaultdict(float)
    missing=[]
    for product,wanted in products.items():
        if abs(sum(wanted.values()))>1e-9:
            raise ValueError('Minute data requires zero net exposure per product')
        graph=defaultdict(list)
        # Prefer complete-year codes, then higher coverage when equivalent edges exist.
        ordered=sorted((edge for edge in edges if edge[0]==product and counts.get(edge[3],0)>0),
                       key=lambda edge:(-sum(len(y) for y in re.findall(r'\d+',edge[3])),-counts[edge[3]],edge[3]))
        for _,a,b,instrument in ordered:
            graph[a].append((b,instrument,1))
            graph[b].append((a,instrument,-1))
        anchor=min(wanted)
        parents={anchor:None}
        # Maximise the weakest edge's stored coverage before minimising leg count.
        # A newly recorded direct spread must not displace a well-covered calendar chain.
        best={anchor:(float('inf'),0)}
        queue=[(-float('inf'),0,anchor)]
        while queue:
            negative_coverage,hops,current=heappop(queue)
            available=-negative_coverage
            if best[current] != (available,-hops):
                continue
            for neighbor,instrument,direction in graph[current]:
                candidate=(min(available,counts[instrument]),-hops-1)
                if candidate > best.get(neighbor,(-1,-float('inf'))):
                    best[neighbor]=candidate
                    parents[neighbor]=(current,instrument,direction)
                    heappush(queue,(-candidate[0],hops+1,neighbor))
        for month,weight in wanted.items():
            if month not in parents:
                missing.append(product+' '+MONTHS[month%12]+f'{month//12%100:02d}')
                continue
            current=month
            while parents[current] is not None:
                previous,instrument,direction=parents[current]
                recipe[instrument]-=weight*direction
                current=previous
    diagnostics=dict(candidates=candidates,coverage=coverage.to_dict('records'),missing=missing)
    if missing:
        return {},diagnostics
    return {instrument:weight for instrument,weight in recipe.items() if abs(weight)>1e-9},diagnostics


def resolve(structure, database, *, start, end, interval='15min', fetched_only=False, fill_live_gaps=False,
            normalize_spikes=True):
    request=parse_structure(structure)
    start,end=pd.Timestamp(start),pd.Timestamp(end)
    if pd.isna(start) or pd.isna(end):
        raise ValueError('Provide a minute start and end time')
    start=start.tz_localize('UTC') if start.tzinfo is None else start.tz_convert('UTC')
    end=end.tz_localize('UTC') if end.tzinfo is None else end.tz_convert('UTC')
    if start>=end:
        raise ValueError('Minute start must be before end; end is exclusive')
    minutes=None if interval is None else interval_minutes(interval)
    recipe,diagnostics=stored_recipe(request,database,start,end,fetched_only=fetched_only,fill_live_gaps=fill_live_gaps)
    source_options={'fetched_only':True} if fetched_only else {}
    if fill_live_gaps:
        source_options['fill_live_gaps']=True
    frame=(database.structure_minutes(recipe,start.to_pydatetime(),end.to_pydatetime(),**source_options) if recipe else
           pd.DataFrame({'timestamp':pd.Series(dtype='datetime64[ns, UTC]'),'price':pd.Series(dtype=float)}))
    frame['timestamp']=pd.to_datetime(frame['timestamp'],utc=True)
    # Filter the complete structure before OHLC aggregation, so artificial wicks
    # caused by combinations of otherwise smooth legs can also be detected.
    diagnostics['normalized_spikes']=0
    if normalize_spikes:
        tick=min({'HO':.0001,'RB':.0001,'NG':.001,'G':.25}.get(product,.01)
                 for product,_ in request.settlement_legs())
        frame,diagnostics['normalized_spikes']=normalize_minutes(frame,tick)
    diagnostics['dropped_spikes']=diagnostics['normalized_spikes']
    if minutes is not None:
        frame=(frame.set_index('timestamp')['price'].resample(f'{minutes}min',origin='epoch')
               .ohlc().dropna().reset_index())
    message=('No stored spread path with observations in this window for: '+', '.join(diagnostics['missing'])
             if diagnostics['missing'] else 'Required stored spreads have no exact common timestamps' if frame.empty else '')
    if frame.empty and fetched_only and not fill_live_gaps:
        message='No fetched-only minute data with all required legs available. '+message
    return dict(structure=request.text,source='minute',status='available' if not frame.empty else 'no_data',
                data=frame,recipe=recipe,diagnostics=diagnostics,message=message)


def snapshots(selections,timestamps,database):
    targets=[]
    for value in timestamps:
        timestamp=pd.Timestamp(value)
        if pd.isna(timestamp) or timestamp.tzinfo is None or timestamp!=timestamp.floor('min'):
            raise ValueError('Snapshot timestamps must be timezone-aware whole minutes')
        timestamp=timestamp.tz_convert('UTC')
        if timestamp not in targets:
            targets.append(timestamp)
    result={timestamp:{} for timestamp in targets}
    if not targets or not selections:
        return result
    for key,structure in selections:
        frame=resolve(structure,database,start=min(targets),end=max(targets)+pd.Timedelta(minutes=1),interval=None)['data']
        prices=frame.set_index('timestamp')['price']
        for timestamp in targets:
            if timestamp in prices.index:
                result[timestamp][key]=float(prices.loc[timestamp])
    return result


def calendar_histograms(filters,database,*,start,end):
    """Retain the existing calendar-year filtering query behind this resolver."""
    return database.calendar_filter_histograms(filters,pd.Timestamp(start),pd.Timestamp(end))

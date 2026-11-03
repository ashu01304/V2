"""User-run Phase 1 checks. No servers/data imports; master data stays read-only."""
import argparse
from contextlib import closing
from datetime import datetime,timezone
import math
import time
import traceback
import uuid
import pandas as pd
from market_data.config import DATABASE_PATH,LIVE_MANAGER_URL
from market_data.data_client import DataClient
from market_data.queries import MarketData
from market_data.structure_parser import parse_structure,from_legacy

DEFAULT_STRUCTURES=['CL X27 1MDF','CO X27 3MS','CL X27 1MDF - CO X27 3MS']


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--structure',action='append',help='Repeat to replace the three default structures')
    parser.add_argument('--start',help='UTC start; default 14 days before end')
    parser.add_argument('--end',help='UTC end; default now; exclusive for minute data')
    parser.add_argument('--database',default=str(DATABASE_PATH))
    parser.add_argument('--live-url',default=LIVE_MANAGER_URL)
    parser.add_argument('--wait',type=int,default=30,help='Maximum live waiting time in seconds (0–120)')
    parser.add_argument('--skip-live',action='store_true')
    parser.add_argument('--daily',action='store_true',help='Inspect recent daily candle extrema and their stored spread components')
    args=parser.parse_args()
    if not 0<=args.wait<=120:
        parser.error('--wait must be between 0 and 120')
    structures=list(dict.fromkeys(args.structure or DEFAULT_STRUCTURES))
    if len(structures)>64:
        parser.error('Use at most 64 structures')
    end=pd.Timestamp(args.end) if args.end else pd.Timestamp.now(tz='UTC')
    end=end.tz_localize('UTC') if end.tzinfo is None else end.tz_convert('UTC')
    start=pd.Timestamp(args.start) if args.start else end.normalize()-pd.Timedelta(days=14)
    start=start.tz_localize('UTC') if start.tzinfo is None else start.tz_convert('UTC')
    if pd.isna(start) or pd.isna(end) or start>=end:
        parser.error('Provide valid start and end times, with start before end')
    counts=dict(PASS=0,WARN=0,FAIL=0)

    def report(status,message):
        counts[status]+=1
        print(f'{status}: {message}',flush=True)

    def check(label,operation):
        try:
            operation()
        except Exception as error:
            report('FAIL',f'{label}: {type(error).__name__}: {error}')
            print(traceback.format_exc(limit=3),flush=True)

    def equal(actual,expected):
        if not math.isfinite(float(actual)) or not math.isclose(float(actual),float(expected),rel_tol=1e-8,abs_tol=1e-8):
            raise AssertionError(f'Actual {actual!r} != expected {expected!r}')

    print('PHASE 1 MANUAL CHECK | '+datetime.now(timezone.utc).isoformat(),flush=True)
    print(f'Database: {args.database}\nUTC range: {start} -> {end}\nManager: {args.live_url}',flush=True)
    print('Historical access is read-only. Live checks renew shared structures; unused quotes expire after 8 hours.\n',flush=True)
    client=DataClient(args.database,live_url=args.live_url,owner='manual-check-'+uuid.uuid4().hex[:12])
    try:
        def parser_checks():
            expected={('CL','X27'):1,('CL','Z27'):-3,('CL','F28'):3,('CL','G28'):-1}
            assert parse_structure('CL X27 1MDF').settlement_legs()==expected
            assert parse_structure('CL X271MF - CL Z271MF').settlement_legs()==expected
            converted=from_legacy('CL','X27-3*Z27+3*F28-G28')
            assert parse_structure(converted).settlement_legs()==expected
            assert parse_structure(converted).terms[0].kind=='DF'
            assert [w for _,w in parse_structure('CL X27 1MDDF').terms[0].legs()]==[1,-4,6,-4,1]
            for source in ('minute','live'):
                try:
                    client.plan('CL X27 OUT',source)
                except ValueError:
                    continue
                raise AssertionError(f'{source} accepted an unsupported outright')
            report('PASS','Parser: MDF, MDDF, compact notation, legacy conversion and outright rejection')
        check('Parser',parser_checks)

        for structure in structures:
            print('\n=== '+structure+' ===',flush=True)
            def plans():
                for source in ('seac','minute','live'):
                    print(source.upper()+' recipe: '+repr(client.plan(structure,source)),flush=True)
                report('PASS','All three recipes resolved')
            check('Recipes',plans)

            def settlements():
                result=client.seac(structure,start=start.date(),end=end.date())
                frame=result['data']
                if frame.empty:
                    report('WARN','SEAC: no common observations in this date range; not a reconstruction pass')
                    return
                assert frame['timestamp'].is_unique and frame['timestamp'].is_monotonic_increasing
                samples=frame.tail(3)
                with closing(MarketData(args.database)) as db:
                    parts={}
                    for leg in result['recipe']:
                        history=db.settlement(leg['product'],leg['contract'])
                        parts[(leg['product'],leg['contract'])]=history.set_index(pd.to_datetime(history['trading_date']))['price']
                    for row in samples.itertuples(index=False):
                        expected=sum(leg['weight']*parts[(leg['product'],leg['contract'])].loc[row.timestamp]
                                     for leg in result['recipe'])
                        equal(row.price,expected)
                print(samples.to_string(index=False),flush=True)
                report('PASS',f'SEAC: {len(frame)} dates; last {len(samples)} independently checked against individual settlements')
            check('SEAC',settlements)

            def minutes():
                raw=client.minute(structure,start=start,end=end,interval=None)
                frame=raw['data']
                print('Resolved stored minute recipe: '+repr(raw['recipe']),flush=True)
                if frame.empty:
                    report('WARN','Minute: '+raw.get('message','No common observations'))
                    diagnostics=raw.get('diagnostics',{})
                    candidates=diagnostics.get('candidates',list(client.plan(structure,'minute')))
                    window={row['instrument']:row['rows'] for row in diagnostics.get('coverage',[])}
                    print(f'Stored candidates recognised: {len(candidates)}; with observations in requested window: {len(window)}',flush=True)
                    with closing(MarketData(args.database)) as db:
                        history=db.spread_coverage(candidates)
                        if not history.empty:
                            history['rows_in_window']=history['instrument'].map(window).fillna(0).astype(int)
                            history=history.sort_values(['rows_in_window','last'],ascending=False)
                            print('Actual stored coverage (up to 12 candidates):\n'+history.head(12).to_string(index=False),flush=True)
                        else:
                            print('No history for these candidate IDs. Stored-name examples: '+repr(db.spreads()[:15]),flush=True)
                    return
                assert frame['timestamp'].is_unique and frame['timestamp'].is_monotonic_increasing
                samples=frame.tail(3)
                with closing(MarketData(args.database)) as db:
                    parts={}
                    for instrument in raw['recipe']:
                        series=db.spread(instrument,start=samples['timestamp'].min().to_pydatetime(),
                                         end=samples['timestamp'].max().to_pydatetime())
                        parts[instrument]=series.set_index(pd.to_datetime(series['timestamp'],utc=True))['price']
                    for row in samples.itertuples(index=False):
                        equal(row.price,sum(weight*parts[instrument].loc[row.timestamp] for instrument,weight in raw['recipe'].items()))
                report('PASS',f'Minute: {len(frame)} observations; last {len(samples)} independently checked against stored spreads')
                if args.daily:
                    points=frame.set_index('timestamp')['price']
                    daily=points.resample('1D').ohlc().dropna()
                    daily['minute_points']=points.resample('1D').count().reindex(daily.index)
                    print('\nDAILY UTC CANDLES (last 10 observed days):\n'+daily.tail(10).to_string(),flush=True)
                    extremes=[]
                    for day in daily.tail(5).index:
                        values=points.loc[(points.index>=day)&(points.index<day+pd.Timedelta(days=1))]
                        for side,stamp in [('low',values.idxmin()),('high',values.idxmax())]:
                            extremes.append(dict(day=str(day.date()),side=side,timestamp=stamp,structure_price=points.loc[stamp]))
                    with closing(MarketData(args.database)) as db:
                        for instrument,weight in raw['recipe'].items():
                            series=db.spread(instrument,start=min(row['timestamp'] for row in extremes).to_pydatetime(),
                                             end=max(row['timestamp'] for row in extremes).to_pydatetime())
                            lookup=series.set_index(pd.to_datetime(series['timestamp'],utc=True))['price']
                            for row in extremes:
                                price=float(lookup.loc[row['timestamp']])
                                row[f'{weight:g} * {instrument}']=price*weight
                    print('\nPRICES BEHIND EACH DAILY EXTREME (columns are weighted contributions):\n'+
                          pd.DataFrame(extremes).to_string(index=False),flush=True)
                    for row in extremes:
                        equal(row['structure_price'],sum(value for key,value in row.items() if ' * ' in key))
                    print('Extrema match stored components. This checks reconstruction, not whether source quotes are valid.',flush=True)
                candles=client.minute(structure,start=start,end=end,interval='15min')['data']
                if candles.empty:
                    raise AssertionError('Raw minute data exists but candles are empty')
                for candle in candles.tail(3).itertuples(index=False):
                    values=frame.loc[(frame['timestamp']>=candle.timestamp)&
                                     (frame['timestamp']<candle.timestamp+pd.Timedelta(minutes=15)),'price']
                    if values.empty:
                        raise AssertionError('Candle has no corresponding raw points')
                    for actual,expected in zip((candle.open,candle.high,candle.low,candle.close),
                                               (values.iloc[0],values.max(),values.min(),values.iloc[-1])):
                        equal(actual,expected)
                print(candles.tail(3).to_string(index=False),flush=True)
                report('PASS',f'15-minute OHLC: {len(candles)} candles; last three checked against raw points')
            check('Minute/OHLC',minutes)

        if args.skip_live:
            report('WARN','Live checks skipped by --skip-live')
        else:
            def live():
                print('\n=== LIVE BATCH ===',flush=True)
                deadline=time.monotonic()+args.wait
                while True:
                    results=client.live_many(structures)
                    complete=all((r.get('data') or {}).get('bid') is not None and
                                 (r.get('data') or {}).get('ask') is not None for r in results)
                    if complete or all(r['status']=='unavailable' and r.get('data') is None for r in results) or time.monotonic()>=deadline:
                        break
                    time.sleep(1)
                for result in results:
                    quote=result.get('data') or {}
                    bid,ask,mid=quote.get('bid'),quote.get('ask'),quote.get('mid')
                    print(dict(structure=result['structure'],status=result['status'],bid=bid,ask=ask,average=mid,
                               timestamp=quote.get('timestamp'),bid_optimal=quote.get('bid_optimal'),
                               ask_optimal=quote.get('ask_optimal'),message=result.get('message')),flush=True)
                    if bid is not None and ask is not None:
                        equal(mid,(bid+ask)/2)
                        equal(quote['value'],mid)
                        report('PASS','Live: both sides and midpoint received for '+result['structure'])
                    elif bid is not None or ask is not None:
                        assert mid is None
                        report('WARN','Live: only one side is available for '+result['structure'])
                    else:
                        report('WARN','Live: no usable prices for '+result['structure']+'; inspect manager/market availability')
            check('Live batch',live)
            def recorder_status():
                status=client.live_resolver._call('GET','/status').get('minute_recording')
                print('\nMINUTE RECORDING:',status,flush=True)
                if not status:
                    report('WARN','Manager does not report minute recording; restart market_data.live_server')
                elif not status.get('running') or status.get('last_error'):
                    report('WARN','Minute recorder is stopped or has a pending error; inspect manager status')
                else:
                    report('PASS','Minute recorder worker is running (publication values shown above; not independently verified)')
            check('Minute recorder status',recorder_status)
    finally:
        for structure in list(client.live_resolver.active):
            try:
                client.disconnect([structure])
            except Exception as error:
                report('WARN',f'Could not release {structure}: {error}; its lease will expire')
        client.close()
        print('\nSUMMARY: '+', '.join(f'{key}={value}' for key,value in counts.items()),flush=True)
        print('WARN means unavailable/skipped coverage; it is not a successful price check. Paste this output for review.',flush=True)
    return 1 if counts['FAIL'] else 0


if __name__=='__main__':
    raise SystemExit(main())

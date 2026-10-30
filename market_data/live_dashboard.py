"""Multi-structure live watchlist. Run: python -B -m market_data.live_dashboard."""
import argparse
import time
import requests
from dash import Dash,Input,Output,State,ctx,dcc,html,no_update
from .live_resolver import LiveResolver


class LivePriceClient(LiveResolver):
    """Small HTTP adapter for the watchlist; the manager owns every shared quote."""
    def register(self,structures):
        return self._call('POST','/requests',json=dict(structures=structures))['requests']

    def renew(self,ids):
        return self._call('POST','/requests/heartbeat',json=dict(request_ids=ids))['renewed']

    def quotes(self,ids,include_details=False):
        return self._call('POST','/quotes',json=dict(request_ids=ids,include_details=include_details))['quotes']

app=Dash(__name__,title='Live structures',include_assets_files=False)
app.index_string=app.index_string.replace('</head>','''<style>
:root{color-scheme:dark}*{box-sizing:border-box}body{margin:0;background:#090909;color:#eee;font:14px Segoe UI,Arial,sans-serif}
main{max-width:1400px;margin:auto;padding:24px}h2{font-size:22px;margin:0 0 10px}p,.status{color:#aaa;line-height:1.6}
label{display:block;margin:18px 0 8px}textarea{width:100%;min-height:110px;resize:vertical;background:#141414;color:#fff;border:1px solid #555;border-radius:5px;padding:12px;font:15px/1.7 Consolas,monospace}
.controls{display:flex;gap:10px;align-items:center;margin:12px 0;flex-wrap:wrap}button{padding:9px 15px;border:1px solid #666;border-radius:4px;background:#eee;color:#111;cursor:pointer;font:inherit}button.secondary{background:#171717;color:#ddd}
.table-wrap{overflow-x:auto;margin-top:22px}table{border-collapse:collapse;width:100%}th,td{border-bottom:1px solid #333;padding:13px 12px;text-align:left}th{color:#aaa;font-size:12px}td.structure{font-family:Consolas,monospace}td.price,th.price{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}td.status{font-size:12px}footer{color:#888;font-size:12px;margin-top:20px}
</style></head>''')


def layout():
    return html.Main([
        html.H2('Live structures'),
        html.P('Enter one complete structure per line. Connect / update replaces your watchlist.'),
        html.Label('Structures'),
        dcc.Textarea(id='structures',value='CL X27 1MDF\nCO X27 3MS\nCL X27 1MDF - CO X27 3MS'),
        html.Div([html.Button('Connect / update',id='connect'),
                  html.Button('Disconnect all',id='disconnect',className='secondary'),
                  html.Span(id='action',className='status')],className='controls'),
        html.Div(html.Table([
            html.Thead(html.Tr([html.Th('Structure'),*[html.Th(label,className='price') for label in ('Best bid','Best ask','Average')],html.Th('Status')])),
            html.Tbody(id='prices'),
        ]),className='table-wrap'),
        html.Div(id='message',className='status'),
        html.Footer('Prices come only from the shared manager on port 8071. Average = (best bid + best ask) / 2. Missing prices remain blank.'),
        dcc.Store(id='watchlist',data=[],storage_type='session'),
        dcc.Store(id='heartbeat',data=0),dcc.Interval(id='live-tick',interval=1000),
    ])


app.layout=layout


@app.callback(Output('watchlist','data'),Output('action','children'),
    Input('connect','n_clicks'),Input('disconnect','n_clicks'),
    State('structures','value'),prevent_initial_call=True)
def connect(_connect,_disconnect,text):
    client=LivePriceClient()
    try:
        if ctx.triggered_id=='disconnect':
            rows=[]
        else:
            expressions=list(dict.fromkeys(line.strip() for line in (text or '').splitlines() if line.strip()))
            if not 1<=len(expressions)<=64:
                return no_update,'Enter between 1 and 64 structures, one per line.'
            rows=client.register(expressions)
        message=f'{len(rows)} structures connected.' if rows else 'Watchlist disconnected.'
        message+=' Unused shared quotes expire after 8 hours.'
        return rows,message
    except (ValueError,requests.RequestException) as error:
        return no_update,str(error)
    finally:
        client.session.close()


def table_rows(watched,results):
    def price(value):
        return '\u2014' if value is None else f'{value:.6f}'.rstrip('0').rstrip('.')
    rows=[]
    for request,result in zip(watched,results):
        quote=(result or {}).get('quote') or {}
        overall=quote.get('overall') or {}
        status=quote.get('status') or (result or {}).get('status') or ('Waiting for prices' if result else 'Request expired')
        if quote.get('price_reason'):
            status+=' | '+quote['price_reason']
        elif any(overall.get(side) is not None and not overall.get(side+'_optimal') for side in ('bid','ask')):
            status+=' | best found; optimum unconfirmed'
        if quote.get('errors'):
            status+=' | '+'; '.join(quote['errors'])
        rows.append(html.Tr([html.Td(request['expression'],className='structure'),
            *[html.Td(price(overall.get(side)),className='price') for side in ('bid','ask','mid')],
            html.Td(status,className='status')]))
    return rows


@app.callback(Output('prices','children'),Output('message','children'),Output('heartbeat','data'),
    Input('watchlist','data'),Input('live-tick','n_intervals'),State('heartbeat','data'))
def refresh(watched,_tick,last_heartbeat):
    if not watched:
        return [],'No structures connected.',0
    client=LivePriceClient()
    ids=[row['request_id'] for row in watched]
    try:
        now=time.time()
        if now-(last_heartbeat or 0)>=15 or ctx.triggered_id=='watchlist':
            if client.renew(ids)<len(ids):
                client.register([row['expression'] for row in watched])
            last_heartbeat=now
        results=client.quotes(ids,include_details=False)
        return table_rows(watched,results),f'{len(watched)} structures | updates every second',last_heartbeat
    except (ValueError,requests.RequestException) as error:
        # Clear old prices instead of presenting them as current after an outage.
        return table_rows(watched,[{'status':'Manager unavailable'} for _ in watched]),str(error),last_heartbeat
    finally:
        client.session.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port',type=int,default=8070)
    args=parser.parse_args()
    app.run(host='127.0.0.1',port=args.port,debug=False)

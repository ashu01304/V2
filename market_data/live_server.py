"""Shared live publisher and small subscription monitor. Run with -m."""
import argparse
from flask import Flask,jsonify,request
from .live_manager import PriceManager
from .quote_catalogue import index, names
from .structure_parser import ALIASES,MONTHS
from .config import LIVE_MANAGER_URL, LIVE_SUBSCRIPTION_TTL_SECONDS
from .publication import writer_lock
from .live_request_store import DEFAULT_PATH
from urllib.parse import urlparse

MONITOR='''<!doctype html><meta charset="utf-8"><title>Live manager</title>
<style>body{background:#090909;color:#eee;font:14px Arial;margin:30px}h2{font-size:20px}
table{border-collapse:collapse;width:100%;margin:18px 0}td,th{text-align:left;border-bottom:1px solid #333;padding:8px}
summary{cursor:pointer}#status{color:#aaa}code{font-size:12px}</style>
<h2>Live subscription manager</h2><div id="status">Loading…</div><p id="minute-status"></p>
<h3>Shared structures</h3><table><thead><tr><th>Structure</th><th>IDs</th><th>Expires in</th></tr></thead><tbody id="requests"></tbody></table>
<details><summary>Subscribed contract IDs</summary><table><thead><tr><th>Product</th><th>Structure</th><th>TT ID</th><th>Accepted</th><th>Received</th></tr></thead><tbody id="contracts"></tbody></table></details>
<script>
function rows(id,items){const target=document.getElementById(id);target.replaceChildren();
 for(const values of items){const row=document.createElement('tr');for(const value of values){
 const cell=document.createElement('td');cell.textContent=value;row.appendChild(cell);}target.appendChild(row);}}
async function update(){try{const response=await fetch('/status');if(!response.ok)throw Error();const s=await response.json();
 document.getElementById('status').textContent=`${s.status} | ${s.active_requests} unique structures | ${s.subscribed}/${s.required} IDs accepted | ${s.received} received`+
 (s.manager_error?' | '+s.manager_error:'')+(s.errors.length?' | '+s.errors.join('; '):'');
 const m=s.minute_recording;
 document.getElementById('minute-status').textContent=m ?
 `Minute recording: ${m.running?'running':'stopped'} | ${m.received_updates} received updates | ${m.queued_closes} buffered closes | ${m.pending_updates} pending | last publish: ${m.last_publish_at || 'waiting'} | ${m.last_published_spreads} provisional points`+
 (m.last_error?' | '+m.last_error:'') : 'Minute recording status unavailable';
 rows('requests',s.requests.map(r=>[r.expression,r.required_ids,Math.max(0,Math.round(r.expires_at-s.server_time))+'s']));
 rows('contracts',s.contracts.map(r=>[r.product,r.structure,r.tt_id,r.accepted?'Yes':'Waiting',r.received?'Yes':'Waiting']));
 }catch(e){document.getElementById('status').textContent='Manager unavailable';}finally{setTimeout(update,2000);}}
update();</script>'''


def create_app(manager):
    app=Flask(__name__)
    app.config['MAX_CONTENT_LENGTH']=256*1024

    @app.errorhandler(ValueError)
    def invalid(error):
        return jsonify(error=str(error)),400

    def body():
        value=request.get_json()
        if not isinstance(value,dict):
            raise ValueError('Provide a JSON object')
        return value

    def lease(value):
        ttl=value.get('ttl',LIVE_SUBSCRIPTION_TTL_SECONDS)
        if isinstance(ttl,bool) or not isinstance(ttl,(int,float)) or not 30<=ttl<=LIVE_SUBSCRIPTION_TTL_SECONDS:
            raise ValueError(f'Lease must be between 30 and {LIVE_SUBSCRIPTION_TTL_SECONDS} seconds')
        return LIVE_SUBSCRIPTION_TTL_SECONDS

    @app.get('/')
    def monitor():
        return MONITOR

    @app.get('/status')
    def status():
        return jsonify(manager.status())

    @app.get('/health')
    def health():
        return jsonify(manager.status())

    @app.get('/contracts')
    def contracts():
        return jsonify(contracts=manager.contracts())

    @app.get('/universe')
    def universe():
        product=request.args.get('product','').upper()
        product=ALIASES.get(product,product)
        if product not in {'CL','CO','G','HO','NG','RB','CL-CO'}:
            raise ValueError('Choose a supported product')
        products={'CL','CO','CL-CO'} if product=='CL-CO' else {product}
        months={month for (symbol,legs) in index() if symbol in products for month,_ in legs}
        return jsonify(product=product,months=sorted(months,key=lambda month:(int(month[1:]),MONTHS.index(month[0]))))

    @app.post('/requests')
    def register():
        value=body()
        rows=manager.register(value.get('owner'),value.get('structures'),lease(value))
        return jsonify(requests=rows),201

    @app.post('/requests/heartbeat')
    def heartbeat():
        value=body()
        ids=value.get('request_ids')
        if not isinstance(ids,list) or not 1<=len(ids)<=64 or not all(isinstance(i,str) for i in ids):
            raise ValueError('Provide 1–64 request IDs')
        return jsonify(renewed=manager.store.renew(None,ids,lease(value)))

    @app.get('/quotes/<key>')
    def quote(key):
        result=manager.read(key)
        return (jsonify(result),200) if result else (jsonify(error='Request missing or expired; register it again'),404)

    @app.post('/quotes')
    def quotes():
        value=body()
        ids=value.get('request_ids')
        if not isinstance(ids,list) or not 1<=len(ids)<=64 or not all(isinstance(i,str) for i in ids):
            raise ValueError('Provide 1–64 request IDs')
        details=value.get('include_details',True)
        if not isinstance(details,bool):
            raise ValueError('include_details must be true or false')
        recipes=value.get('include_recipes',False)
        if not isinstance(recipes,bool):
            raise ValueError('include_recipes must be true or false')
        alternatives=value.get('include_alternatives',False)
        if not isinstance(alternatives,bool):
            raise ValueError('include_alternatives must be true or false')
        rows=[manager.read(key) for key in ids]
        if recipes:
            try:
                labels=names()
            except (OSError,ValueError,KeyError,TypeError):
                labels={}
            for row in rows:
                quote=(row or {}).get('quote') or {}
                overall=quote.get('overall') or {}
                books=quote.get('books',{})
                for side in ('bid','ask'):
                    components=[]
                    for part in overall.get(side+'_recipe',[]):
                        weight=part['weight']
                        book_side=('bid' if weight>0 else 'ask') if side=='bid' else ('ask' if weight>0 else 'bid')
                        components.append(dict(tt_id=part['tt_id'],weight=weight,
                            structure=labels.get(part['tt_id'],part['tt_id']),book_side=book_side,
                            price=books.get(part['tt_id'],{}).get(book_side)))
                    if overall:
                        overall[side+'_recipe']=components
        if not details:
            for row in rows:
                if row and row.get('quote'):
                    quote=row['quote']
                    overall=quote.get('overall')
                    row['quote']={key:quote[key] for key in ('status','price_status','price_reason','computed_at','errors') if key in quote}
                    if alternatives:
                        row['quote'].update({key:quote.get(key) for key in
                            ('alternatives','alternatives_truncated','alternatives_scope')})
                    keys=('bid','ask','mid','timestamp','bid_optimal','ask_optimal')
                    if recipes:
                        keys+=('bid_recipe','ask_recipe')
                    row['quote']['overall']=({key:overall.get(key) for key in keys} if overall else None)
        return jsonify(quotes=rows)

    @app.delete('/requests/<key>')
    def release(key):
        return jsonify(released=manager.store.release(request.args.get('owner',''),key))

    return app


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port',type=int,default=urlparse(LIVE_MANAGER_URL).port or 8071)
    args=parser.parse_args()
    try:
        with writer_lock(DEFAULT_PATH,timeout=0):
            manager=PriceManager()
            try:
                manager.start()
                create_app(manager).run(host='127.0.0.1',port=args.port,debug=False,use_reloader=False)
            finally:
                manager.stop()
    except TimeoutError:
        parser.exit(1,'A manager or minute recorder already owns its registry. Stop it before starting another.\n')


if __name__=='__main__':
    main()



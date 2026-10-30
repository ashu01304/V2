"""Shared lab publisher and tiny subscription monitor. Run with -m."""
import argparse
from flask import Flask,jsonify,request
from .price_manager import PriceManager

MONITOR='''<!doctype html><meta charset="utf-8"><title>Lab live manager</title>
<style>body{background:#090909;color:#eee;font:14px Arial;margin:30px}h2{font-size:20px}
table{border-collapse:collapse;width:100%;margin:18px 0}td,th{text-align:left;border-bottom:1px solid #333;padding:8px}
summary{cursor:pointer}#status{color:#aaa}code{font-size:12px}</style>
<h2>Live subscription manager · lab</h2><div id="status">Loading…</div>
<h3>Requested structures</h3><table><thead><tr><th>Dashboard</th><th>Original structure</th><th>IDs</th><th>Lease remaining</th></tr></thead><tbody id="requests"></tbody></table>
<details><summary>Subscribed contract IDs</summary><table><thead><tr><th>Product</th><th>Structure</th><th>TT ID</th><th>Accepted</th><th>Received</th></tr></thead><tbody id="contracts"></tbody></table></details>
<script>
function rows(id,items){const target=document.getElementById(id);target.replaceChildren();
 for(const values of items){const row=document.createElement('tr');for(const value of values){
 const cell=document.createElement('td');cell.textContent=value;row.appendChild(cell);}target.appendChild(row);}}
async function update(){try{const response=await fetch('/status');if(!response.ok)throw Error();const s=await response.json();
 document.getElementById('status').textContent=`${s.status} | ${s.active_requests} requests | ${s.subscribed}/${s.required} IDs accepted | ${s.received} received`+
 (s.manager_error?' | '+s.manager_error:'')+(s.errors.length?' | '+s.errors.join('; '):'');
 rows('requests',s.requests.map(r=>[r.owner,r.expression,r.required_ids,Math.max(0,Math.round(r.expires_at-s.server_time))+'s']));
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
        ttl=value.get('ttl',1800)
        if isinstance(ttl,bool) or not isinstance(ttl,(int,float)) or not 30<=ttl<=1800:
            raise ValueError('Lease must be between 30 and 1800 seconds')
        return ttl

    @app.get('/')
    def monitor():
        return MONITOR

    @app.get('/status')
    def status():
        return jsonify(manager.status())

    @app.post('/requests')
    def register():
        value=body()
        rows=manager.register(value.get('owner'),value.get('structures'),lease(value))
        return jsonify(requests=rows),201

    @app.post('/requests/heartbeat')
    def heartbeat():
        value=body()
        ids=value.get('request_ids')
        if not isinstance(value.get('owner'),str) or not isinstance(ids,list) or not 1<=len(ids)<=64 or not all(isinstance(i,str) for i in ids):
            raise ValueError('Provide owner and 1–64 request IDs')
        return jsonify(renewed=manager.store.renew(value['owner'],ids,lease(value)))

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
        rows=[manager.read(key) for key in ids]
        if not details:
            for row in rows:
                if row and row.get('quote'):
                    quote=row['quote']
                    overall=quote.get('overall')
                    row['quote']={key:quote[key] for key in ('status','price_status','price_reason','computed_at','errors') if key in quote}
                    row['quote']['overall']=({key:overall.get(key) for key in
                        ('bid','ask','mid','timestamp','bid_optimal','ask_optimal')} if overall else None)
        return jsonify(quotes=rows)

    @app.delete('/requests/<key>')
    def release(key):
        return jsonify(released=manager.store.release(request.args.get('owner',''),key))

    return app


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port',type=int,default=8071)
    args=parser.parse_args()
    manager=PriceManager()
    manager.start()
    try:
        create_app(manager).run(host='127.0.0.1',port=args.port,debug=False,use_reloader=False)
    finally:
        manager.stop()

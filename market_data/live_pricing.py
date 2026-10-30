"""Fresh fallback prices and bounded best-bid/ask optimization for whole structures."""
from collections import defaultdict
import math
import time
from itertools import islice, product as combinations
import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import csr_matrix, eye, hstack
from .structure_parser import MONTHS
from .live_resolver import quoted_components


def vector(product,legs):
    result=defaultdict(float)
    for month,weight in legs:
        for symbol,sign in ([('CL',1),('CO',-1)] if product=='CL-CO' else [(product,1)]):
            result[symbol+':'+month]+=sign*weight
    return {key:value for key,value in result.items() if abs(value)>1e-9}


def price_recipe(target,entries,side,recipe):
    """Reprice a recipe from current fresh books, checking every required leg."""
    books={tt:(weights,row) for tt,weights,row in entries}
    quantities=defaultdict(float)
    for part in recipe:
        quantities[part['tt_id']]+=part['weight']
    quantities={tt:q for tt,q in quantities.items() if abs(q)>1e-9}
    actual=defaultdict(float)
    value=0.
    sizes,timestamps=[],[]
    for tt,q in quantities.items():
        if tt not in books or not math.isfinite(q):
            return None
        weights,row=books[tt]
        field=('ask' if q>0 else 'bid') if side=='ask' else ('bid' if q>0 else 'ask')
        price,size=row.get(field),row.get(field+'_size')
        if price is None or not math.isfinite(price) or (size is not None and (not math.isfinite(size) or size<=0)):
            return None
        value+=q*price
        sizes.append(None if size is None else size/abs(q))
        timestamps.append(row['received_at'])
        for key,weight in weights.items():
            actual[key]+=q*weight
    if not quantities or not math.isfinite(value) or any(abs(actual[key]-target.get(key,0))>1e-7 for key in set(actual)|set(target)):
        return None
    return dict(value=value,size=min(sizes) if all(s is not None for s in sizes) else None,
                timestamp=min(timestamps),count=len(quantities),
                recipe=[dict(tt_id=tt,weight=q) for tt,q in quantities.items()])


def construction_parts(request,target):
    components=quoted_components(request)
    named=[(vector(term.product,term.legs()),count) for term,count in components]
    products=defaultdict(list)
    for (product,month),weight in request.settlement_legs().items():
        products[product].append((month,weight))
    spreads=[]
    for product,legs in products.items():
        legs.sort(key=lambda item:(int(item[0][1:]),MONTHS.index(item[0][0])))
        cumulative=0
        for i,(month,weight) in enumerate(legs[:-1]):
            cumulative+=weight
            if abs(cumulative)>1e-9:
                spreads.append((vector(product,[(month,1),(legs[i+1][0],-1)]),cumulative))
    label='Quoted flies' if components and all(term.kind=='MF' for term,_ in components) else 'Named quoted structures'
    return [('Direct quote',[(target,1)]),(label,named),('Quoted spreads',spreads)]


def available_constructions(request,target,entries,extra_recipes=(),limit=64):
    """Paired sides for each discovered exact recipe; no construction is ranked."""
    result,seen=[],set()
    truncated=False
    def add(label,recipe):
        quantities=defaultdict(float)
        for part in recipe:
            quantities[part['tt_id']]+=part['weight']
        key=tuple(sorted((tt,q) for tt,q in quantities.items() if abs(q)>1e-9))
        if not key or key in seen:
            return
        seen.add(key)
        recipe=[dict(tt_id=tt,weight=q) for tt,q in key]
        bid=price_recipe(target,entries,'bid',recipe)
        ask=price_recipe(target,entries,'ask',recipe)
        if not bid and not ask:
            return
        result.append(dict(construction=label,components=recipe,
            bid=bid['value'] if bid else None,ask=ask['value'] if ask else None,
            mid=(bid['value']+ask['value'])/2 if bid and ask else None,
            bid_size=bid['size'] if bid else None,ask_size=ask['size'] if ask else None,
            timestamp=min(q['timestamp'] for q in (bid,ask) if q),
            bid_timestamp=bid['timestamp'] if bid else None,ask_timestamp=ask['timestamp'] if ask else None,
            fresh=True))
    for label,parts in construction_parts(request,target):
        choices=[]
        for wanted,count in parts:
            matches=[]
            for tt,weights,_ in sorted(entries,key=lambda entry:entry[0]):
                if weights.keys()!=wanted.keys():
                    continue
                first=next(iter(weights))
                ratio=wanted[first]/weights[first]
                if all(abs(ratio*weights[key]-wanted[key])<=1e-8 for key in wanted):
                    matches.append(dict(tt_id=tt,weight=count*ratio))
            if not matches:
                break
            choices.append(matches)
        if not parts or len(choices)!=len(parts):
            continue
        for index,recipe in enumerate(islice(combinations(*choices),limit+1)):
            if index==limit:
                truncated=True
                break
            add(label,recipe)
    for recipe in extra_recipes:
        if recipe:
            add('Additional quoted mix',recipe)
    return result,truncated


def available_quote(request,target,entries,side,previous=None):
    def assemble(parts):
        recipe=[]
        for wanted,count in parts:
            choices=[]
            for tt,weights,row in entries:
                if weights.keys()!=wanted.keys():
                    continue
                first=next(iter(weights))
                ratio=wanted[first]/weights[first]
                if any(abs(ratio*weights[key]-wanted[key])>1e-8 for key in wanted):
                    continue
                q=count*ratio
                candidate=price_recipe({key:count*w for key,w in wanted.items()},[(tt,weights,row)],side,[dict(tt_id=tt,weight=q)])
                if candidate:
                    choices.append(candidate)
            if not choices:
                return None
            chosen=(min if side=='ask' else max)(choices,key=lambda quote:quote['value'])
            recipe.extend(chosen['recipe'])
        return price_recipe(target,entries,side,recipe)

    options=[assemble(parts) for _,parts in construction_parts(request,target)]
    if previous:
        options.append(price_recipe(target,entries,side,previous))
    options=[quote for quote in options if quote is not None]
    return (min if side=='ask' else max)(options,key=lambda quote:quote['value']) if options else None


def optimize_side(target, entries, side, seed=None):
    """Buy target at lowest ask; selling target is buying its negative vector."""
    sign = 1 if side == 'ask' else -1
    component_limit=max(3,seed['count'] if seed else 0)
    variables = []
    for item, weights, row in entries:
        for direction, field, size_field in ((1, 'ask', 'ask_size'), (-1, 'bid', 'bid_size')):
            price, size = row.get(field), row.get(size_field)
            if price is not None and (size is None or size > 0):
                variables.append((item, weights, row, direction, direction * price))
    if not variables:
        return seed, False
    months = sorted(set(target).union(*(set(v[1]) for v in variables)))
    n = len(variables)
    a = np.array([[direction * weights.get(m, 0) for _, weights, _, direction, _ in variables] for m in months])
    wanted = sign * np.array([target.get(m, 0) for m in months])
    # Bound intermediate exposure and disallow redundant arbitrage cycles below.
    # Cramer's-rule bound for up to three independent integer leg vectors.
    bound = max(1., 2 * max(1., float(np.abs(a).max()))**2 * sum(abs(w) for w in target.values()))
    if seed:
        bound=max(bound,max(abs(part['weight']) for part in seed['recipe']))
    zeros = csr_matrix(a.shape)
    link = hstack((eye(n), -bound * eye(n)))
    groups = {}
    for i, (item, *_) in enumerate(variables):
        groups.setdefault(item, []).append(i)
    group_rows = csr_matrix(([1.] * n, ([g for g, ids in enumerate(groups.values()) for _ in ids],
                            [i for ids in groups.values() for i in ids])), shape=(len(groups), n))
    constraints = [LinearConstraint(hstack((csr_matrix(a), zeros)), wanted, wanted),
                   LinearConstraint(link, -np.inf, 0),
                   LinearConstraint(hstack((csr_matrix((len(groups), n)), group_rows)), -np.inf, 1),
                   LinearConstraint(csr_matrix(np.r_[np.zeros(n), np.ones(n)][None, :]), -np.inf, component_limit)]
    objective = np.r_[[v[4] for v in variables], np.zeros(n)]
    best, certified = seed, False
    deadline = time.monotonic() + .35
    while time.monotonic() < deadline:
        result = milp(objective, integrality=np.r_[np.zeros(n), np.ones(n)],
                      bounds=Bounds(np.zeros(2*n), np.r_[np.full(n, bound), np.ones(n)]),
                      constraints=constraints, options={'time_limit': max(.01, deadline-time.monotonic()), 'mip_rel_gap': 0.})
        if result.x is None:
            break
        selected = np.flatnonzero(result.x[:n] > 1e-7)
        if not len(selected) or len(selected) > component_limit:
            break
        if np.linalg.matrix_rank(a[:, selected], tol=1e-8) < len(selected):
            # Dependent columns can add unrelated arbitrage exposure; forbid that set.
            cut = np.zeros(2*n)
            cut[n+selected] = 1
            constraints.append(LinearConstraint(csr_matrix(cut[None, :]), -np.inf, len(selected)-1))
            continue
        if not np.allclose(a @ result.x[:n], wanted, atol=1e-7, rtol=0):
            break
        components = [(sign * variables[i][3] * float(result.x[i]), variables[i][2]) for i in selected]
        value = sign * float(objective @ result.x)
        quantities = [row.get(('ask_size' if q > 0 else 'bid_size') if side == 'ask'
                              else ('bid_size' if q > 0 else 'ask_size'))
                      for q, row in components]
        size = min(s/abs(q) for s,(q,_) in zip(quantities,components)) if all(s is not None for s in quantities) else None
        proposal = dict(value=value, size=size, timestamp=min(row['received_at'] for _, row in components), count=len(components),
                        recipe=[dict(tt_id=variables[i][0],weight=sign*variables[i][3]*float(result.x[i])) for i in selected])
        if best is None or (value < best['value'] if side == 'ask' else value > best['value']):
            best = proposal
        certified = result.status == 0 and abs(best['value']-value)<=1e-8
        break
    return best, certified



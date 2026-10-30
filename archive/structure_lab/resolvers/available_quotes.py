"""Fast complete-structure quotes to use before/as fallback to optimization."""
from collections import defaultdict
import math
from ..expression import MONTHS
from .live import quoted_components


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

    named=[(vector(term.product,term.legs()),count) for term,count in quoted_components(request)]
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
    options=[assemble([(target,1)]),assemble(named),assemble(spreads)]
    if previous:
        options.append(price_recipe(target,entries,side,previous))
    options=[quote for quote in options if quote is not None]
    return (min if side=='ask' else max)(options,key=lambda quote:quote['value']) if options else None

"""Explain three symbolic recipes without prices, network access or databases."""
from .expression import parse,MONTHS
from .resolvers.minute import recipe_for
from .resolvers.live import quoted_components
from .catalogue import index,named_ids,spread_ids


def formula(rows):
    return '\n'.join(('−' if r['weight']<0 else '+')+f" {abs(r['weight']):g} × {r['instrument']}" for r in rows)


def interpret(expression):
    request=parse(expression)
    legs=sorted(request.settlement_legs().items(),key=lambda item:(item[0][0],int(item[0][1][1:]),MONTHS.index(item[0][1][0])))
    seac=[dict(instrument=p+' '+c,weight=w) for (p,c),w in legs]
    result=dict(expression=request.text,named=request.named_rows(),seac=dict(rows=seac,formula=formula(seac)))
    try:
        minutes=[dict(instrument=i,weight=w) for i,w in recipe_for(request).items()]
        result['minute']=dict(rows=minutes,formula=formula(minutes))
    except ValueError as error:
        result['minute']=dict(rows=[],formula='',error=str(error))
    try:
        catalogue_error=None
        try:
            lookup=index()
        except (OSError,ValueError,KeyError,TypeError) as error:
            lookup={}
            catalogue_error='Cannot read saved catalogue: '+str(error)
        quotes=[dict(instrument=term.label,weight=count,tt_ids=named_ids(term,lookup)) for term,count in quoted_components(request)]
        totals={p:sum(w for (symbol,_),w in request.exposures().items() if symbol==p) for p,_ in request.exposures()}
        if any(abs(w)>1e-9 for w in totals.values()):
            raise ValueError('Live structures must have zero net exposure per product')
        spreads=[dict(instrument=i,weight=w,tt_ids=spread_ids(i,lookup)) for i,w in recipe_for(request,stored=False).items()]
        result['live']=dict(rows=quotes,formula=formula(quotes),alternative=spreads,alternative_formula=formula(spreads))
        if catalogue_error:
            result['live']['catalogue_error']=catalogue_error
    except ValueError as error:
        result['live']=dict(rows=[],formula='',error=str(error))
    return result

"""Parse named structures while retaining their identity and expanded exposure."""
from collections import defaultdict
from dataclasses import dataclass
import re
import math

MONTHS = 'FGHJKMNQUVXZ'
ALIASES = {'BRN':'CO', 'LCO':'CO', 'GO':'G', 'LGO':'G'}


def canonical_structure(text):
    """Stable named expression independent of spelling, spacing and term order."""
    counts = defaultdict(float)
    for term in parse(text).terms:
        counts[term.label] += term.count
    parts = []
    for label, count in sorted(counts.items()):
        if abs(count) <= 1e-10:
            continue
        prefix = ('- ' if count < 0 else '+ ') if parts else ('- ' if count < 0 else '')
        parts.append(prefix + (f'{abs(count):.17g} * ' if abs(count) != 1 else '') + label)
    return ' '.join(parts)
WEIGHTS = {'OUT':(1,), 'MS':(1,-1), 'MF':(1,-2,1), 'DF':(1,-3,3,-1), 'DDF':(1,-4,6,-4,1)}
TERM = re.compile(r'\s*([+-]?)\s*(?:(\d+(?:\.\d+)?)\s*\*\s*)?'
                  r'(CL-CO|[A-Z]+)\s+([FGHJKMNQUVXZ])(\d{4}|\d{2})\s*(?:(\d+)\s*(MDDF|MDF|DDF|MS|MF|DF)|(OUT))', re.I)


def month_code(ordinal):
    return MONTHS[ordinal % 12] + f'{ordinal // 12 % 100:02d}'


@dataclass(frozen=True)
class NamedStructure:
    count: float
    product: str
    anchor: int
    kind: str
    gap: int

    @property
    def label(self):
        suffix={'DF':'MDF','DDF':'MDDF'}.get(self.kind,self.kind)
        return f'{self.product} {month_code(self.anchor)} ' + ('OUT' if self.kind == 'OUT' else f'{self.gap}{suffix}')

    def legs(self):
        return [(month_code(self.anchor+i*self.gap), self.count*w) for i,w in enumerate(WEIGHTS[self.kind])]


@dataclass(frozen=True)
class StructureRequest:
    text: str
    terms: tuple

    def exposures(self):
        weights = defaultdict(float)
        for term in self.terms:
            for contract, weight in term.legs():
                weights[(term.product,contract)] += weight
        return {key:value for key,value in weights.items() if abs(value)>1e-10}

    def settlement_legs(self):
        weights = defaultdict(float)
        for (product,contract), weight in self.exposures().items():
            for symbol,sign in ([('CL',1),('CO',-1)] if product=='CL-CO' else [(product,1)]):
                weights[(symbol,contract)] += sign*weight
        return {key:value for key,value in weights.items() if abs(value)>1e-10}

    def expression(self, product):
        legs = sorted(((c,w) for (p,c),w in self.exposures().items() if p==product),
                      key=lambda x:(int(x[0][1:]),MONTHS.index(x[0][0])))
        return ''.join(('+' if i and w>0 else '-' if w<0 else '')+
                       (f'{abs(w):g}*' if abs(w)!=1 else '')+c for i,(c,w) in enumerate(legs))

    def named_rows(self):
        return [dict(structure=t.label,count=t.count,product=t.product,kind=t.kind,gap=t.gap) for t in self.terms]

    def leg_rows(self):
        return [dict(product=p,month=c,weight=w) for (p,c),w in self.exposures().items()]


def parse(text):
    if not isinstance(text,str) or not 1<=len(text)<=2048:
        raise ValueError('Provide a complete structure string of at most 2048 characters')
    text=(text or '').strip().replace('−','-').replace('×','*')
    position, terms = 0, []
    while position<len(text):
        match=TERM.match(text,position)
        if not match or (terms and not match[1]):
            raise ValueError(f'Invalid expression near {text[position:]!r}. Example: CL X27 1MF - CL Z27 1MF')
        sign,size,product,month,year,gap,kind,outright=match.groups()
        kind=(kind or outright).upper()
        kind={'MDF':'DF','MDDF':'DDF'}.get(kind,kind)
        product=ALIASES.get(product.upper(),product.upper())
        if product not in {'CL','CO','G','HO','NG','RB','CL-CO'}:
            raise ValueError('Unsupported product: '+product)
        year=int(year)+(2000 if len(year)==2 else 0)
        gap=int(gap or 1)
        if not 2000<=year<=2099 or not 1<=gap<=24:
            raise ValueError('Use years 2000–2099 and spacing 1–24 months')
        if year*12+MONTHS.index(month.upper())+(len(WEIGHTS[kind])-1)*gap >= 2100*12:
            raise ValueError('Expanded legs must be before year 2100')
        count=float(size or 1)*(-1 if sign=='-' else 1)
        if not math.isfinite(count) or count==0 or abs(count)>1_000_000:
            raise ValueError('Use a finite, nonzero structure count of at most 1,000,000')
        terms.append(NamedStructure(count,product,year*12+MONTHS.index(month.upper()),kind,gap))
        position=match.end()
        if len(terms)>16:
            raise ValueError('Use at most 16 named components per request')
    request=StructureRequest(text,tuple(terms))
    if not terms or not request.exposures() or not request.settlement_legs():
        raise ValueError('The request has no nonzero exposure')
    return request


parse_structure=parse


def from_legacy(product,expression):
    """Central compatibility conversion; callers still send a complete expression."""
    if not isinstance(product,str) or not isinstance(expression,str):
        raise ValueError('Provide a product and complete expression')
    product=ALIASES.get(product.upper(),product.upper())
    text=expression.replace(' ','').upper()
    pattern=re.compile(r'([+-]?)(?:(\d+(?:\.\d+)?)\*)?([FGHJKMNQUVXZ]\d{2})')
    parts=[]
    position=0
    for match in pattern.finditer(text):
        if match.start()!=position or (parts and not match[1]):
            raise ValueError('Invalid month-weight expression')
        sign,size,month=match.groups()
        count=float(size or 1)*(-1 if sign=='-' else 1)
        parts.append((count,month))
        position=match.end()
    if not parts or position!=len(text):
        raise ValueError('Use a complete month-weight expression or a named structure')
    weights=defaultdict(float)
    for count,month in parts:
        weights[month]+=count
    ordered=sorted(((month,count) for month,count in weights.items() if abs(count)>1e-10),
                   key=lambda pair:(int(pair[0][1:]),MONTHS.index(pair[0][0])))
    if ordered:
        dates=[(2000+int(month[1:]))*12+MONTHS.index(month[0]) for month,_ in ordered]
        gap=dates[1]-dates[0] if len(dates)>1 else 1
        scale=ordered[0][1]
        for kind,pattern in WEIGHTS.items():
            if len(pattern)==len(ordered) and 1<=gap<=24 and all(dates[i]==dates[0]+i*gap for i in range(len(dates))) and all(
                    math.isclose(count,scale*weight,rel_tol=0,abs_tol=1e-9) for (_,count),weight in zip(ordered,pattern)):
                suffix='OUT' if kind=='OUT' else str(gap)+{'DF':'MDF','DDF':'MDDF'}.get(kind,kind)
                count='' if abs(scale)==1 else f'{abs(scale):g} * '
                return parse_structure(('- ' if scale<0 else '')+count+f'{product} {ordered[0][0]} {suffix}').text
    named=' '.join(('- ' if count<0 else '+ ' if i else '')+
                   f'{abs(count):g} * {product} {month} OUT' for i,(count,month) in enumerate(parts))
    return parse_structure(named).text

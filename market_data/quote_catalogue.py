"""Read-only TT-ID lookup using full names, not potentially incorrect saved codes."""
from collections import defaultdict
from functools import lru_cache
from pathlib import Path
import json
import re
from .structure_parser import MONTHS,month_code

CATALOGUE=Path(__file__).resolve().parents[1]/'data/quoted_live_probe_full_20261029_131318.json'
NAMES='Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec'.split()


def signature(product,legs):
    weights=defaultdict(float)
    for contract,weight in legs:
        weights[contract]+=weight
    return product,tuple(sorted((c,w) for c,w in weights.items() if abs(w)>1e-10))


@lru_cache(maxsize=2)
def _index(path,mtime,size):
    groups=json.loads(Path(path).read_text(encoding='utf-8-sig'))['contracts']
    index=defaultdict(list)
    for product,structures in groups.items():
        if isinstance(structures,list):
            structures={'boxes':structures}
        for kind,records in structures.items():
            if kind=='outrights':
                continue
            for record in records:
                name=record['name']
                dates=[(2000+int(y))*12+NAMES.index(m) for m,y in re.findall(r'\b('+'|'.join(NAMES)+r')(\d{2})\b',name)]
                spacing=re.search(r'(\d+)mo Butterfly',name)
                if spacing and len(dates)==1:
                    dates=[dates[0]+i*int(spacing[1]) for i in range(3)]
                if kind=='boxes':
                    if len(dates)!=4 or dates[0]!=dates[1] or dates[2]!=dates[3]:
                        continue
                    # This catalogue's boxes are +CL-BZ at the first date, reversed at the second.
                    if not name.startswith('+1xCL ') or ':-1xBZ ' not in name or ':-1xCL ' not in name or ':+1xBZ ' not in name:
                        continue
                    dates=[dates[0],dates[2]]
                weights=(1,-2,1) if kind=='flies' else (1,-1)
                if len(dates)!=len(weights):
                    continue
                key=signature('CL-CO' if kind=='boxes' else product,zip(map(month_code,dates),weights))
                instrument=str(record['id'])
                if not instrument.isdigit():
                    continue
                tt='TT-'+instrument
                if tt not in index[key]:
                    index[key].append(tt)
    return dict(index)


def index(path=CATALOGUE):
    path=Path(path)
    stat=path.stat()
    return _index(str(path.resolve()),stat.st_mtime_ns,stat.st_size)


@lru_cache(maxsize=2)
def _names(path,mtime,size):
    groups=json.loads(Path(path).read_text(encoding='utf-8-sig'))['contracts']
    return {'TT-'+str(record['id']).removeprefix('TT-'):record['name']
            for structures in groups.values()
            for records in ([structures] if isinstance(structures,list) else structures.values())
            for record in records}


def names(path=CATALOGUE):
    """Saved display names keyed by TT ID, cached until the catalogue changes."""
    path=Path(path)
    stat=path.stat()
    return _names(str(path.resolve()),stat.st_mtime_ns,stat.st_size)


def named_ids(term,lookup):
    return lookup.get(signature(term.product,term.legs()),[])


def spread_ids(instrument,lookup):
    match=re.fullmatch(r'(CL|CO|HO|NG|RB|G)([FGHJKMNQUVXZ]\d{2})-([FGHJKMNQUVXZ]\d{2})',instrument)
    return lookup.get(signature(match[1],[(match[2],1),(match[3],-1)]),[]) if match else []


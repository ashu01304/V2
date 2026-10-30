"""Resolve prices and executable bid/ask estimates using quoted structures only."""
from datetime import datetime, timezone
import time

import numpy as np
from scipy.optimize import linprog
from .best_quotes import optimize_side


class StructureResolver:
    def __init__(self, store):
        self.store = store
        self.plans = {}
        self.side_cache = {}

    def weights(self, expression):
        key, scale = self.store.recipe(expression)
        return {contract: weight * scale for contract, weight in key}

    def available(self, product, require_value=True):
        now = datetime.now(timezone.utc).timestamp()
        result = []
        for item, meta in self.store.metadata.items():
            if meta['product'] != product or meta['structure'] == 'outrights':
                continue
            row = self.store.rows.get((product, meta['contract']))
            user = self.store.item_users.get(item)
            connected = self.store.connections.get(user, self.store.status).startswith('CONNECTED:')
            priced = row and (row['value'] is not None if require_value else any(row.get(f) is not None for f in ('bid','ask')))
            if priced and connected and 0 <= now - row['_received'] <= self.store.max_age:
                result.append((item, meta, row))
        return result

    def plan(self, target, candidates):
        """Minimize gross component quantity; validate exact leg replication."""
        if not candidates:
            return None
        signature = (tuple(sorted(target.items())), tuple(c[0] for c in candidates))
        if signature in self.plans:
            return self.plans[signature]
        recipes = [self.weights(meta['contract']) for _, meta, _ in candidates]
        months = sorted(set(target).union(*(set(r) for r in recipes)))
        matrix = np.array([[r.get(month, 0) for r in recipes] for month in months], dtype=float)
        wanted = np.array([target.get(month, 0) for month in months], dtype=float)
        # Positive/negative variables allow buying and selling each quoted structure.
        result = linprog(np.ones(2 * len(recipes)), A_eq=np.hstack((matrix, -matrix)),
                         b_eq=wanted, bounds=(0, None), method='highs')
        plan = None
        if result.success:
            quantities = result.x[:len(recipes)] - result.x[len(recipes):]
            if np.allclose(matrix @ quantities, wanted, atol=1e-8, rtol=0):
                plan = [(i, float(q)) for i, q in enumerate(quantities) if abs(q) > 1e-9]
        if len(self.plans) > 2048:
            self.plans.clear()
        self.plans[signature] = plan
        return plan

    def combine(self, product, expression, components, source):
        def total(field, opposite=None):
            values = [(q, row.get(opposite if q < 0 and opposite else field)) for q, row in components]
            return sum(q * value for q, value in values) if all(v is not None for _, v in values) else None

        def size(field, opposite):
            values = [row.get(opposite if q < 0 else field) / abs(q)
                      for q, row in components if row.get(opposite if q < 0 else field) is not None]
            return min(values) if len(values) == len(components) else None

        last, bid, ask = total('last'), total('bid', 'ask'), total('ask', 'bid')
        return dict(product=product, expression=expression,
                    value=total('value'), last=last, bid=bid, ask=ask,
                    bid_size=size('bid_size', 'ask_size'), ask_size=size('ask_size', 'bid_size'),
                    timestamp=min(row['received_at'] for _, row in components),
                    timestamp_basis='oldest_component_received_at_utc', source=source,
                    component_count=len(components))

    def _reference(self, product, expression):
        product = product.upper()
        target = self.weights(expression)
        if abs(sum(target.values())) > 1e-8:
            raise LookupError('Outright exposure is unavailable; only quoted spreads, flies and boxes are used')
        with self.store.lock:
            if not self.store.status.startswith('CONNECTED:'):
                raise LookupError('Live feed is disconnected')
            candidates = self.available(product)
            key, scale = self.store.recipe(expression)
            for item, meta, row in candidates:
                candidate_key, candidate_scale = self.store.recipe(meta['contract'])
                if key == candidate_key:
                    quote = self.combine(product, expression, [(scale / candidate_scale, row)], 'direct')
                    quote['instrument'] = item
                    return quote
            # A defly is first replicated by quoted flies, then by quoted spreads.
            for kinds, source in (({'flies'}, 'quoted_flies'), ({'spreads', 'boxes'}, 'quoted_spreads'),
                                  ({'flies', 'spreads', 'boxes'}, 'quoted_mixed')):
                selected = [c for c in candidates if c[1]['structure'] in kinds]
                plan = self.plan(target, selected)
                if plan:
                    return self.combine(product, expression, [(q, selected[i][2]) for i, q in plan], source)
            if product == 'CL-CO':
                # Cross-product structures use quoted CL/CO structures, never outright legs.
                cl, co = self._reference('CL', expression), self._reference('CO', expression)
                return dict(product=product, expression=expression, value=cl['value'] - co['value'],
                            last=None if cl['last'] is None or co['last'] is None else cl['last']-co['last'],
                            bid=None if cl['bid'] is None or co['ask'] is None else cl['bid']-co['ask'],
                            ask=None if cl['ask'] is None or co['bid'] is None else cl['ask']-co['bid'],
                            bid_size=None, ask_size=None, timestamp=min(cl['timestamp'], co['timestamp']),
                            timestamp_basis='oldest_component_received_at_utc', source='quoted_cross_product',
                            component_count=cl['component_count'] + co['component_count'])
        raise LookupError('No fresh quoted structures can replicate ' + product + ' ' + expression)

    def resolve(self, product, expression, best_sides=True):
        # Reference and side seeds must come from the same consistent book view.
        with self.store.lock:
            return self._resolve(product, expression, best_sides)

    def _resolve(self, product, expression, best_sides):
        product = product.upper()
        error = None
        try:
            quote = self._reference(product, expression)
        except LookupError as unavailable:
            if not best_sides or not self.store.status.startswith('CONNECTED:') or abs(sum(self.weights(expression).values())) > 1e-8:
                raise
            error = unavailable
            quote = dict(product=product, expression=expression, value=None,last=None,bid=None,ask=None,
                         bid_size=None,ask_size=None,timestamp=None,source='quoted_sides',component_count=0,
                         timestamp_basis='oldest_component_received_at_utc')
        if not best_sides:
            return quote
        with self.store.lock:
            target = self.weights(expression)
            entries = []
            if product == 'CL-CO':
                target = {f'{p}:{c}': s*w for p,s in (('CL',1),('CO',-1)) for c,w in target.items()}
                for p in ('CL-CO', 'CL', 'CO'):
                    for item, meta, row in self.available(p, require_value=False):
                        weights = self.weights(meta['contract'])
                        weights = ({f'{leg}:{c}': s*w for leg,s in (('CL',1),('CO',-1)) for c,w in weights.items()}
                                   if p == 'CL-CO' else {f'{p}:{c}': w for c,w in weights.items()})
                        entries.append((item, weights, row))
            else:
                entries = [(item,self.weights(meta['contract']),row) for item,meta,row in self.available(product, require_value=False)]
            signature = (tuple(sorted(target.items())), tuple((item,row.get('bid'),row.get('ask'),row.get('bid_size'),row.get('ask_size'))
                                                           for item,_,row in entries))
            cached = self.side_cache.get(signature)
            now = datetime.now(timezone.utc).timestamp()
            cache_fresh = cached and all(not cached[1][side+'_timestamp'] or
                now - datetime.fromisoformat(cached[1][side+'_timestamp']).timestamp() <= self.store.max_age for side in ('bid','ask'))
            if cache_fresh and time.monotonic() - cached[0] < 2:
                sides = cached[1]
            else:
                sides = {}
                for side in ('bid','ask'):
                    seed = (dict(value=quote[side],size=quote[side+'_size'],timestamp=quote['timestamp'],count=quote['component_count'])
                            if quote[side] is not None and quote['component_count'] <= 3
                            and (quote[side+'_size'] is None or quote[side+'_size'] > 0) else None)
                    best, certified = optimize_side(target, entries, side, seed)
                    sides.update({side: None if best is None else best['value'],
                                  side+'_size': None if best is None else best['size'],
                                  side+'_components': 0 if best is None else best['count'],
                                  side+'_optimal': certified,
                                  side+'_timestamp': None if best is None else best['timestamp']})
                if len(self.side_cache) > 512:
                    self.side_cache.clear()
                self.side_cache[signature] = (time.monotonic(), sides)
            quote.update(sides)
            if error:
                timestamps = [sides[f'{side}_timestamp'] for side in ('bid','ask') if sides[f'{side}_timestamp']]
                if not timestamps:
                    raise error
                quote['timestamp'] = min(timestamps)
            return quote

    def curve(self, product, expression):
        months = 'FGHJKMNQUVXZ'
        weights = self.weights(expression)
        ordinal = lambda c: (2000 + int(c[1:])) * 12 + months.index(c[0])
        first = min(map(ordinal, weights))
        available = {ordinal(c) for m in self.store.metadata.values()
                     if m['product'] in ({'CL-CO', 'CL', 'CO'} if product == 'CL-CO' else {product})
                     for c in self.weights(m['contract'])}
        points = []
        for anchor in sorted(available):
            legs = [(w, anchor + ordinal(c) - first) for c, w in sorted(weights.items(), key=lambda p: ordinal(p[0]))]
            if not all(d in available for _, d in legs):
                continue
            codes = [months[d % 12] + f'{d // 12 % 100:02d}' for _, d in legs]
            shifted = ''.join(('+' if i and w > 0 else '-' if w < 0 else '') +
                              (f'{abs(w):g}*' if abs(w) != 1 else '') + code
                              for i, ((w, _), code) in enumerate(zip(legs, codes)))
            try:
                quote = self.resolve(product, shifted, best_sides=False)
            except LookupError:
                continue
            points.append(dict(contract=shifted, label=codes[0], price=quote['value'], order=anchor,
                               bid=quote['bid'], ask=quote['ask'], source=quote['source']))
        return {'live': points}

"""Lab-local optimizer snapshot: no production imports or subscriptions."""
import time
import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import csr_matrix, eye, hstack


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

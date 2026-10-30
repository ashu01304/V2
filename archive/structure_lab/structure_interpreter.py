"""Translate a whole named request to its live subscription requirements."""
from .live_feed import selection
from .interpretation import interpret


def resolve(expression):
    if not isinstance(expression,str) or not 1<=len(expression)<=2048:
        raise ValueError('Provide a structure string of at most 2048 characters')
    plan=interpret(expression)
    if plan['live'].get('error'):
        raise ValueError(plan['live']['error'])
    request,target,contracts=selection(expression)
    return dict(expression=expression,target=target,contracts=contracts,components=plan['live'])

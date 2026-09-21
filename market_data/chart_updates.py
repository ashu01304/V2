"""Preserve interactive chart state when timer callbacks refresh live data."""

from functools import wraps
import hashlib
import json

from dash import Patch, ctx, no_update
from plotly.utils import PlotlyJSONEncoder


def _plain(value):
    return json.loads(json.dumps(value, cls=PlotlyJSONEncoder))


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _update_figure(figure, previous, selection, automatic):
    if figure is no_update or isinstance(figure, Patch):
        return figure
    current = _plain(figure.to_plotly_json() if hasattr(figure, "to_plotly_json") else figure)
    if not isinstance(current, dict):
        return figure
    layout = current.setdefault("layout", {})
    meta = layout.get("meta")
    meta = dict(meta) if isinstance(meta, dict) else {"original_meta": meta}
    meta["live_selection"] = selection
    layout["meta"] = meta
    layout.setdefault("uirevision", selection)
    occurrences = {}
    for trace in current.get("data", []):
        identity = (trace.get("type"), trace.get("name"), trace.get("xaxis"), trace.get("yaxis"))
        occurrence = occurrences.get(identity, 0)
        occurrences[identity] = occurrence + 1
        trace["uid"] = "trace" + _digest([identity, occurrence])

    previous = previous or {}
    old_layout = previous.get("layout", {})
    old_meta = old_layout.get("meta") or {}
    if (not automatic or not isinstance(old_meta, dict)
            or old_meta.get("live_selection") != selection
            or old_layout.get("uirevision") != layout.get("uirevision")):
        return current

    patch = Patch()
    changed = False

    def diff(old, new, path):
        nonlocal changed
        if isinstance(old, dict) and isinstance(new, dict):
            for key in old.keys() - new.keys():
                target = patch
                for part in path:
                    target = target[part]
                del target[key]
                changed = True
            for key, value in new.items():
                diff(old.get(key), value, path + [key])
        elif old != new:
            target = patch
            for part in path[:-1]:
                target = target[part]
            target[path[-1]] = new
            changed = True

    old_data, new_data = previous.get("data", []), current.get("data", [])
    if [t.get("uid") for t in old_data] == [t.get("uid") for t in new_data]:
        for index, trace in enumerate(new_data):
            # Visibility belongs to the user, not the live-data callback.
            old = {k: v for k, v in old_data[index].items() if k != "visible"}
            new = {k: v for k, v in trace.items() if k != "visible"}
            diff(old, new, ["data", index])
    else:
        visibility = {t.get("uid"): t.get("visible", True) for t in old_data}
        for trace in new_data:
            trace["visible"] = visibility.get(trace["uid"], True)
        diff(old_data, new_data, ["data"])

    # Live annotations and levels may change; never resend axes or viewport.
    for key in ("title", "annotations", "shapes"):
        diff(old_layout.get(key), layout.get(key), ["layout", key])
    return patch if changed else no_update


def preserve_live_charts(figure_outputs):
    """Wrap a callback whose trailing States are its current chart figures."""
    def decorate(callback):
        @wraps(callback)
        def wrapped(*args, **kwargs):
            count = len(figure_outputs)
            previous = args[-count:]
            result = callback(*args[:-count], **kwargs)
            multiple = isinstance(result, (tuple, list))
            values = list(result) if multiple else [result]
            selected = {key: value for key, value in ctx.inputs.items()
                        if not key.endswith(".n_intervals")}
            selected.update({key: value for key, value in ctx.states.items()
                             if not key.endswith(".figure")})
            selection = _digest(_plain(selected))
            triggers = set(ctx.triggered_prop_ids)
            automatic = bool(triggers) and all(
                key.endswith(".n_intervals") for key in triggers)
            for index, old in zip(figure_outputs, previous):
                values[index] = _update_figure(values[index], old, selection, automatic)
            return tuple(values) if multiple else values[0]
        return wrapped
    return decorate

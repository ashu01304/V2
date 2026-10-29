def csv(values, *, maximum=None):
    items = values.split(",") if isinstance(values, str) else list(values)
    items = [str(item).strip() for item in items]
    if not items or any(not item for item in items):
        raise ValueError("Provide at least one nonempty instrument/product")
    if maximum and len(items) > maximum:
        raise ValueError(f"At most {maximum} instruments per request; batch explicitly")
    return ",".join(items)


def options(**kwargs):
    if "count" in kwargs and kwargs["count"] is not None:
        count = kwargs["count"]
        if isinstance(count, bool) or int(count) != float(count) or int(count) < 1:
            raise ValueError("count must be a positive integer")
    if kwargs.get("start") is not None and kwargs.get("end") is not None:
        if float(kwargs["start"]) > float(kwargs["end"]):
            raise ValueError("start must not exceed end")
    return {key: value for key, value in kwargs.items() if value is not None}

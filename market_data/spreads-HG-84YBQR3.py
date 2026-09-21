from datetime import datetime
from pathlib import Path

import ijson
import requests
import urllib3

from .config import SPREAD_URL


def download(path, url=SPREAD_URL):
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    with requests.get(url, stream=True, timeout=300, verify=False) as response:
        response.raise_for_status()
        with Path(path).open("wb") as file:
            for chunk in response.iter_content(1024 * 1024):
                if chunk:
                    file.write(chunk)


def _timestamp(value):
    if "." in value:
        head, tail = value.split(".", 1)
        value = f"{head}.{tail.rstrip('Z')[:6].ljust(6, '0')}+00:00"
    else:
        value = value.rstrip("Z") + "+00:00"
    return datetime.fromisoformat(value)


def rows(path):
    """Yield the header once, followed by timestamp/value rows."""
    with Path(path).open("rb") as file:
        headers = []
        row = None
        reading_headers = False
        reading_rows = False
        for prefix, event, value in ijson.parse(file):
            if prefix == "item" and event == "start_array" and not headers:
                reading_headers = True
            elif reading_headers and prefix == "item.item" and event == "string":
                headers.append(value)
            elif reading_headers and prefix == "item" and event == "end_array":
                reading_headers = False
                yield headers, None
            elif prefix == "item" and event == "start_array" and headers:
                reading_rows = True
            elif reading_rows and prefix == "item.item" and event == "start_array":
                row = []
            elif row is not None and prefix == "item.item.item" and event in (
                "string", "number", "null"
            ):
                row.append(value)
            elif row is not None and prefix == "item.item" and event == "end_array":
                yield _timestamp(row[0]), row[1:]
                row = None

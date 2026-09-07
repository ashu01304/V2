import json
from pathlib import Path

import requests
import urllib3

from .config import SEAC_URL


def download(path, url=SEAC_URL):
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    with requests.get(url, stream=True, timeout=300, verify=False) as response:
        response.raise_for_status()
        with Path(path).open("wb") as file:
            for chunk in response.iter_content(1024 * 1024):
                if chunk:
                    file.write(chunk)


def rows(path):
    text = Path(path).read_text(encoding="utf-8")
    start = text.find("{")
    end = text.rfind("}") + 1
    if start < 0 or end == 0:
        raise ValueError("SEAC source does not contain a JSON object")
    payload = json.loads(text[start:end])
    for symbol, dates in payload.items():
        for trading_date, contracts in dates.items():
            for contract_code, price in contracts.items():
                yield symbol, contract_code, trading_date, price

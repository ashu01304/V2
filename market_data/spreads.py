from datetime import datetime
import json
import logging
import time
from pathlib import Path

import requests
import urllib3

from .config import SPREAD_URL


logger = logging.getLogger(__name__)


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


class _JSONStream:
    """Decode individual arrays without loading the whole history into memory."""

    CHUNK_SIZE = 64 * 1024
    MAX_VALUE_SIZE = 4 * 1024 * 1024

    def __init__(self, file):
        self.file = file
        self.decoder = json.JSONDecoder()
        self.buffer = ""
        self.position = 0
        self.offset = 0
        self.eof = False

    def _read_more(self):
        self.offset += self.position
        self.buffer = self.buffer[self.position:]
        self.position = 0
        chunk = self.file.read(self.CHUNK_SIZE)
        self.buffer += chunk
        if not chunk:
            self.eof = True
        return bool(chunk)

    def peek(self):
        while True:
            while self.position < len(self.buffer):
                character = self.buffer[self.position]
                if character not in " \t\r\n":
                    return character
                self.position += 1
            if self.eof or not self._read_more():
                return ""

    def expect(self, character):
        actual = self.peek()
        if actual != character:
            raise ValueError(
                f"Invalid spread JSON at character {self.offset + self.position}: "
                f"expected {character!r}, got {actual or 'end of file'!r}"
            )
        self.position += 1

    def value(self):
        if self.peek() != "[":
            raise ValueError("Spread headers and timestamp rows must be JSON arrays")
        while True:
            try:
                value, end = self.decoder.raw_decode(self.buffer, self.position)
            except json.JSONDecodeError as error:
                if len(self.buffer) - self.position >= self.MAX_VALUE_SIZE:
                    raise ValueError("Invalid or oversized spread JSON row (limit: 4 MB)") from error
                if self.eof or not self._read_more():
                    raise ValueError(
                        f"Invalid or incomplete spread JSON near character "
                        f"{self.offset + self.position}: {error.msg}"
                    ) from error
            else:
                self.position = end
                return value


def rows(path):
    """Read [headers, [timestamp rows]] one row at a time using standard JSON."""
    logger.info("Reading spread history with the standard-library streaming JSON reader")
    scanned = 0
    last_progress = time.monotonic()
    with Path(path).open("r", encoding="utf-8-sig") as file:
        stream = _JSONStream(file)
        stream.expect("[")
        headers = stream.value()
        if not headers or not all(isinstance(name, str) for name in headers):
            raise ValueError("Spread history must contain a non-empty array of instrument names")
        yield headers, None
        stream.expect(",")
        stream.expect("[")
        if stream.peek() != "]":
            while True:
                row = stream.value()
                if not row or not isinstance(row[0], str):
                    raise ValueError("Each spread history row must begin with a timestamp string")
                timestamp = _timestamp(row[0])
                scanned += 1
                if scanned % 1_000 == 0 and time.monotonic() - last_progress >= 10:
                    logger.info("Spread history scanned: %s timestamp rows | reached: %s",
                                f"{scanned:,}", timestamp)
                    last_progress = time.monotonic()
                yield timestamp, row[1:]
                del row
                if stream.peek() == "]":
                    break
                stream.expect(",")
        stream.expect("]")
        stream.expect("]")
        if stream.peek():
            raise ValueError("Unexpected trailing content in spread history JSON")
        logger.info("Spread history scan complete: %s timestamp rows", f"{scanned:,}")

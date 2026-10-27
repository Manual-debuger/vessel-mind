"""Telemetry: route decisions and turn summaries, one JSON object per line.

A record is {"time": <ISO-8601 UTC>, "kind": <kind>, ...its data}. The file is
appended to, never rewritten, so `jq` or a spreadsheet can read a server's whole
history. It holds no message text and no key.
"""

import datetime
import json
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Protocol

from pydantic import JsonValue


class Telemetry(Protocol):
    def record(self, kind: str, data: Mapping[str, JsonValue]) -> None: ...


def _utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


class JsonlTelemetry:
    """Appends each record to a JSONL file, creating its directory when needed."""

    def __init__(self, path: Path, *, clock: Callable[[], datetime.datetime] = _utc_now) -> None:
        self.path = path
        self._clock = clock
        self._lock = threading.Lock()

    def record(self, kind: str, data: Mapping[str, JsonValue]) -> None:
        time = self._clock().astimezone(datetime.UTC).isoformat(timespec="microseconds")
        line = json.dumps({"time": time, "kind": kind, **data}, ensure_ascii=False)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8", newline="\n") as file:
                file.write(line + "\n")


class NoTelemetry:
    """Records nothing."""

    def record(self, kind: str, data: Mapping[str, JsonValue]) -> None:
        pass

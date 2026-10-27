"""The JSONL telemetry sink."""

import datetime
import json

from vessel_mind.core.telemetry import JsonlTelemetry

NOW = datetime.datetime(2026, 10, 27, 9, 30, tzinfo=datetime.UTC)


def test_each_record_is_one_json_line_with_its_time_and_kind(tmp_path):
    path = tmp_path / "telemetry" / "events.jsonl"
    telemetry = JsonlTelemetry(path, clock=lambda: NOW)

    telemetry.record("route", {"route": "fast", "usage": {"input_tokens": 3}})
    telemetry.record("turn", {"status": "done"})

    lines = path.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line) for line in lines] == [
        {
            "time": "2026-10-27T09:30:00.000000+00:00",
            "kind": "route",
            "route": "fast",
            "usage": {"input_tokens": 3},
        },
        {"time": "2026-10-27T09:30:00.000000+00:00", "kind": "turn", "status": "done"},
    ]


def test_records_are_appended_to_an_existing_file(tmp_path):
    path = tmp_path / "events.jsonl"
    JsonlTelemetry(path, clock=lambda: NOW).record("turn", {"n": 1})

    JsonlTelemetry(path, clock=lambda: NOW).record("turn", {"n": 2})

    assert [json.loads(line)["n"] for line in path.read_text(encoding="utf-8").splitlines()] == [
        1,
        2,
    ]

"""Sessions over the protocol: history across turns, across restarts, and the turn limit."""

import asyncio
import datetime
import json

import pytest
from fastapi.testclient import TestClient

from vessel_mind.core.chat import (
    AssistantMessage,
    ChatRequest,
    Stop,
    TextDelta,
    Usage,
    UserMessage,
)
from vessel_mind.core.telemetry import JsonlTelemetry
from vessel_mind.server.app import create_app
from vessel_mind.server.state import SessionStore


class EchoModel:
    """Answers every request with "re: <last user text>" and keeps the requests it received."""

    id = "echo"

    def __init__(self) -> None:
        self.requests: list[ChatRequest] = []

    async def stream(self, req: ChatRequest):
        self.requests.append(req)
        last = req.messages[-1]
        assert isinstance(last, UserMessage)
        yield TextDelta(delta=f"re: {last.text}")
        yield Stop(reason="end", usage=Usage(input_tokens=1, output_tokens=1))


def run_to_end(client: TestClient, turn_id: str) -> list[str]:
    """Reads a turn's SSE stream to its end and returns the event names."""
    names: list[str] = []
    with client.stream("GET", f"/turns/{turn_id}/events") as response:
        for line in response.iter_lines():
            if line.startswith("data: "):
                names.append(json.loads(line.removeprefix("data: "))["type"])
    return names


def test_a_turn_without_a_session_creates_one_and_the_next_turn_sees_its_history(tmp_path):
    model = EchoModel()
    with TestClient(create_app(model, state_path=tmp_path / "state.db")) as client:
        first = client.post("/turns", json={"text": "Hello"})
        assert first.status_code == 200
        session_id = first.json()["session_id"]
        assert run_to_end(client, first.json()["turn_id"])[-1] == "done"

        second = client.post("/turns", json={"session_id": session_id, "text": "Again"})
        assert second.status_code == 200
        assert second.json()["session_id"] == session_id
        run_to_end(client, second.json()["turn_id"])

    assert model.requests[0].messages == [UserMessage(text="Hello")]
    assert model.requests[1].messages == [
        UserMessage(text="Hello"),
        AssistantMessage(text="re: Hello"),
        UserMessage(text="Again"),
    ]


class HangOnFirstModel(EchoModel):
    """Hangs on its first request until cancelled; answers like EchoModel after that."""

    id = "hang-on-first"

    def __init__(self) -> None:
        super().__init__()
        self.cancelled = False

    async def stream(self, req: ChatRequest):
        if not self.requests:
            self.requests.append(req)
            yield TextDelta(delta="thinking")
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        async for event in super().stream(req):
            yield event


def test_a_new_turn_cancels_the_running_turn_of_its_session(tmp_path):
    model = HangOnFirstModel()
    with TestClient(create_app(model, state_path=tmp_path / "state.db")) as client:
        first = client.post("/turns", json={"text": "Tell me a long story."}).json()
        session_id = first["session_id"]

        second = client.post("/turns", json={"session_id": session_id, "text": "Never mind."})
        assert second.status_code == 200

        assert run_to_end(client, first["turn_id"]) == ["text", "done"]
        assert run_to_end(client, second.json()["turn_id"]) == ["text", "done"]

    assert model.cancelled
    # The cancelled turn left no history behind.
    assert model.requests[1].messages == [UserMessage(text="Never mind.")]


def test_history_survives_a_restart_on_the_same_state_file(tmp_path):
    state = tmp_path / "state.db"
    with TestClient(create_app(EchoModel(), state_path=state)) as client:
        first = client.post("/turns", json={"text": "Remember me"}).json()
        run_to_end(client, first["turn_id"])

    model = EchoModel()
    with TestClient(create_app(model, state_path=state)) as client:
        later = client.post("/turns", json={"session_id": first["session_id"], "text": "Still?"})
        assert later.status_code == 200
        run_to_end(client, later.json()["turn_id"])

    assert model.requests[0].messages == [
        UserMessage(text="Remember me"),
        AssistantMessage(text="re: Remember me"),
        UserMessage(text="Still?"),
    ]


def test_a_turn_running_at_a_crash_is_not_resumed(tmp_path):
    state = tmp_path / "state.db"
    # A crash: a turn begins and the process dies before it ends.
    crashed = SessionStore.open(state)
    session_id = crashed.create_session()
    turn_id = crashed.begin_turn(session_id)
    crashed.close()

    model = EchoModel()
    with TestClient(create_app(model, state_path=state)) as client:
        # The old turn is not running again: nothing called the model at startup.
        assert model.requests == []
        later = client.post("/turns", json={"session_id": session_id, "text": "Hello?"}).json()
        run_to_end(client, later["turn_id"])
        assert client.get(f"/turns/{turn_id}/events").status_code == 404

    assert model.requests[0].messages == [UserMessage(text="Hello?")]
    reopened = SessionStore.open(state)
    assert reopened.turn_status(turn_id) == "interrupted"
    reopened.close()


def test_past_the_turn_limit_a_turn_is_refused_with_a_clear_error(tmp_path):
    app = create_app(EchoModel(), state_path=tmp_path / "state.db", max_turns_per_session=2)
    with TestClient(app) as client:
        first = client.post("/turns", json={"text": "one"}).json()
        session_id = first["session_id"]
        run_to_end(client, first["turn_id"])
        second = client.post("/turns", json={"session_id": session_id, "text": "two"}).json()
        run_to_end(client, second["turn_id"])

        third = client.post("/turns", json={"session_id": session_id, "text": "three"})

        # Another session is not affected.
        other = client.post("/turns", json={"text": "fresh"})

    assert third.status_code == 429
    assert third.json()["detail"]["code"] == "turn_limit"
    assert "2" in third.json()["detail"]["message"]
    assert other.status_code == 200


class Clock:
    def __init__(self, now: datetime.datetime) -> None:
        self.now = now

    def __call__(self) -> datetime.datetime:
        return self.now


def test_past_the_daily_turn_limit_new_sessions_are_refused_too_until_the_next_day(tmp_path):
    # A visitor who reloads the console gets a new session each time; the daily
    # limit holds across all of them, so the free quota cannot be exhausted.
    clock = Clock(datetime.datetime(2026, 10, 27, 23, 0, tzinfo=datetime.UTC))
    app = create_app(
        EchoModel(),
        state_path=tmp_path / "state.db",
        max_turns_per_session=5,
        max_turns_per_day=2,
        clock=clock,
    )
    with TestClient(app) as client:
        for text in ("one", "two"):
            started = client.post("/turns", json={"text": text}).json()
            run_to_end(client, started["turn_id"])

        refused = client.post("/turns", json={"text": "three"})
        clock.now += datetime.timedelta(hours=1)  # 00:00 UTC: a new day
        next_day = client.post("/turns", json={"text": "four"})

    assert refused.status_code == 429
    assert refused.json()["detail"]["code"] == "daily_turn_limit"
    assert "2" in refused.json()["detail"]["message"]
    assert next_day.status_code == 200


def test_each_turn_ends_with_a_summary_in_the_telemetry_file(tmp_path):
    telemetry = tmp_path / "telemetry.jsonl"
    app = create_app(
        EchoModel(), state_path=tmp_path / "state.db", telemetry=JsonlTelemetry(telemetry)
    )
    with TestClient(app) as client:
        started = client.post("/turns", json={"text": "Hello"}).json()
        run_to_end(client, started["turn_id"])

    (summary,) = [json.loads(line) for line in telemetry.read_text(encoding="utf-8").splitlines()]
    assert summary["kind"] == "turn"
    assert summary["turn_id"] == started["turn_id"]
    assert summary["session_id"] == started["session_id"]
    assert summary["status"] == "done"
    assert summary["steps"] == 1
    assert summary["usage"] == {"input_tokens": 1, "output_tokens": 1, "thinking_tokens": 0}


def test_an_unknown_session_is_a_404(tmp_path):
    with TestClient(create_app(EchoModel(), state_path=tmp_path / "state.db")) as client:
        response = client.post("/turns", json={"session_id": "0" * 32, "text": "hi"})

    assert response.status_code == 404


@pytest.mark.parametrize("session_id", ["", "not-hex", "0" * 31, 5, "../../etc/passwd", "A" * 32])
def test_a_malformed_session_id_is_a_422(tmp_path, session_id):
    with TestClient(create_app(EchoModel(), state_path=tmp_path / "state.db")) as client:
        response = client.post("/turns", json={"session_id": session_id, "text": "hi"})

    assert response.status_code == 422

"""Proposals over the protocol: approve, reject, expiry, restart and demo mode."""

import asyncio
import datetime
import json

from fastapi.testclient import TestClient

from vessel_mind.core.capabilities import CapabilityRegistry
from vessel_mind.core.chat import ChatRequest, Stop, TextDelta, ToolCall, ToolSpec, Usage
from vessel_mind.server.app import create_app

WRITE = {"resource": "workspace", "effect": "write", "exfil": True, "taints_context": False}
START = datetime.datetime(2026, 10, 26, 9, 0, tzinfo=datetime.UTC)


class ProposeOnceModel:
    """Calls notion_create on its first step, then answers in text."""

    id = "propose-once"

    def __init__(self) -> None:
        self.requests: list[ChatRequest] = []

    async def stream(self, req: ChatRequest):
        self.requests.append(req)
        if len(self.requests) % 2 == 1:
            yield ToolCall(id="c1", name="notion_create", input={"title": "Demo"})
            yield Stop(reason="tool_use", usage=Usage())
        else:
            yield TextDelta(delta="It waits for your approval.")
            yield Stop(reason="end", usage=Usage())


class WriteTool:
    spec = ToolSpec(name="notion_create", description="Creates a page.", parameters={})

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def execute(self, input):
        self.calls.append(input)
        return {"page": "p-1"}


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime.datetime:
        return self.now


def app_with(tool, tmp_path, **kwargs):
    tools = CapabilityRegistry(warn=lambda message: None)
    tools.register(tool, WRITE)
    kwargs.setdefault("model", ProposeOnceModel())
    return create_app(tools=tools, state_path=tmp_path / "state.db", **kwargs)


def read_events(client: TestClient, turn_id: str) -> list[dict]:
    with client.stream("GET", f"/turns/{turn_id}/events") as response:
        return [
            json.loads(line.removeprefix("data: "))
            for line in response.iter_lines()
            if line.startswith("data: ")
        ]


def propose(client: TestClient) -> dict:
    """Runs one turn that makes a proposal and returns its proposal event."""
    turn = client.post("/turns", json={"text": "Make a page."}).json()
    events = read_events(client, turn["turn_id"])
    assert [e["type"] for e in events] == ["tool_start", "proposal", "tool_result", "text", "done"]
    return events[1]


def test_approve_executes_the_call_once_even_when_sent_twice(tmp_path):
    tool = WriteTool()
    with TestClient(app_with(tool, tmp_path, clock=Clock())) as client:
        proposal = propose(client)
        assert tool.calls == []
        assert proposal["name"] == "notion_create"
        assert proposal["expires_at"] == "2026-10-26T09:10:00Z"

        first = client.post(f"/proposals/{proposal['proposal_id']}/approve")
        second = client.post(f"/proposals/{proposal['proposal_id']}/approve")

    assert tool.calls == [{"title": "Demo"}]
    assert first.status_code == 200
    assert first.json() == {
        "proposal_id": proposal["proposal_id"],
        "status": "approved",
        "name": "notion_create",
        "input": {"title": "Demo"},
        "result": {"page": "p-1"},
        "is_error": False,
    }
    assert second.status_code == 200
    assert second.json() == first.json()


def test_reject_never_executes_and_a_rejected_proposal_cannot_be_approved(tmp_path):
    tool = WriteTool()
    with TestClient(app_with(tool, tmp_path, clock=Clock())) as client:
        proposal_id = propose(client)["proposal_id"]

        rejected = client.post(f"/proposals/{proposal_id}/reject")
        again = client.post(f"/proposals/{proposal_id}/reject")
        approved = client.post(f"/proposals/{proposal_id}/approve")

    assert tool.calls == []
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"
    assert again.json() == rejected.json()
    assert approved.status_code == 409
    assert approved.json()["detail"]["code"] == "rejected"


def test_a_proposal_expires_after_ten_minutes_and_cannot_be_approved(tmp_path):
    tool = WriteTool()
    clock = Clock()
    with TestClient(app_with(tool, tmp_path, clock=clock)) as client:
        proposal_id = propose(client)["proposal_id"]
        clock.now = START + datetime.timedelta(minutes=10)

        approved = client.post(f"/proposals/{proposal_id}/approve")

    assert tool.calls == []
    assert approved.status_code == 409
    assert approved.json()["detail"]["code"] == "expired"


def test_an_unknown_proposal_is_a_404(tmp_path):
    with TestClient(app_with(WriteTool(), tmp_path)) as client:
        assert client.post(f"/proposals/{'0' * 32}/approve").status_code == 404
        assert client.post(f"/proposals/{'0' * 32}/reject").status_code == 404


def test_after_a_restart_every_pending_proposal_is_expired(tmp_path):
    tool = WriteTool()
    with TestClient(app_with(tool, tmp_path, clock=Clock())) as client:
        kept = propose(client)["proposal_id"]
        approved = propose(client)["proposal_id"]
        assert client.post(f"/proposals/{approved}/approve").status_code == 200

    with TestClient(app_with(tool, tmp_path, clock=Clock())) as client:
        after = client.post(f"/proposals/{kept}/approve")
        again = client.post(f"/proposals/{approved}/approve")

    assert after.status_code == 409
    assert after.json()["detail"]["code"] == "expired"
    # The proposal approved before the restart keeps its outcome and does not run again.
    assert again.status_code == 200
    assert again.json()["result"] == {"page": "p-1"}
    assert tool.calls == [{"title": "Demo"}]


class ProposeThenHangModel:
    """Calls notion_create, then hangs on the next step until cancelled."""

    id = "propose-then-hang"

    async def stream(self, req: ChatRequest):
        if len(req.messages) == 1:
            yield ToolCall(id="c1", name="notion_create", input={"title": "Demo"})
            yield Stop(reason="tool_use", usage=Usage())
            return
        await asyncio.Event().wait()
        yield Stop(reason="end", usage=Usage())


def test_a_cancelled_turn_withdraws_its_proposal(tmp_path):
    tool = WriteTool()
    app = app_with(tool, tmp_path, model=ProposeThenHangModel(), clock=Clock())
    with TestClient(app) as client:
        turn_id = client.post("/turns", json={"text": "Make a page."}).json()["turn_id"]
        events: list[dict] = []
        with client.stream("GET", f"/turns/{turn_id}/events") as response:
            for line in response.iter_lines():
                if not line.startswith("data: "):
                    continue
                events.append(json.loads(line.removeprefix("data: ")))
                if events[-1]["type"] == "tool_result":
                    client.post(f"/turns/{turn_id}/cancel")
        proposal_id = events[1]["proposal_id"]

        approved = client.post(f"/proposals/{proposal_id}/approve")

    assert events[-1]["type"] == "done"
    assert events[-1]["reason"] == "cancelled"
    assert approved.status_code == 409
    assert approved.json()["detail"]["code"] == "withdrawn"
    assert tool.calls == []


def test_in_demo_mode_a_proposal_is_shown_but_its_approval_is_refused(tmp_path):
    tool = WriteTool()
    with TestClient(app_with(tool, tmp_path, demo_mode=True, clock=Clock())) as client:
        proposal = propose(client)

        approved = client.post(f"/proposals/{proposal['proposal_id']}/approve")
        # Still pending: a reject after the refusal succeeds.
        rejected = client.post(f"/proposals/{proposal['proposal_id']}/reject")

    assert approved.status_code == 403
    assert approved.json()["detail"]["code"] == "demo_mode"
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"
    assert tool.calls == []

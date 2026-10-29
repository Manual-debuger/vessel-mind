"""An MCP write tool goes through the proposal flow, against the fake stdio MCP server."""

import json
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from vessel_mind.agent.mcp_client import McpServer
from vessel_mind.core.capabilities import CapabilityRegistry
from vessel_mind.core.chat import ChatRequest, Stop, TextDelta, ToolCall, Usage
from vessel_mind.server.app import create_app

FAKE = Path(__file__).resolve().parent.parent / "fixtures" / "fake_mcp_server.py"
WRITE = {"resource": "workspace", "effect": "write", "exfil": True, "taints_context": True}


class AppendOnceModel:
    """Calls the MCP append tool on its first step, then answers in text."""

    id = "append-once"

    async def stream(self, req: ChatRequest):
        if len(req.messages) == 1:
            yield ToolCall(id="c1", name="fake_append_note", input={"text": "Saved by the agent."})
            yield Stop(reason="tool_use", usage=Usage())
        else:
            yield TextDelta(delta="It waits for your approval.")
            yield Stop(reason="end", usage=Usage())


def mcp_app(tmp_path, **kwargs):
    registry = CapabilityRegistry(warn=lambda message: None)
    server = McpServer(
        "fake",
        command=sys.executable,
        args=[str(FAKE), str(tmp_path / "calls.jsonl")],
        labels={"append_note": WRITE},
        registry=registry,
    )
    return create_app(
        AppendOnceModel(),
        tools=registry,
        services=[server],
        state_path=tmp_path / "state.db",
        **kwargs,
    )


def executions(tmp_path) -> list[dict]:
    calls = tmp_path / "calls.jsonl"
    if not calls.exists():
        return []
    return [json.loads(line) for line in calls.read_text(encoding="utf-8").splitlines()]


def propose(client: TestClient) -> dict:
    turn = client.post("/turns", json={"text": "Save a note."}).json()
    with client.stream("GET", f"/turns/{turn['turn_id']}/events") as response:
        events = [
            json.loads(line.removeprefix("data: "))
            for line in response.iter_lines()
            if line.startswith("data: ")
        ]
    assert [e["type"] for e in events] == ["tool_start", "proposal", "tool_result", "text", "done"]
    assert events[2]["result"]["status"] == "awaiting_approval"
    return events[1]


def test_approving_an_mcp_write_twice_executes_it_once(tmp_path):
    with TestClient(mcp_app(tmp_path)) as client:
        proposal = propose(client)
        assert proposal["name"] == "fake_append_note"
        assert executions(tmp_path) == []

        first = client.post(f"/proposals/{proposal['proposal_id']}/approve")
        second = client.post(f"/proposals/{proposal['proposal_id']}/approve")

    assert first.status_code == 200
    assert first.json()["result"] == {"appended": "Saved by the agent."}
    assert first.json()["is_error"] is False
    assert second.json() == first.json()
    assert executions(tmp_path) == [{"text": "Saved by the agent."}]


def test_rejecting_an_mcp_write_never_executes_it(tmp_path):
    with TestClient(mcp_app(tmp_path)) as client:
        proposal_id = propose(client)["proposal_id"]

        rejected = client.post(f"/proposals/{proposal_id}/reject")
        approved = client.post(f"/proposals/{proposal_id}/approve")

    assert rejected.json()["status"] == "rejected"
    assert approved.status_code == 409
    assert executions(tmp_path) == []


def test_in_demo_mode_an_mcp_write_is_proposed_but_never_executes(tmp_path):
    with TestClient(mcp_app(tmp_path, demo_mode=True)) as client:
        proposal_id = propose(client)["proposal_id"]

        approved = client.post(f"/proposals/{proposal_id}/approve")

    assert approved.status_code == 403
    assert approved.json()["detail"]["code"] == "demo_mode"
    assert executions(tmp_path) == []

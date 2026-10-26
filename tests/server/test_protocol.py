"""The event protocol, driven through FastAPI's test client with a scripted fake ChatModel."""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from vessel_mind.agent.router import Router
from vessel_mind.core.capabilities import CapabilityRegistry
from vessel_mind.core.chat import (
    ChatModelError,
    ChatRequest,
    Stop,
    TextDelta,
    ToolCall,
    ToolSpec,
    Usage,
    UserMessage,
)
from vessel_mind.server.app import create_app


class ScriptedModel:
    """A ChatModel that replays fixed events and keeps the requests it received."""

    id = "scripted"

    def __init__(self, events) -> None:
        self.events = events
        self.requests: list[ChatRequest] = []

    async def stream(self, req: ChatRequest):
        self.requests.append(req)
        for event in self.events:
            yield event


def read_events(client: TestClient, turn_id: str) -> list[tuple[str, dict]]:
    """Reads the SSE stream to its end, as (event name, JSON data) pairs."""
    events: list[tuple[str, dict]] = []
    name = ""
    with client.stream("GET", f"/turns/{turn_id}/events") as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        for line in response.iter_lines():
            if line.startswith("event: "):
                name = line.removeprefix("event: ")
            elif line.startswith("data: "):
                events.append((name, json.loads(line.removeprefix("data: "))))
    return events


def test_a_turn_streams_text_then_done_with_usage_and_step_count():
    model = ScriptedModel(
        [
            TextDelta(delta="Rayleigh "),
            TextDelta(delta="scattering."),
            Stop(reason="end", usage=Usage(input_tokens=14, output_tokens=25, thinking_tokens=3)),
        ]
    )
    with TestClient(create_app(model)) as client:
        started = client.post("/turns", json={"text": "Why is the sky blue?"})
        assert started.status_code == 200
        turn_id = started.json()["turn_id"]

        events = read_events(client, turn_id)

    assert events == [
        ("text", {"type": "text", "text": "Rayleigh "}),
        ("text", {"type": "text", "text": "scattering."}),
        (
            "done",
            {
                "type": "done",
                "reason": "end",
                "usage": {"input_tokens": 14, "output_tokens": 25, "thinking_tokens": 3},
                "steps": 1,
            },
        ),
    ]
    assert model.requests[0].messages[-1] == UserMessage(text="Why is the sky blue?")


class StepModel:
    """Replays one scripted list of events per model step."""

    id = "steps"

    def __init__(self, *steps) -> None:
        self.steps = list(steps)
        self.requests: list[ChatRequest] = []

    async def stream(self, req: ChatRequest):
        self.requests.append(req)
        for event in self.steps[len(self.requests) - 1]:
            yield event


class SearchTool:
    spec = ToolSpec(name="search_docs", description="Search the corpus.", parameters={})

    async def execute(self, input):
        return {"hits": [f"GLOSSARY.md#{input['query']}"]}


def test_a_turn_streams_tool_start_and_tool_result_events_then_done():
    tools = CapabilityRegistry()
    tools.register(
        SearchTool(),
        {"resource": "public_corpus", "effect": "read", "exfil": False, "taints_context": False},
    )
    model = StepModel(
        [
            ToolCall(id="c1", name="search_docs", input={"query": "proposal"}),
            Stop(reason="tool_use", usage=Usage(input_tokens=5)),
        ],
        [TextDelta(delta="It waits."), Stop(reason="end", usage=Usage(input_tokens=9))],
    )
    with TestClient(create_app(model, tools=tools)) as client:
        started = client.post("/turns", json={"text": "What is a proposal?"}).json()
        events = read_events(client, started["turn_id"])

    assert [name for name, _ in events] == ["tool_start", "tool_result", "text", "done"]
    assert events[0][1] == {
        "type": "tool_start",
        "call_id": "c1",
        "name": "search_docs",
        "input": {"query": "proposal"},
    }
    assert events[1][1] == {
        "type": "tool_result",
        "call_id": "c1",
        "name": "search_docs",
        "result": {"hits": ["GLOSSARY.md#proposal"]},
        "is_error": False,
    }
    assert events[-1][1]["steps"] == 2
    assert [t.name for t in model.requests[0].tools] == ["search_docs"]


class HangingModel:
    """Streams one delta, then generates forever until cancelled."""

    id = "hanging"

    def __init__(self) -> None:
        self.cancelled = False

    async def stream(self, req: ChatRequest):
        yield TextDelta(delta="Once upon a time")
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        yield TextDelta(delta=" never sent")


def test_cancel_ends_the_stream_with_done_and_stops_generation():
    model = HangingModel()
    events: list[tuple[str, dict]] = []
    with TestClient(create_app(model)) as client:
        turn_id = client.post("/turns", json={"text": "Tell me a long story."}).json()["turn_id"]
        name = ""
        with client.stream("GET", f"/turns/{turn_id}/events") as response:
            for line in response.iter_lines():
                if line.startswith("event: "):
                    name = line.removeprefix("event: ")
                elif line.startswith("data: "):
                    events.append((name, json.loads(line.removeprefix("data: "))))
                    if name == "text":
                        cancelled = client.post(f"/turns/{turn_id}/cancel")
                        assert cancelled.status_code == 202

    assert events == [
        ("text", {"type": "text", "text": "Once upon a time"}),
        (
            "done",
            {
                "type": "done",
                "reason": "cancelled",
                "usage": {"input_tokens": 0, "output_tokens": 0, "thinking_tokens": 0},
                "steps": 1,
            },
        ),
    ]
    assert model.cancelled


class FailingModel:
    id = "failing"

    async def stream(self, req: ChatRequest):
        raise ChatModelError("failing: HTTP 429")
        yield  # makes this an async generator


def test_a_model_failure_ends_the_stream_with_an_error_event():
    with TestClient(create_app(FailingModel())) as client:
        turn_id = client.post("/turns", json={"text": "Hello?"}).json()["turn_id"]
        events = read_events(client, turn_id)

    assert events == [
        ("error", {"type": "error", "code": "model_error", "message": "failing: HTTP 429"})
    ]


@pytest.mark.parametrize("body", [{}, {"text": ""}, {"text": 5}, {"text": "hi", "extra": 1}])
def test_an_invalid_turn_body_is_a_422(body):
    with TestClient(create_app(ScriptedModel([]))) as client:
        assert client.post("/turns", json=body).status_code == 422


def test_an_unknown_turn_is_a_404():
    with TestClient(create_app(ScriptedModel([]))) as client:
        assert client.get("/turns/nope/events").status_code == 404
        assert client.post("/turns/nope/cancel").status_code == 404


def test_health_reports_ready():
    with TestClient(create_app(ScriptedModel([]))) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


class NamedStepModel:
    """A ChatModel with an id that answers each request with its next scripted step."""

    def __init__(self, id: str, *steps) -> None:
        self.id = id
        self.steps = list(steps)

    async def stream(self, req: ChatRequest):
        for event in self.steps.pop(0):
            yield event


def test_a_routed_turn_streams_the_route_first_then_the_answer():
    fast = NamedStepModel(
        "small",
        [TextDelta(delta='{"route": "fast"}'), Stop(reason="end", usage=Usage(input_tokens=9))],
        [TextDelta(delta="Hello."), Stop(reason="end", usage=Usage(input_tokens=20))],
    )
    slow = NamedStepModel("large")
    app = create_app(slow, router=Router(fast, slow))

    with TestClient(app) as client:
        turn_id = client.post("/turns", json={"text": "Hi"}).json()["turn_id"]
        events = read_events(client, turn_id)

    assert events[0] == (
        "route",
        {
            "type": "route",
            "route": "fast",
            "reason": "classified",
            "router_model": "small",
            "router_usage": {"input_tokens": 9, "output_tokens": 0, "thinking_tokens": 0},
            "model": "small",
        },
    )
    assert [name for name, _ in events[1:]] == ["text", "done"]
    assert events[-1][1]["usage"]["input_tokens"] == 20

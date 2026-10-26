"""The agent loop with tools, driven by a scripted fake ChatModel. No live API."""

import asyncio
import datetime
import json

import pytest
from pydantic import JsonValue

from vessel_mind.agent.loop import MAX_STEPS, TurnTally, run_turn
from vessel_mind.agent.proposals import Proposal
from vessel_mind.core.capabilities import CapabilityRegistry, Tool, ToolError
from vessel_mind.core.chat import (
    AssistantMessage,
    ChatRequest,
    Stop,
    TextDelta,
    ToolCall,
    ToolMessage,
    ToolSpec,
    Usage,
    UserMessage,
)
from vessel_mind.core.events import (
    DoneEvent,
    ProposalEvent,
    TextEvent,
    ToolResultEvent,
    ToolStartEvent,
)


class ScriptedModel:
    """Replays one scripted list of events per step and keeps every request."""

    id = "scripted"

    def __init__(self, *steps) -> None:
        self.steps = list(steps)
        self.requests: list[ChatRequest] = []

    async def stream(self, req: ChatRequest):
        self.requests.append(req)
        for event in self.steps[min(len(self.requests), len(self.steps)) - 1]:
            yield event


class FakeTool:
    def __init__(self, name: str, result=None, error: Exception | None = None) -> None:
        self.spec = ToolSpec(name=name, description=f"The {name} tool.", parameters={})
        self.result = result
        self.error = error
        self.calls: list[dict] = []

    async def execute(self, input):
        self.calls.append(input)
        if self.error is not None:
            raise self.error
        return self.result


READ = {"resource": "public_corpus", "effect": "read", "exfil": False, "taints_context": False}
WRITE = {"resource": "workspace", "effect": "write", "exfil": True, "taints_context": False}


def registry(*tools: tuple[Tool, dict]) -> CapabilityRegistry:
    reg = CapabilityRegistry(warn=lambda message: None)
    for tool, labels in tools:
        reg.register(tool, labels)
    return reg


def call(call_id: str, name: str, **input) -> ToolCall:
    return ToolCall(id=call_id, name=name, input=input)


async def collect(*args, **kwargs) -> list:
    return [event async for event in run_turn(*args, **kwargs)]


@pytest.mark.asyncio
async def test_a_turn_calling_two_read_tools_emits_events_in_order_and_ends_done():
    search = FakeTool("search_docs", result={"hits": ["GLOSSARY.md#proposal"]})
    clock = FakeTool("clock", result="2026-10-26T09:00:00Z")
    first = call("c1", "search_docs", query="proposal")
    second = call("c2", "clock")
    model = ScriptedModel(
        [first, second, Stop(reason="tool_use", usage=Usage(input_tokens=10, output_tokens=5))],
        [
            TextDelta(delta="A proposal waits for approval."),
            Stop(reason="end", usage=Usage(input_tokens=30, output_tokens=7)),
        ],
    )
    tally = TurnTally()

    events = await collect(
        model,
        [],
        "What is a proposal?",
        tools=registry((search, READ), (clock, READ)),
        tally=tally,
    )

    assert events == [
        ToolStartEvent(call_id="c1", name="search_docs", input={"query": "proposal"}),
        ToolResultEvent(
            call_id="c1",
            name="search_docs",
            result={"hits": ["GLOSSARY.md#proposal"]},
            is_error=False,
        ),
        ToolStartEvent(call_id="c2", name="clock", input={}),
        ToolResultEvent(call_id="c2", name="clock", result="2026-10-26T09:00:00Z", is_error=False),
        TextEvent(text="A proposal waits for approval."),
        DoneEvent(reason="end", usage=Usage(input_tokens=40, output_tokens=12), steps=2),
    ]
    assert search.calls == [{"query": "proposal"}]
    # Both steps offer the tools; the second carries the calls and their results.
    assert [t.name for t in model.requests[0].tools] == ["search_docs", "clock"]
    assert model.requests[1].messages == tally.messages[:4]
    assert tally.messages == [
        UserMessage(text="What is a proposal?"),
        AssistantMessage(text="", tool_calls=[first, second]),
        ToolMessage(tool_call_id="c1", content='{"hits": ["GLOSSARY.md#proposal"]}'),
        ToolMessage(tool_call_id="c2", content='"2026-10-26T09:00:00Z"'),
        AssistantMessage(text="A proposal waits for approval."),
    ]


class FakeProposals:
    """Records each proposal and hands out ids p1, p2, ..."""

    def __init__(self) -> None:
        self.made: list[tuple[ToolCall, str]] = []

    def propose(self, call: ToolCall, effect) -> Proposal:
        self.made.append((call, effect))
        return Proposal(
            id=f"p{len(self.made)}",
            expires_at=datetime.datetime(2026, 10, 26, 9, 10, tzinfo=datetime.UTC),
        )


@pytest.mark.asyncio
async def test_a_write_tool_becomes_a_proposal_and_is_never_executed():
    notion_create = FakeTool("notion_create", result="created")
    write_call = call("c1", "notion_create", title="x")
    model = ScriptedModel(
        [write_call, Stop(reason="tool_use", usage=Usage())],
        [TextDelta(delta="Approve it to create the page."), Stop(reason="end", usage=Usage())],
    )
    proposals = FakeProposals()
    tally = TurnTally()

    events = await collect(
        model,
        [],
        "Make a page.",
        tools=registry((notion_create, WRITE)),
        proposals=proposals,
        tally=tally,
    )

    assert notion_create.calls == []
    assert proposals.made == [(write_call, "write")]
    awaiting: dict[str, JsonValue] = {"status": "awaiting_approval", "proposal_id": "p1"}
    assert events[:3] == [
        ToolStartEvent(call_id="c1", name="notion_create", input={"title": "x"}),
        ProposalEvent(
            proposal_id="p1",
            call_id="c1",
            name="notion_create",
            input={"title": "x"},
            effect="write",
            expires_at=datetime.datetime(2026, 10, 26, 9, 10, tzinfo=datetime.UTC),
        ),
        ToolResultEvent(call_id="c1", name="notion_create", result=awaiting, is_error=False),
    ]
    assert tally.messages[2] == ToolMessage(tool_call_id="c1", content=json.dumps(awaiting))
    assert isinstance(events[-1], DoneEvent)


@pytest.mark.asyncio
async def test_without_a_proposal_store_a_write_tool_is_refused_and_never_executed():
    notion_create = FakeTool("notion_create", result="created")
    model = ScriptedModel(
        [call("c1", "notion_create", title="x"), Stop(reason="tool_use", usage=Usage())],
        [TextDelta(delta="I could not."), Stop(reason="end", usage=Usage())],
    )
    tally = TurnTally()

    events = await collect(
        model, [], "Make a page.", tools=registry((notion_create, WRITE)), tally=tally
    )

    assert notion_create.calls == []
    result = events[1]
    assert isinstance(result, ToolResultEvent)
    assert result.is_error
    sent = tally.messages[2]
    assert isinstance(sent, ToolMessage)
    assert json.loads(sent.content) == result.result


@pytest.mark.asyncio
async def test_an_unknown_tool_is_an_error_result_not_a_crash():
    model = ScriptedModel(
        [call("c1", "made_up"), Stop(reason="tool_use", usage=Usage())],
        [TextDelta(delta="Sorry."), Stop(reason="end", usage=Usage())],
    )

    events = await collect(model, [], "Hi", tools=registry())

    assert events[1] == ToolResultEvent(
        call_id="c1", name="made_up", result={"error": "Unknown tool 'made_up'."}, is_error=True
    )
    assert isinstance(events[-1], DoneEvent)


@pytest.mark.asyncio
async def test_a_failing_tool_reports_its_tool_error_and_hides_any_other_exception():
    told = FakeTool("told", error=ToolError("No such page."))
    crashed = FakeTool("crashed", error=RuntimeError("secret path /home/x"))
    model = ScriptedModel(
        [call("c1", "told"), call("c2", "crashed"), Stop(reason="tool_use", usage=Usage())],
        [Stop(reason="end", usage=Usage())],
    )

    events = await collect(model, [], "Hi", tools=registry((told, READ), (crashed, READ)))

    results = [e for e in events if isinstance(e, ToolResultEvent)]
    assert [(r.result, r.is_error) for r in results] == [
        ({"error": "No such page."}, True),
        ({"error": "The tool failed."}, True),
    ]


@pytest.mark.asyncio
async def test_a_result_that_is_not_json_is_an_error_result():
    odd = FakeTool("odd", result={"when": object()})
    model = ScriptedModel(
        [call("c1", "odd"), Stop(reason="tool_use", usage=Usage())],
        [Stop(reason="end", usage=Usage())],
    )

    events = await collect(model, [], "Hi", tools=registry((odd, READ)))

    assert events[1] == ToolResultEvent(
        call_id="c1", name="odd", result={"error": "The tool failed."}, is_error=True
    )
    assert isinstance(events[-1], DoneEvent)


@pytest.mark.asyncio
async def test_a_turn_stops_after_the_step_limit_and_the_last_step_offers_no_tools():
    looping = FakeTool("again", result="ok")
    model = ScriptedModel([call("c", "again"), Stop(reason="tool_use", usage=Usage())])

    events = await collect(model, [], "Loop", tools=registry((looping, READ)))

    assert MAX_STEPS == 8
    assert len(model.requests) == 8
    assert all(req.tools for req in model.requests[:-1])
    assert model.requests[-1].tools == []
    assert len(looping.calls) == 7
    assert events[-1] == DoneEvent(reason="max_steps", usage=Usage(), steps=8)


class HangingTool:
    spec = ToolSpec(name="slow_search", description="Never returns.", parameters={})

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = False

    async def execute(self, input):
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


@pytest.mark.asyncio
async def test_cancelling_during_a_tool_call_stops_the_turn_with_no_further_text():
    tool = HangingTool()
    model = ScriptedModel(
        [
            TextDelta(delta="Let me check."),
            call("c1", "slow_search"),
            Stop(reason="tool_use", usage=Usage()),
        ],
        [TextDelta(delta="never sent"), Stop(reason="end", usage=Usage())],
    )
    seen: list = []

    async def consume() -> None:
        async for event in run_turn(model, [], "Hi", tools=registry((tool, READ))):
            seen.append(event)

    task = asyncio.create_task(consume())
    await asyncio.wait_for(tool.started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert tool.cancelled
    assert seen == [
        TextEvent(text="Let me check."),
        ToolStartEvent(call_id="c1", name="slow_search", input={}),
    ]
    assert len(model.requests) == 1

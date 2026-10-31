"""The router (§5.7), driven by scripted fake models. No live API."""

import asyncio
import json
from collections.abc import AsyncGenerator

import pytest
from pydantic import JsonValue

from vessel_mind.agent.loop import TurnTally
from vessel_mind.agent.router import Router
from vessel_mind.core.capabilities import CapabilityRegistry
from vessel_mind.core.chat import (
    AssistantMessage,
    ChatModelError,
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
    ErrorEvent,
    RouteEvent,
    TextEvent,
    ToolResultEvent,
    ToolStartEvent,
)


class ScriptedModel:
    """Replays one scripted list of events per request and keeps every request."""

    def __init__(self, id: str, *steps) -> None:
        self.id = id
        self.steps = list(steps)
        self.requests: list[ChatRequest] = []

    async def stream(self, req: ChatRequest):
        self.requests.append(req)
        step = self.steps[len(self.requests) - 1]
        if isinstance(step, Exception):
            raise step
        for event in step:
            yield event


class HangingModel:
    """Never answers: a classification that times out. asked is set once it is asked."""

    id = "hanging"

    def __init__(self) -> None:
        self.requests: list[ChatRequest] = []
        self.asked = asyncio.Event()

    async def stream(self, req: ChatRequest):
        self.requests.append(req)
        self.asked.set()
        await asyncio.Event().wait()
        yield TextDelta(delta="never")


class FakeSearch:
    spec = ToolSpec(name="search_docs", description="Search.", parameters={})

    def __init__(self, result) -> None:
        self.result = result
        self.calls: list[dict] = []

    async def execute(self, input):
        self.calls.append(input)
        return self.result


RESULTS: JsonValue = {
    "results": [
        {
            "n": 1,
            "source": "vessel-mind/GLOSSARY.md#proposal",
            "url": "https://github.com/o/r/blob/c/GLOSSARY.md#proposal",
            "heading": "Proposal",
            "text": "A proposal waits for a human decision.",
            "score": 0.03,
        }
    ]
}

READ = {"resource": "public_corpus", "effect": "read", "exfil": False, "taints_context": False}


def tools(*registered) -> CapabilityRegistry:
    registry = CapabilityRegistry(warn=lambda message: None)
    for tool in registered:
        registry.register(tool, READ)
    return registry


ROUTER_USAGE = Usage(input_tokens=50, output_tokens=6)
ANSWER_USAGE = Usage(input_tokens=200, output_tokens=12)


def classified(text: str) -> list:
    return [TextDelta(delta=text), Stop(reason="end", usage=ROUTER_USAGE)]


def answer(text: str) -> list:
    return [TextDelta(delta=text), Stop(reason="end", usage=ANSWER_USAGE)]


async def collect(router: Router, history, text: str, **kwargs) -> list:
    return [event async for event in router.run(history, text, **kwargs)]


@pytest.mark.asyncio
async def test_a_fast_turn_retrieves_first_and_answers_on_the_fast_model_with_that_context():
    fast = ScriptedModel(
        "small", classified('{"route": "fast"}'), answer("It waits for approval [1].")
    )
    slow = ScriptedModel("large")
    search = FakeSearch(RESULTS)
    history = [UserMessage(text="Hi"), AssistantMessage(text="Hello.")]

    events = await collect(
        Router(fast, slow), history, "What is a proposal?", tools=tools(search), system="Be brief."
    )

    assert events == [
        RouteEvent(
            route="fast",
            reason="classified",
            router_model="small",
            router_usage=ROUTER_USAGE,
            model="small",
        ),
        ToolStartEvent(
            call_id="fast-path-search", name="search_docs", input={"query": "What is a proposal?"}
        ),
        ToolResultEvent(
            call_id="fast-path-search", name="search_docs", result=RESULTS, is_error=False
        ),
        TextEvent(text="It waits for approval [1]."),
        DoneEvent(reason="end", usage=ANSWER_USAGE, steps=1),
    ]
    assert search.calls == [{"query": "What is a proposal?"}]
    assert slow.requests == []
    classify, reply = fast.requests
    # The classification sees the session as text and offers no tools.
    assert classify.tools == []
    assert classify.messages[-1] == UserMessage(text="What is a proposal?")
    # The answer gets the retrieved sections, numbered as search_docs numbers them.
    assert reply.tools == []
    assert reply.system is not None
    assert reply.system.startswith("Be brief.")
    assert "A proposal waits for a human decision." in reply.system
    assert '"n": 1' in reply.system
    assert reply.messages == [*history, UserMessage(text="What is a proposal?")]


@pytest.mark.asyncio
async def test_a_fast_turn_keeps_its_search_in_history_so_later_citations_resolve():
    fast = ScriptedModel("small", classified('{"route": "fast"}'), answer("It waits [1]."))
    tally = TurnTally()

    await collect(
        Router(fast, ScriptedModel("large")),
        [],
        "What is a proposal?",
        tools=tools(FakeSearch(RESULTS)),
        tally=tally,
    )

    # The same shape as a slow turn's search, so [n] in the answer still points
    # at its sources in every later turn.
    search = ToolCall(
        id="fast-path-search", name="search_docs", input={"query": "What is a proposal?"}
    )
    assert tally.messages == [
        UserMessage(text="What is a proposal?"),
        AssistantMessage(text="", tool_calls=[search]),
        ToolMessage(tool_call_id="fast-path-search", content=json.dumps(RESULTS)),
        AssistantMessage(text="It waits [1]."),
    ]


@pytest.mark.asyncio
async def test_a_fast_turn_sends_earlier_tool_calls_as_text_since_it_offers_no_tools():
    fast = ScriptedModel("small", classified('{"route": "fast"}'), answer("Thanks!"))
    call = ToolCall(id="c1", name="search_docs", input={"query": "x"})
    history = [
        UserMessage(text="Find x."),
        AssistantMessage(text="", tool_calls=[call]),
        ToolMessage(tool_call_id="c1", content="{}"),
        AssistantMessage(text="Found x [1]."),
    ]

    await collect(Router(fast, ScriptedModel("large")), history, "Thanks.")

    reply = fast.requests[1]
    assert reply.tools == []
    assert reply.messages == [
        UserMessage(text="Find x."),
        AssistantMessage(text="Found x [1]."),
        UserMessage(text="Thanks."),
    ]


class RecordedTelemetry:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict]] = []

    def record(self, kind: str, data) -> None:
        self.records.append((kind, dict(data)))


@pytest.mark.asyncio
async def test_the_route_decision_goes_to_telemetry_with_both_models_usage():
    fast = ScriptedModel("small", classified('{"route": "fast"}'), answer("Hello!"))
    telemetry = RecordedTelemetry()

    await collect(Router(fast, ScriptedModel("large"), telemetry=telemetry), [], "Hi")

    assert telemetry.records == [
        (
            "route",
            {
                "route": "fast",
                "reason": "classified",
                "router_model": "small",
                "router_usage": ROUTER_USAGE.model_dump(),
                "model": "small",
                "usage": ANSWER_USAGE.model_dump(),
            },
        )
    ]


@pytest.mark.asyncio
async def test_a_slow_turn_runs_the_tool_loop_on_the_slow_model():
    fast = ScriptedModel("small", classified('{"route": "slow"}'))
    search = FakeSearch(RESULTS)
    slow = ScriptedModel(
        "large",
        [
            ToolCall(id="c1", name="search_docs", input={"query": "proposal"}),
            Stop(reason="tool_use", usage=ANSWER_USAGE),
        ],
        answer("Compared [1]."),
    )

    events = await collect(Router(fast, slow), [], "Compare X and Y.", tools=tools(search))

    assert events[0] == RouteEvent(
        route="slow",
        reason="classified",
        router_model="small",
        router_usage=ROUTER_USAGE,
        model="large",
    )
    assert [type(e) for e in events[1:]] == [
        ToolStartEvent,
        ToolResultEvent,
        TextEvent,
        DoneEvent,
    ]
    assert events[-1] == DoneEvent(reason="end", usage=ANSWER_USAGE + ANSWER_USAGE, steps=2)
    assert len(fast.requests) == 1  # only the classification
    assert [t.name for t in slow.requests[0].tools] == ["search_docs"]


@pytest.mark.asyncio
async def test_a_classification_that_times_out_falls_back_to_the_slow_path():
    fast = HangingModel()
    slow = ScriptedModel("large", answer("Slow answer."))

    events = await collect(Router(fast, slow, timeout=0.01), [], "Hello?")

    assert events == [
        RouteEvent(
            route="slow",
            reason="timeout",
            router_model="hanging",
            router_usage=Usage(),
            model="large",
        ),
        TextEvent(text="Slow answer."),
        DoneEvent(reason="end", usage=ANSWER_USAGE, steps=1),
    ]


@pytest.mark.parametrize(
    "reply",
    [
        "fast, I think",
        '{"route": "medium"}',
        '{"route": "fast", "why": "one search"}',
        '{"route": "fast"} {"route": "slow"}',
        "",
    ],
)
@pytest.mark.asyncio
async def test_a_classification_that_is_not_the_expected_json_falls_back_to_the_slow_path(reply):
    fast = ScriptedModel("small", classified(reply))
    slow = ScriptedModel("large", answer("Slow answer."))

    events = await collect(Router(fast, slow), [], "Hello?")

    assert events[0] == RouteEvent(
        route="slow",
        reason="invalid",
        router_model="small",
        router_usage=ROUTER_USAGE,
        model="large",
    )
    assert events[1:] == [
        TextEvent(text="Slow answer."),
        DoneEvent(reason="end", usage=ANSWER_USAGE, steps=1),
    ]


@pytest.mark.parametrize("reply", ["", '{"rou'])
@pytest.mark.asyncio
async def test_a_classification_cut_off_by_its_token_budget_is_reported_as_truncated(reply):
    # Gemini can spend the whole budget thinking and write no text at all.
    fast = ScriptedModel(
        "small", [TextDelta(delta=reply), Stop(reason="max_tokens", usage=ROUTER_USAGE)]
    )
    slow = ScriptedModel("large", answer("Slow answer."))

    events = await collect(Router(fast, slow), [], "Hello?")

    assert events[0] == RouteEvent(
        route="slow",
        reason="max_tokens",
        router_model="small",
        router_usage=ROUTER_USAGE,
        model="large",
    )


@pytest.mark.asyncio
async def test_json_in_a_markdown_code_fence_is_still_a_classification():
    fast = ScriptedModel("small", classified('```json\n{"route": "fast"}\n```'), answer("Hi!"))
    slow = ScriptedModel("large")

    events = await collect(Router(fast, slow), [], "Hello!")

    assert events[0].route == "fast"
    assert events[0].reason == "classified"


@pytest.mark.asyncio
async def test_a_fast_model_error_while_classifying_falls_back_to_the_slow_path():
    fast = ScriptedModel("small", ChatModelError("small: HTTP 503"))
    slow = ScriptedModel("large", answer("Slow answer."))

    events = await collect(Router(fast, slow), [], "Hello?")

    assert events[0] == RouteEvent(
        route="slow",
        reason="model_error",
        router_model="small",
        router_usage=Usage(),
        model="large",
    )
    assert events[-1] == DoneEvent(reason="end", usage=ANSWER_USAGE, steps=1)


@pytest.mark.asyncio
async def test_with_the_router_off_every_turn_takes_the_slow_path_without_classifying():
    fast = ScriptedModel("small")
    slow = ScriptedModel("large", answer("Slow answer."))

    events = await collect(Router(fast, slow, enabled=False), [], "Hello!")

    assert events[0] == RouteEvent(
        route="slow", reason="router_off", router_model=None, router_usage=Usage(), model="large"
    )
    assert fast.requests == []


@pytest.mark.asyncio
async def test_a_fast_turn_without_a_search_tool_answers_with_no_retrieval():
    fast = ScriptedModel("small", classified('{"route": "fast"}'), answer("Hello!"))
    slow = ScriptedModel("large")

    events = await collect(Router(fast, slow), [], "Hi", system="Be brief.")

    assert [type(e) for e in events] == [RouteEvent, TextEvent, DoneEvent]
    assert fast.requests[1].system == "Be brief."


@pytest.mark.asyncio
async def test_when_the_slow_model_fails_its_fallback_answers_and_the_route_names_it():
    fast = ScriptedModel("small", classified('{"route": "slow"}'))
    slow = ScriptedModel("large", ChatModelError("large: HTTP 429"))
    fallback = ScriptedModel("large-older", answer("Fallback answer."))

    events = await collect(Router(fast, slow, slow_fallbacks=[fallback]), [], "Compare X and Y.")

    assert events == [
        RouteEvent(
            route="slow",
            reason="classified",
            router_model="small",
            router_usage=ROUTER_USAGE,
            model="large-older",
        ),
        TextEvent(text="Fallback answer."),
        DoneEvent(reason="end", usage=ANSWER_USAGE, steps=1),
    ]
    assert len(slow.requests) == 1
    assert fallback.requests[0].messages == slow.requests[0].messages


@pytest.mark.asyncio
async def test_once_a_fallback_answers_later_steps_of_the_turn_skip_the_failed_model():
    fast = ScriptedModel("small", classified('{"route": "slow"}'))
    slow = ScriptedModel(
        "large",
        ChatModelError("large: HTTP 404"),
        ChatModelError("large: HTTP 404"),
    )
    fallback = ScriptedModel(
        "large-older",
        [
            ToolCall(id="c1", name="search_docs", input={"query": "proposal"}),
            Stop(reason="tool_use", usage=ANSWER_USAGE),
        ],
        answer("Compared [1]."),
    )
    search = FakeSearch(RESULTS)

    events = await collect(
        Router(fast, slow, slow_fallbacks=[fallback]),
        [],
        "Compare X and Y.",
        tools=tools(search),
    )

    assert len(slow.requests) == 1
    assert len(fallback.requests) == 2
    assert events[0].model == "large-older"
    assert events[-1] == DoneEvent(reason="end", usage=ANSWER_USAGE + ANSWER_USAGE, steps=2)


@pytest.mark.asyncio
async def test_on_the_fast_path_the_route_names_the_fallback_and_still_comes_first():
    fast = ScriptedModel("small", classified('{"route": "fast"}'), ChatModelError("small: 503"))
    fallback = ScriptedModel("small-older", answer("It waits [1]."))
    search = FakeSearch(RESULTS)

    events = await collect(
        Router(fast, ScriptedModel("large"), fast_fallbacks=[fallback]),
        [],
        "What is a proposal?",
        tools=tools(search),
    )

    assert [type(e) for e in events] == [
        RouteEvent,
        ToolStartEvent,
        ToolResultEvent,
        TextEvent,
        DoneEvent,
    ]
    assert events[0].model == "small-older"
    assert events[0].router_model == "small"


@pytest.mark.asyncio
async def test_a_classification_the_fast_model_cannot_make_goes_to_its_fallback():
    fast = ScriptedModel("small", ChatModelError("small: HTTP 429"))
    fallback = ScriptedModel("small-older", classified('{"route": "slow"}'))
    slow = ScriptedModel("large", answer("Slow answer."))

    events = await collect(Router(fast, slow, fast_fallbacks=[fallback]), [], "Hello?")

    assert events[0] == RouteEvent(
        route="slow",
        reason="classified",
        router_model="small-older",
        router_usage=ROUTER_USAGE,
        model="large",
    )


@pytest.mark.asyncio
async def test_a_fast_turn_classified_by_the_fallback_is_answered_by_it_without_asking_the_failed_model():
    fast = ScriptedModel("small", ChatModelError("small: HTTP 429"))
    fallback = ScriptedModel("small-older", classified('{"route": "fast"}'), answer("Hello!"))
    telemetry = RecordedTelemetry()

    events = await collect(
        Router(fast, ScriptedModel("large"), fast_fallbacks=[fallback], telemetry=telemetry),
        [],
        "Hi",
    )

    assert len(fast.requests) == 1
    assert len(fallback.requests) == 2
    assert events[0] == RouteEvent(
        route="fast",
        reason="classified",
        router_model="small-older",
        router_usage=ROUTER_USAGE,
        model="small-older",
    )
    assert telemetry.records[0][1]["model"] == "small-older"


@pytest.mark.asyncio
async def test_when_every_model_of_the_path_fails_the_turn_ends_with_a_model_error():
    fast = ScriptedModel("small", classified('{"route": "slow"}'))
    slow = ScriptedModel("large", ChatModelError("large: HTTP 429"))
    fallback = ScriptedModel("large-older", ChatModelError("large-older: timed out"))

    events = await collect(Router(fast, slow, slow_fallbacks=[fallback]), [], "Hello?")

    assert events[0].model == "large-older"  # no model answered: the one it was asking
    assert isinstance(events[-1], ErrorEvent)
    assert events[-1].code == "model_error"
    assert "large-older" in events[-1].message


@pytest.mark.asyncio
async def test_cancelling_before_a_slow_first_byte_ends_the_turn_within_100_ms():
    # With a fallback list the route event waits for the first byte; cancelling
    # meanwhile still stops the model at once.
    class SlowFirstByte:
        id = "large"

        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.cancelled = False

        async def stream(self, req):
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
            yield TextDelta(delta="never")

    fast = ScriptedModel("small", classified('{"route": "slow"}'))
    slow = SlowFirstByte()
    fallback = ScriptedModel("large-older", answer("Never asked."))
    router = Router(fast, slow, slow_fallbacks=[fallback])
    seen: list = []

    async def consume() -> None:
        async for event in router.run([], "Compare X and Y."):
            seen.append(event)

    task = asyncio.create_task(consume())
    await asyncio.wait_for(slow.started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=0.1)

    assert slow.cancelled
    assert seen == []
    assert fallback.requests == []


@pytest.mark.asyncio
async def test_a_model_that_fails_after_it_started_answering_is_not_replaced():
    # Its text already reached the client; a second answer would follow the first.
    class FailsMidAnswer:
        id = "large"

        async def stream(self, req):
            yield TextDelta(delta="Half an ")
            raise ChatModelError("large: timed out")

    fast = ScriptedModel("small", classified('{"route": "slow"}'))
    fallback = ScriptedModel("large-older", answer("Whole answer."))

    events = await collect(Router(fast, FailsMidAnswer(), slow_fallbacks=[fallback]), [], "Hello?")

    assert events[0].model == "large"
    assert events[1] == TextEvent(text="Half an ")
    assert isinstance(events[-1], ErrorEvent)
    assert fallback.requests == []


@pytest.mark.asyncio
async def test_the_classification_sees_the_session_as_text_only():
    fast = ScriptedModel("small", classified('{"route": "slow"}'))
    slow = ScriptedModel("large", answer("Ok."))
    call = ToolCall(id="c1", name="search_docs", input={"query": "x"})
    history = [
        UserMessage(text="Find x."),
        AssistantMessage(text="", tool_calls=[call]),
        ToolMessage(tool_call_id="c1", content="{}"),
        AssistantMessage(text="Found x [1]."),
    ]

    await collect(Router(fast, slow), history, "And y?")

    assert fast.requests[0].messages == [
        UserMessage(text="Find x."),
        AssistantMessage(text="Found x [1]."),
        UserMessage(text="And y?"),
    ]


@pytest.mark.asyncio
async def test_the_classification_is_told_that_a_request_to_change_something_is_slow():
    # The fast path offers no tools, so a write routed fast can never become a
    # proposal. Unprompted, qwen3:8b routed 5 of 6 such requests fast.
    fast = ScriptedModel("small", classified('{"route": "slow"}'))
    slow = ScriptedModel("large", answer("Ok."))

    await collect(Router(fast, slow), [], "Add an item to the Notion page.")

    system = fast.requests[0].system
    assert system is not None
    assert "add, change or delete something" in system
    assert "needs a write tool, so it is slow" in system


class SearchesThenHangs:
    """Calls search_docs in its first step, then streams a delta and hangs until cancelled."""

    id = "large"

    def __init__(self) -> None:
        self.requests: list[ChatRequest] = []
        self.hanging = asyncio.Event()

    async def stream(self, req: ChatRequest):
        self.requests.append(req)
        if len(self.requests) == 1:
            yield ToolCall(id="c1", name="search_docs", input={"query": "x"})
            yield Stop(reason="tool_use", usage=ANSWER_USAGE)
            return
        yield TextDelta(delta="Comparing")
        self.hanging.set()
        await asyncio.Event().wait()


async def cancel_when(ready: asyncio.Event, router: Router, text: str, **kwargs) -> None:
    """Runs a turn until ready is set, then cancels it."""
    task = asyncio.create_task(collect(router, [], text, **kwargs))
    await asyncio.wait_for(ready.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def slow_route_line(model: str, usage: Usage) -> tuple[str, dict]:
    """The route line of a turn the fast model classified slow."""
    return (
        "route",
        {
            "route": "slow",
            "reason": "classified",
            "router_model": "small",
            "router_usage": ROUTER_USAGE.model_dump(),
            "model": model,
            "usage": usage.model_dump(),
        },
    )


@pytest.mark.asyncio
async def test_a_turn_cancelled_after_its_route_still_records_the_route_with_the_usage_so_far():
    fast = ScriptedModel("small", classified('{"route": "slow"}'))
    slow = SearchesThenHangs()
    telemetry = RecordedTelemetry()
    router = Router(fast, slow, telemetry=telemetry)

    await cancel_when(slow.hanging, router, "Compare X and Y.", tools=tools(FakeSearch(RESULTS)))

    assert telemetry.records == [slow_route_line("large", ANSWER_USAGE)]


@pytest.mark.asyncio
async def test_a_turn_cancelled_before_any_fallback_answered_records_the_model_it_was_asking():
    fast = ScriptedModel("small", classified('{"route": "slow"}'))
    slow = ScriptedModel("large", ChatModelError("large: HTTP 429"))
    fallback = HangingModel()
    telemetry = RecordedTelemetry()
    router = Router(fast, slow, slow_fallbacks=[fallback], telemetry=telemetry)

    await cancel_when(fallback.asked, router, "Compare X and Y.")

    assert len(slow.requests) == 1
    assert telemetry.records == [slow_route_line("hanging", Usage())]


@pytest.mark.asyncio
async def test_a_turn_closed_after_its_done_event_records_its_route_once():
    fast = ScriptedModel("small", classified('{"route": "fast"}'), answer("Hello!"))
    telemetry = RecordedTelemetry()
    events = Router(fast, ScriptedModel("large"), telemetry=telemetry).run([], "Hi")
    assert isinstance(events, AsyncGenerator)

    async for event in events:
        if isinstance(event, DoneEvent):
            break
    await events.aclose()

    assert [kind for kind, _ in telemetry.records] == ["route"]


@pytest.mark.asyncio
async def test_a_turn_cancelled_while_classifying_records_no_route():
    fast = HangingModel()
    telemetry = RecordedTelemetry()
    router = Router(fast, ScriptedModel("large"), timeout=60, telemetry=telemetry)

    await cancel_when(fast.asked, router, "Hi")

    assert telemetry.records == []


class Clock:
    """The router's clock, moved by hand: seconds since an arbitrary start."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.mark.asyncio
async def test_a_failed_model_is_asked_after_its_fallback_until_the_cooldown_passes():
    fast = ScriptedModel(
        "small",
        classified('{"route": "slow"}'),
        classified('{"route": "slow"}'),
        classified('{"route": "slow"}'),
    )
    slow = ScriptedModel("large", ChatModelError("large: HTTP 429"), answer("Large again."))
    fallback = ScriptedModel("large-older", answer("First."), answer("Second."))
    clock = Clock()
    router = Router(fast, slow, slow_fallbacks=[fallback], clock=clock)

    first = await collect(router, [], "Compare X and Y.")
    clock.now += 299
    second = await collect(router, [], "Compare X and Y.")
    clock.now += 1  # the default cooldown, 5 minutes, has passed
    third = await collect(router, [], "Compare X and Y.")

    assert len(slow.requests) == 2
    assert len(fallback.requests) == 2
    assert [events[0].model for events in (first, second, third)] == [
        "large-older",
        "large-older",
        "large",
    ]
    assert third[1] == TextEvent(text="Large again.")


@pytest.mark.asyncio
async def test_a_failure_with_a_retry_after_is_remembered_until_it_passes():
    fast = ScriptedModel("small", *[classified('{"route": "slow"}')] * 3)
    slow = ScriptedModel(
        "large", ChatModelError("large: HTTP 429", retry_after=3600), answer("Large again.")
    )
    fallback = ScriptedModel("large-older", answer("First."), answer("Second."))
    clock = Clock()
    router = Router(fast, slow, slow_fallbacks=[fallback], cooldown=60, clock=clock)

    await collect(router, [], "Compare X and Y.")
    clock.now += 3599
    second = await collect(router, [], "Compare X and Y.")
    clock.now += 1
    third = await collect(router, [], "Compare X and Y.")

    assert (second[0].model, third[0].model) == ("large-older", "large")
    assert len(slow.requests) == 2


@pytest.mark.asyncio
async def test_a_remembered_model_is_still_asked_when_its_fallbacks_fail():
    fast = ScriptedModel("small", *[classified('{"route": "slow"}')] * 2)
    slow = ScriptedModel("large", ChatModelError("large: HTTP 429"), answer("Large after all."))
    fallback = ScriptedModel(
        "large-older", answer("First."), ChatModelError("large-older: timed out")
    )
    router = Router(fast, slow, slow_fallbacks=[fallback], clock=Clock())

    await collect(router, [], "Compare X and Y.")
    second = await collect(router, [], "Compare X and Y.")

    assert second[0].model == "large"
    assert second[1] == TextEvent(text="Large after all.")
    assert len(fallback.requests) == 2


@pytest.mark.asyncio
async def test_when_every_model_fails_with_a_remembered_one_last_the_route_names_it():
    fast = ScriptedModel("small", *[classified('{"route": "slow"}')] * 2)
    slow = ScriptedModel(
        "large", ChatModelError("large: HTTP 429"), ChatModelError("large: HTTP 429")
    )
    fallback = ScriptedModel(
        "large-older", answer("First."), ChatModelError("large-older: timed out")
    )
    telemetry = RecordedTelemetry()
    router = Router(fast, slow, slow_fallbacks=[fallback], clock=Clock(), telemetry=telemetry)

    await collect(router, [], "Compare X and Y.")
    second = await collect(router, [], "Compare X and Y.")

    # The remembered model was asked last: the route names it, as a cancel would.
    assert second[0].model == "large"
    assert isinstance(second[-1], ErrorEvent)
    assert telemetry.records[-1] == slow_route_line("large", Usage())


@pytest.mark.asyncio
async def test_a_later_short_failure_does_not_cut_short_an_earlier_retry_after():
    fast = ScriptedModel("small", *[classified('{"route": "slow"}')] * 3)
    slow = ScriptedModel(
        "large",
        ChatModelError("large: HTTP 429", retry_after=86400),
        ChatModelError("large: timed out"),
        answer("Too soon."),
    )
    fallback = ScriptedModel(
        "large-older", answer("First."), ChatModelError("large-older: timed out"), answer("Third.")
    )
    clock = Clock()
    router = Router(fast, slow, slow_fallbacks=[fallback], cooldown=300, clock=clock)

    await collect(router, [], "Compare X and Y.")
    await collect(router, [], "Compare X and Y.")
    clock.now += 301  # the cooldown has passed; the day-long Retry-After has not
    third = await collect(router, [], "Compare X and Y.")

    assert third[0].model == "large-older"
    assert len(slow.requests) == 2


@pytest.mark.asyncio
async def test_a_turn_closed_after_its_route_event_records_its_route():
    fast = ScriptedModel("small", classified('{"route": "slow"}'))
    telemetry = RecordedTelemetry()
    events = Router(fast, ScriptedModel("large", answer("Never.")), telemetry=telemetry).run(
        [], "Compare X and Y."
    )
    assert isinstance(events, AsyncGenerator)

    assert isinstance(await anext(events), RouteEvent)
    await events.aclose()

    assert telemetry.records == [slow_route_line("large", Usage())]


@pytest.mark.asyncio
async def test_a_model_that_failed_after_it_started_answering_is_not_remembered():
    class FailsMidAnswerOnce:
        id = "large"

        def __init__(self) -> None:
            self.requests = 0

        async def stream(self, req):
            self.requests += 1
            yield TextDelta(delta="Half an ")
            if self.requests == 1:
                raise ChatModelError("large: timed out")
            yield Stop(reason="end", usage=ANSWER_USAGE)

    fast = ScriptedModel("small", *[classified('{"route": "slow"}')] * 2)
    slow = FailsMidAnswerOnce()
    fallback = ScriptedModel("large-older", answer("Never asked."))
    router = Router(fast, slow, slow_fallbacks=[fallback], clock=Clock())

    await collect(router, [], "Hello?")
    second = await collect(router, [], "Hello?")

    assert second[0].model == "large"
    assert fallback.requests == []


@pytest.mark.asyncio
async def test_a_fast_model_that_failed_to_classify_is_asked_after_its_fallback_next_turn():
    fast = ScriptedModel("small", ChatModelError("small: HTTP 429"))
    fallback = ScriptedModel("small-older", *[classified('{"route": "slow"}')] * 2)
    slow = ScriptedModel("large", answer("One."), answer("Two."))
    router = Router(fast, slow, fast_fallbacks=[fallback], clock=Clock())

    await collect(router, [], "Hello?")
    second = await collect(router, [], "Hello?")

    assert second[0].router_model == "small-older"
    assert len(fast.requests) == 1


class SearchesThenFails:
    """Calls search_docs in its first step, then raises failure on the next request."""

    id = "large"

    def __init__(self, failure: Exception) -> None:
        self.failure = failure
        self.requests: list[ChatRequest] = []

    async def stream(self, req: ChatRequest):
        self.requests.append(req)
        if len(self.requests) == 1:
            yield ToolCall(id="c1", name="search_docs", input={"query": "x"})
            yield Stop(reason="tool_use", usage=ANSWER_USAGE)
            return
        raise self.failure


@pytest.mark.asyncio
async def test_a_turn_that_ends_in_an_error_after_its_route_records_the_route_with_the_usage_so_far():
    fast = ScriptedModel("small", classified('{"route": "slow"}'))
    slow = SearchesThenFails(ChatModelError("large: HTTP 500"))
    telemetry = RecordedTelemetry()
    router = Router(fast, slow, telemetry=telemetry)

    events = await collect(router, [], "Compare X and Y.", tools=tools(FakeSearch(RESULTS)))

    assert isinstance(events[-1], ErrorEvent)
    assert telemetry.records == [slow_route_line("large", ANSWER_USAGE)]


@pytest.mark.asyncio
async def test_a_turn_whose_path_raises_after_its_route_records_the_route_with_the_usage_so_far():
    fast = ScriptedModel("small", classified('{"route": "slow"}'))
    slow = SearchesThenFails(RuntimeError("a bug"))
    telemetry = RecordedTelemetry()
    router = Router(fast, slow, telemetry=telemetry)

    with pytest.raises(RuntimeError):
        await collect(router, [], "Compare X and Y.", tools=tools(FakeSearch(RESULTS)))

    assert telemetry.records == [slow_route_line("large", ANSWER_USAGE)]


@pytest.mark.asyncio
async def test_a_turn_that_raises_while_classifying_records_no_route():
    fast = ScriptedModel("small", RuntimeError("a bug"))
    telemetry = RecordedTelemetry()
    router = Router(fast, ScriptedModel("large"), telemetry=telemetry)

    with pytest.raises(RuntimeError):
        await collect(router, [], "Hi")

    assert telemetry.records == []

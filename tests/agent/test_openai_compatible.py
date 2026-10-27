"""The OpenAI-compatible adapter, driven through httpx.MockTransport with recorded SSE fixtures.

No test here calls a live API. The Gemini fixtures were recorded from decoded bytes.
"""

import asyncio
import json
import time
from pathlib import Path

import httpx
import pytest

from vessel_mind.agent.models.openai_compatible import OpenAICompatibleModel
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
from vessel_mind.core.credentials import BearerAuth, resolve_credential

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
BASE_URL = "https://models.test/v1"


def sse_response(name: str) -> httpx.Response:
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content=(FIXTURES / name).read_bytes(),
    )


def make_model(handler, **kwargs) -> OpenAICompatibleModel:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    credential = resolve_credential(BearerAuth(key_env="TEST_KEY"), {"TEST_KEY": "test-key"})
    return OpenAICompatibleModel(
        id="gemini-3.5-flash", base_url=BASE_URL, credential=credential, client=client, **kwargs
    )


def question() -> ChatRequest:
    return ChatRequest(
        system="Answer in one short sentence.",
        messages=[UserMessage(text="Why is the sky blue?")],
        max_tokens=2048,
    )


async def collect(model: OpenAICompatibleModel, req: ChatRequest) -> list:
    return [event async for event in model.stream(req)]


@pytest.mark.asyncio
async def test_gemini_text_stream_yields_deltas_then_stop_with_usage():
    model = make_model(lambda request: sse_response("gemini_text.sse"))

    events = await collect(model, question())

    assert events == [
        TextDelta(delta="The sky is blue because"),
        TextDelta(
            delta=" Earth's atmosphere scatters short, blue wavelengths of sunlight"
            " in all directions more than other colors."
        ),
        # Gemini repeats cumulative usage on every chunk; the last one counts.
        # Thinking tokens appear only in total_tokens: 467 - 14 - 25 = 428.
        Stop(reason="end", usage=Usage(input_tokens=14, output_tokens=25, thinking_tokens=428)),
    ]


@pytest.mark.asyncio
async def test_gemini_spending_the_budget_on_thinking_stops_with_max_tokens():
    model = make_model(lambda request: sse_response("gemini_max_tokens.sse"))

    events = await collect(model, question())

    # Recorded with max_tokens 200: Gemini spent all of it thinking and wrote no text.
    assert events == [
        Stop(
            reason="max_tokens",
            usage=Usage(input_tokens=14, output_tokens=0, thinking_tokens=209),
        )
    ]


PARIS_SIGNATURE = (
    "EpADCo0DAWkUfRPoXPZapE8Eclqk9NJ6qKjL7J5ZrpCTBQTuKgFf3rsMInHKB4+HrRm/kz2ICXRok5FIcq0qvapz26X8"
)


def weather_request() -> ChatRequest:
    return ChatRequest(
        system="Use the tools. Call every tool you need in one step.",
        messages=[UserMessage(text="What is the weather in Paris and in Tokyo right now?")],
        tools=[WEATHER],
    )


WEATHER = ToolSpec(
    name="get_weather",
    description="Current weather for one city.",
    parameters={
        "type": "object",
        "properties": {"city": {"type": "string"}},
        "required": ["city"],
    },
)


@pytest.mark.asyncio
async def test_gemini_two_parallel_tool_calls_yield_one_tool_call_each():
    # Recorded live from gemini-3.5-flash. Gemini sends no `index`: each call
    # arrives whole, with its own id, and only the first carries a thought signature.
    model = make_model(lambda request: sse_response("gemini_two_tool_calls.sse"))

    events = await collect(model, weather_request())

    assert [type(e).__name__ for e in events] == ["ToolCall", "ToolCall", "Stop"]
    paris, tokyo, stop = events
    assert (paris.id, paris.name, paris.input) == ("call_2266557", "get_weather", {"city": "Paris"})
    signature = paris.provider_data["extra_content"]["google"]["thought_signature"]
    assert signature.startswith(PARIS_SIGNATURE)
    assert tokyo == ToolCall(id="call_2266560", name="get_weather", input={"city": "Tokyo"})
    # Gemini says finish_reason "stop"; a step that produced calls stops for tool use.
    assert stop == Stop(
        reason="tool_use", usage=Usage(input_tokens=70, output_tokens=32, thinking_tokens=61)
    )


@pytest.mark.asyncio
async def test_interleaved_fragments_keyed_by_index_yield_one_whole_call_each():
    # Hand-written in the OpenAI / llama-server dialect, not recorded: the
    # arguments of two calls arrive in fragments that interleave by `index`.
    model = make_model(lambda request: sse_response("openai_interleaved_tool_calls.sse"))

    events = await collect(model, weather_request())

    assert events == [
        TextDelta(delta="Checking both."),
        ToolCall(id="call_a", name="get_weather", input={"city": "Paris"}),
        ToolCall(id="call_b", name="get_weather", input={"city": "Tokyo"}),
        Stop(reason="tool_use", usage=Usage(input_tokens=80, output_tokens=40)),
    ]


@pytest.mark.asyncio
async def test_a_call_keyed_by_index_in_one_chunk_yields_one_call():
    # Hand-written in the OpenAI / llama-server dialect, not recorded.
    model = make_model(lambda request: sse_response("openai_single_chunk_tool_call.sse"))

    events = await collect(model, weather_request())

    assert events == [
        ToolCall(id="call_c", name="get_weather", input={"city": "Oslo"}),
        Stop(reason="tool_use", usage=Usage(input_tokens=60, output_tokens=12)),
    ]


def sse_lines(*chunks: dict) -> httpx.Response:
    body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)


@pytest.mark.asyncio
async def test_arguments_that_are_not_a_json_object_fail_the_step():
    fragment = {"index": 0, "id": "call_x", "function": {"name": "get_weather", "arguments": "{"}}
    model = make_model(
        lambda request: sse_lines({"choices": [{"delta": {"tool_calls": [fragment]}}]})
    )

    with pytest.raises(ChatModelError, match="get_weather"):
        await collect(model, weather_request())


@pytest.mark.asyncio
async def test_the_second_step_sends_tools_calls_with_provider_data_and_results():
    # Step 1's calls go back unchanged, signature included, as the author's live
    # check did when Gemini accepted step 2 (fixture gemini_after_tool_results.sse).
    step1 = await collect(
        make_model(lambda request: sse_response("gemini_two_tool_calls.sse")), weather_request()
    )
    paris, tokyo = step1[0], step1[1]
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return sse_response("gemini_after_tool_results.sse")

    req = weather_request().model_copy(
        update={
            "messages": [
                *weather_request().messages,
                AssistantMessage(text="", tool_calls=[paris, tokyo]),
                ToolMessage(tool_call_id="call_2266557", content='{"city": "Paris", "temp_c": 18}'),
                ToolMessage(tool_call_id="call_2266560", content='{"city": "Tokyo", "temp_c": 18}'),
            ]
        }
    )
    events = await collect(make_model(handler), req)

    body = json.loads(sent[0].content)
    assert body["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Current weather for one city.",
                "parameters": WEATHER.parameters,
            },
        }
    ]
    assert body["messages"][2:] == [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_2266557",
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": '{"city": "Paris"}'},
                    "extra_content": paris.provider_data["extra_content"],
                },
                {
                    "id": "call_2266560",
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": '{"city": "Tokyo"}'},
                },
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_2266557",
            "content": '{"city": "Paris", "temp_c": 18}',
        },
        {
            "role": "tool",
            "tool_call_id": "call_2266560",
            "content": '{"city": "Tokyo", "temp_c": 18}',
        },
    ]
    assert "".join(e.delta for e in events if isinstance(e, TextDelta)) == (
        "The current weather in both Paris and Tokyo is 18°C."
    )
    assert events[-1] == Stop(
        reason="end", usage=Usage(input_tokens=218, output_tokens=15, thinking_tokens=41)
    )


@pytest.mark.asyncio
async def test_request_translates_the_seam_types_to_chat_completions():
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return sse_response("gemini_text.sse")

    req = ChatRequest(
        system="Be brief.",
        messages=[
            UserMessage(text="Hi"),
            AssistantMessage(text="Hello."),
            UserMessage(text="Why is the sky blue?"),
        ],
        max_tokens=64,
    )
    await collect(make_model(handler), req)

    assert sent[0].url == f"{BASE_URL}/chat/completions"
    assert sent[0].headers["authorization"] == "Bearer test-key"
    assert json.loads(sent[0].content) == {
        "model": "gemini-3.5-flash",
        "messages": [
            {"role": "system", "content": "Be brief."},
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "Hello."},
            {"role": "user", "content": "Why is the sky blue?"},
        ],
        "stream": True,
        "stream_options": {"include_usage": True},
        "max_tokens": 64,
    }


@pytest.mark.asyncio
async def test_a_reasoning_effort_is_sent_only_when_set():
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return sse_response("gemini_text.sse")

    await collect(make_model(handler, reasoning_effort="none"), question())
    await collect(make_model(handler), question())

    assert json.loads(sent[0].content)["reasoning_effort"] == "none"
    assert "reasoning_effort" not in json.loads(sent[1].content)


class HangingStream(httpx.AsyncByteStream):
    """Sends the fixture's first event, then waits forever, as a slow model would."""

    def __init__(self, first_event: bytes) -> None:
        self.first_event = first_event
        self.closed = False

    async def __aiter__(self):
        yield self.first_event
        await asyncio.Event().wait()

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_cancelling_the_consumer_ends_the_stream_fast_and_closes_the_response():
    first_event = (FIXTURES / "gemini_text.sse").read_bytes().split(b"\n\n")[0] + b"\n\n"
    body = HangingStream(first_event)
    model = make_model(
        lambda request: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=body
        )
    )
    received = asyncio.Event()

    async def consume() -> None:
        async for _ in model.stream(question()):
            received.set()

    task = asyncio.create_task(consume())
    await asyncio.wait_for(received.wait(), timeout=1)

    started = time.perf_counter()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    elapsed = time.perf_counter() - started

    assert elapsed < 0.1
    assert body.closed


class Sleeps:
    """Records the backoff delays instead of waiting them out."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


def replies(*responses: httpx.Response):
    queue = list(responses)
    return lambda request: queue.pop(0)


@pytest.mark.asyncio
async def test_http_429_is_retried_honouring_retry_after():
    sleeps = Sleeps()
    model = make_model(
        replies(
            httpx.Response(429, headers={"retry-after": "3"}),
            sse_response("gemini_text.sse"),
        ),
        sleep=sleeps,
    )

    events = await collect(model, question())

    assert sleeps.delays == [3.0]
    assert events[-1] == Stop(
        reason="end", usage=Usage(input_tokens=14, output_tokens=25, thinking_tokens=428)
    )


@pytest.mark.asyncio
async def test_http_429_without_retry_after_backs_off_exponentially_then_raises():
    sleeps = Sleeps()
    model = make_model(
        lambda request: httpx.Response(429),
        sleep=sleeps,
        max_retries=3,
        backoff_base=0.5,
    )

    with pytest.raises(ChatModelError, match="429"):
        await collect(model, question())

    assert sleeps.delays == [0.5, 1.0, 2.0]


@pytest.mark.asyncio
async def test_retry_after_longer_than_the_cap_raises_instead_of_waiting():
    # Gemini's daily free-tier limit answers 429 with a delay of about a day.
    sleeps = Sleeps()
    model = make_model(
        lambda request: httpx.Response(429, headers={"retry-after": "80422"}),
        sleep=sleeps,
        backoff_max=30.0,
    )

    with pytest.raises(ChatModelError, match="429"):
        await collect(model, question())

    assert sleeps.delays == []


@pytest.mark.asyncio
async def test_http_503_is_retried_like_429():
    sleeps = Sleeps()
    model = make_model(
        replies(httpx.Response(503), sse_response("gemini_text.sse")),
        sleep=sleeps,
    )

    events = await collect(model, question())

    assert sleeps.delays == [1.0]
    assert isinstance(events[-1], Stop)


@pytest.mark.asyncio
async def test_a_client_error_raises_at_once_without_retrying():
    sleeps = Sleeps()
    model = make_model(lambda request: httpx.Response(400), sleep=sleeps)

    with pytest.raises(ChatModelError, match="400"):
        await collect(model, question())

    assert sleeps.delays == []


def raises_then(error: Exception, *responses: httpx.Response):
    """A transport that raises error on the first request, then sends responses."""
    queue: list[Exception | httpx.Response] = [error, *responses]

    def handler(request: httpx.Request) -> httpx.Response:
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    return handler


@pytest.mark.asyncio
async def test_a_first_byte_timeout_is_retried_like_429():
    sleeps = Sleeps()
    model = make_model(
        raises_then(httpx.ReadTimeout("no first byte"), sse_response("gemini_text.sse")),
        sleep=sleeps,
    )

    events = await collect(model, question())

    assert sleeps.delays == [1.0]
    assert isinstance(events[-1], Stop)


@pytest.mark.asyncio
async def test_a_connection_error_is_retried_like_429():
    sleeps = Sleeps()
    model = make_model(
        raises_then(httpx.ConnectError("refused"), sse_response("gemini_text.sse")),
        sleep=sleeps,
    )

    events = await collect(model, question())

    assert sleeps.delays == [1.0]
    assert isinstance(events[-1], Stop)


@pytest.mark.asyncio
async def test_timeouts_that_outlast_the_retries_fail_the_step_as_a_model_error():
    sleeps = Sleeps()

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("no first byte")

    model = make_model(handler, sleep=sleeps, max_retries=2)

    with pytest.raises(ChatModelError, match="timed out"):
        await collect(model, question())

    assert sleeps.delays == [1.0, 2.0]


class BrokenStream(httpx.AsyncByteStream):
    """Sends the fixture's first event, then the connection times out."""

    def __init__(self, first_event: bytes) -> None:
        self.first_event = first_event

    async def __aiter__(self):
        yield self.first_event
        raise httpx.ReadTimeout("stalled")


@pytest.mark.asyncio
async def test_a_timeout_after_the_first_event_fails_the_step_without_a_retry():
    # Text has already reached the client: asking again would repeat it.
    sleeps = Sleeps()
    first_event = (FIXTURES / "gemini_text.sse").read_bytes().split(b"\n\n")[0] + b"\n\n"
    model = make_model(
        lambda request: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=BrokenStream(first_event)
        ),
        sleep=sleeps,
    )
    events: list = []

    with pytest.raises(ChatModelError, match="timed out"):
        async for event in model.stream(question()):
            events.append(event)

    assert sleeps.delays == []
    assert events == [TextDelta(delta="The sky is blue because")]

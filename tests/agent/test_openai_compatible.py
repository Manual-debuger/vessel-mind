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
    Usage,
    UserMessage,
)

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
    return OpenAICompatibleModel(
        id="gemini-3.5-flash", base_url=BASE_URL, api_key="test-key", client=client, **kwargs
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

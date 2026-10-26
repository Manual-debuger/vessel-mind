"""The eval spaces its requests to stay inside free-tier limits (spec §5.1, §5.8)."""

import pytest

from vessel_mind.core.chat import ChatRequest, Stop, TextDelta, Usage, UserMessage
from vessel_mind.eval.pacing import PacedModel, Pacer


class FakeTime:
    def __init__(self) -> None:
        self.now = 100.0
        self.slept: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class Echo:
    id = "echo"

    async def stream(self, req: ChatRequest):
        yield TextDelta(delta="hi")
        yield Stop(reason="end", usage=Usage())


REQUEST = ChatRequest(messages=[UserMessage(text="x")])


@pytest.mark.asyncio
async def test_request_starts_are_at_least_the_interval_apart_and_counted():
    time = FakeTime()
    pacer = Pacer(6.0, clock=time.clock, sleep=time.sleep)
    model = PacedModel(Echo(), pacer)

    for _ in range(2):
        assert [e async for e in model.stream(REQUEST)][0] == TextDelta(delta="hi")
    time.now += 10.0  # a long pause needs no wait
    _ = [e async for e in model.stream(REQUEST)]

    assert time.slept == [6.0]
    assert pacer.requests == 3
    assert pacer.waited == pytest.approx(6.0)
    assert model.id == "echo"


@pytest.mark.asyncio
async def test_ready_waits_for_the_next_slot_without_taking_it():
    time = FakeTime()
    pacer = Pacer(6.0, clock=time.clock, sleep=time.sleep)
    model = PacedModel(Echo(), pacer)
    _ = [e async for e in model.stream(REQUEST)]
    time.now += 2.0

    await pacer.ready()
    _ = [e async for e in model.stream(REQUEST)]

    # ready() waited the 4 s left; the request after it starts at once.
    assert time.slept == [4.0]

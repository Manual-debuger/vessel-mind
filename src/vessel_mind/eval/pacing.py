"""Spacing of the eval's model requests, to stay inside the free tier's limits.

Every request of a run (both paths and the judge) takes a slot from one Pacer,
and two request starts are at least ``interval`` seconds apart. The time a turn
spends waiting for a slot is counted in ``waited``, so the eval can leave it
out of the turn's latency.
"""

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable

from vessel_mind.core.chat import ChatEvent, ChatModel, ChatRequest


class Pacer:
    def __init__(
        self,
        interval: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.interval = interval
        self._clock = clock
        self._sleep = sleep
        self._last: float | None = None
        self.requests = 0  # slots taken
        self.waited = 0.0  # seconds spent waiting for a slot

    async def ready(self) -> None:
        """Waits until a request may start, without taking the slot."""
        if self._last is None:
            return
        wait = self._last + self.interval - self._clock()
        if wait > 0:
            self.waited += wait
            await self._sleep(wait)

    async def slot(self) -> None:
        """Waits until a request may start, and takes the slot."""
        await self.ready()
        self._last = self._clock()
        self.requests += 1


class PacedModel:
    """A ChatModel whose every request takes a slot from the pacer first."""

    def __init__(self, model: ChatModel, pacer: Pacer) -> None:
        self.id = model.id
        self._model = model
        self._pacer = pacer

    async def stream(self, req: ChatRequest) -> AsyncIterator[ChatEvent]:
        await self._pacer.slot()
        async for event in self._model.stream(req):
            yield event

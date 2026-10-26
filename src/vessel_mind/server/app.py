"""The FastAPI app: the event protocol, and the operator console that uses only it."""

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field

from vessel_mind.agent.loop import TurnTally, run_turn
from vessel_mind.core.chat import ChatModel
from vessel_mind.core.events import AgentEvent, DoneEvent, ErrorEvent

log = logging.getLogger(__name__)

_HERE = Path(__file__).resolve().parent
_templates = Jinja2Templates(directory=_HERE / "templates")


class StartTurn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=8000)


class TurnStarted(BaseModel):
    turn_id: str


class Turn:
    """One running or finished turn: its events so far, which any reader can replay."""

    def __init__(self) -> None:
        self.events: list[AgentEvent] = []
        self.finished = False
        self.task: asyncio.Task[None] | None = None
        self._changed = asyncio.Condition()

    async def publish(self, event: AgentEvent) -> None:
        async with self._changed:
            self.events.append(event)
            self.finished = isinstance(event, DoneEvent | ErrorEvent)
            self._changed.notify_all()

    async def follow(self) -> AsyncIterator[AgentEvent]:
        """Every event from the first, then each new one, until the turn ends."""
        sent = 0
        while True:
            async with self._changed:
                while len(self.events) <= sent:
                    await self._changed.wait()
                new = self.events[sent:]
            for event in new:
                yield event
            sent += len(new)
            if isinstance(new[-1], DoneEvent | ErrorEvent):
                return


def _sse(event: AgentEvent) -> str:
    return f"event: {event.type}\ndata: {event.model_dump_json()}\n\n"


def create_app(
    model: ChatModel,
    *,
    system: str | None = None,
    max_tokens: int | None = None,
) -> FastAPI:
    turns: dict[str, Turn] = {}

    async def drive(turn: Turn, text: str) -> None:
        tally = TurnTally()
        try:
            async for event in run_turn(
                model, [], text, system=system, max_tokens=max_tokens, tally=tally
            ):
                await turn.publish(event)
        except asyncio.CancelledError:
            await turn.publish(DoneEvent(reason="cancelled", usage=tally.usage, steps=tally.steps))
            raise
        except Exception:
            log.exception("turn failed")
            await turn.publish(ErrorEvent(code="internal_error", message="The turn failed."))

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        running = [t.task for t in turns.values() if t.task and not t.task.done()]
        for task in running:
            task.cancel()
        await asyncio.gather(*running, return_exceptions=True)

    app = FastAPI(title="vessel-mind", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=_HERE / "static"), name="static")

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def console(request: Request) -> HTMLResponse:
        return _templates.TemplateResponse(request, "console.html", {"model_id": model.id})

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "model": model.id}

    @app.post("/turns")
    async def start_turn(body: StartTurn) -> TurnStarted:
        turn_id = uuid.uuid4().hex
        turn = Turn()
        turns[turn_id] = turn
        turn.task = asyncio.create_task(drive(turn, body.text))
        return TurnStarted(turn_id=turn_id)

    def find(turn_id: str) -> Turn:
        turn = turns.get(turn_id)
        if turn is None:
            raise HTTPException(status_code=404, detail="unknown turn")
        return turn

    @app.get("/turns/{turn_id}/events")
    async def turn_events(turn_id: str) -> StreamingResponse:
        turn = find(turn_id)

        async def body() -> AsyncIterator[str]:
            async for event in turn.follow():
                yield _sse(event)

        return StreamingResponse(
            body(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/turns/{turn_id}/cancel", status_code=202)
    async def cancel_turn(turn_id: str) -> dict[str, str]:
        turn = find(turn_id)
        if turn.task is not None and not turn.task.done():
            turn.task.cancel()
        return {"turn_id": turn_id}

    return app

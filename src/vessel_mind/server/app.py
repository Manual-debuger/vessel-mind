"""The FastAPI app: the event protocol, and the operator console that uses only it."""

import asyncio
import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field

from vessel_mind.agent.loop import TurnTally, run_turn
from vessel_mind.agent.router import Router
from vessel_mind.core.capabilities import CapabilityRegistry
from vessel_mind.core.chat import ChatMessage, ChatModel
from vessel_mind.core.events import AgentEvent, DoneEvent, ErrorEvent
from vessel_mind.core.telemetry import NoTelemetry, Telemetry
from vessel_mind.server.proposals import TurnProposals, proposal_routes
from vessel_mind.server.state import (
    Clock,
    DailyTurnLimitReached,
    SessionStore,
    TurnLimitReached,
    TurnStatus,
)

log = logging.getLogger(__name__)

_HERE = Path(__file__).resolve().parent
_templates = Jinja2Templates(directory=_HERE / "templates")


class StartTurn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # None starts a new session.
    session_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    text: str = Field(min_length=1, max_length=8000)


class TurnStarted(BaseModel):
    session_id: str
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

    async def follow(self, start: int = 0) -> AsyncIterator[tuple[int, AgentEvent]]:
        """Every event from position start, then each new one, until the turn ends,
        each with its position."""
        sent = max(start, 0)
        while True:
            async with self._changed:
                while len(self.events) <= sent:
                    if self.finished:
                        return
                    await self._changed.wait()
                new = self.events[sent:]
            for position, event in enumerate(new, start=sent):
                yield position, event
            sent += len(new)
            if isinstance(new[-1], DoneEvent | ErrorEvent):
                return


def _sse(position: int, event: AgentEvent) -> str:
    # The id is the event's position in its turn: a reconnecting EventSource sends
    # it back as Last-Event-ID, and the stream resumes after it.
    return f"id: {position}\nevent: {event.type}\ndata: {event.model_dump_json()}\n\n"


def _resume_from(last_event_id: str | None) -> int:
    """The position after Last-Event-ID; 0 without one, or when it is not ours."""
    if last_event_id is None or not last_event_id.isdigit():
        return 0
    return int(last_event_id) + 1


def create_app(
    model: ChatModel,
    *,
    router: Router | None = None,
    tools: CapabilityRegistry | None = None,
    system: str | None = None,
    max_tokens: int | None = None,
    state_path: Path | str = ":memory:",
    max_turns_per_session: int | None = None,
    max_turns_per_day: int | None = None,
    demo_mode: bool = False,
    public_host: bool = False,
    demo_page_url: str | None = None,
    example_questions: Sequence[str] = (),
    clock: Clock | None = None,
    services: Sequence[AbstractAsyncContextManager[object]] = (),
    telemetry: Telemetry | None = None,
) -> FastAPI:
    """The app over one model. tools is the capability registry the loop may call;
    None offers no tools. state_path is the SQLite state file, opened at startup.

    router, when given, routes every turn between its fast and slow models, and each
    turn's stream starts with a route event; model is then its slow model, which
    /health names. Without a router, model answers every turn and no route event is
    sent.

    services are entered in order at startup, after the state file opens, and
    exited in reverse at shutdown: for example the retrieval index its tools read,
    or an MCP server, which registers its tools in the registry when it starts.

    max_turns_per_session bounds the turns one session may start, and
    max_turns_per_day the turns all sessions together may start in one UTC day,
    to protect the provider's free quota; None means no bound.

    demo_mode (ADR 0002) still makes and shows proposals but refuses every
    approval. public_host shows the free-tier notice: the app is served on the
    public hostname (spec §5.4). demo_page_url links the published page the agent
    reads, and example_questions fill the question box when clicked. clock gives the current time for proposal expiry;
    tests fake it.

    telemetry receives a summary of each turn when it ends: its status, steps and
    usage (and, from the router, its route decision).
    """
    sink: Telemetry = telemetry if telemetry is not None else NoTelemetry()
    registry = tools if tools is not None else CapabilityRegistry()
    # Turns in this process, for streaming. The state file outlives them.
    turns: dict[str, Turn] = {}
    # The latest turn of each session, and a lock so a session starts one turn at a time.
    latest: dict[str, Turn] = {}
    session_locks: dict[str, asyncio.Lock] = {}
    stores: list[SessionStore] = []

    def store() -> SessionStore:
        return stores[0]

    async def drive(
        turn: Turn, turn_id: str, session_id: str, history: list[ChatMessage], text: str
    ) -> None:
        tally = TurnTally()
        finished = False

        def finish(status: TurnStatus, messages: list[ChatMessage] | None = None) -> None:
            nonlocal finished
            if not finished:
                finished = True
                store().finish_turn(turn_id, status, messages or [])
                sink.record(
                    "turn",
                    {
                        "turn_id": turn_id,
                        "session_id": session_id,
                        "status": status,
                        "steps": tally.steps,
                        "usage": tally.usage.model_dump(),
                    },
                )

        options: dict[str, Any] = {
            "tools": registry,
            "proposals": TurnProposals(store(), turn_id),
            "system": system,
            "max_tokens": max_tokens,
            "tally": tally,
        }
        events = (
            router.run(history, text, **options)
            if router is not None
            else run_turn(model, history, text, **options)
        )
        try:
            async for event in events:
                # History is saved before the client is told the turn ended, so a
                # client that starts the next turn on `done` always sees it.
                if isinstance(event, DoneEvent):
                    finish("done", tally.messages)
                elif isinstance(event, ErrorEvent):
                    finish("error")
                await turn.publish(event)
        except asyncio.CancelledError:
            # A cancelled turn leaves no history: its messages may be half a step.
            finish("cancelled")
            await turn.publish(DoneEvent(reason="cancelled", usage=tally.usage, steps=tally.steps))
            raise
        except Exception:
            log.exception("turn failed")
            finish("error")
            await turn.publish(ErrorEvent(code="internal_error", message="The turn failed."))

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        stores.append(SessionStore.open(state_path, clock=clock))
        try:
            async with AsyncExitStack() as stack:
                for service in services:
                    await stack.enter_async_context(service)
                yield
                # Turns end before the services their tools use close.
                running = [t.task for t in turns.values() if t.task and not t.task.done()]
                for task in running:
                    task.cancel()
                await asyncio.gather(*running, return_exceptions=True)
        finally:
            stores.pop().close()

    app = FastAPI(title="vessel-mind", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=_HERE / "static"), name="static")

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def console(request: Request) -> HTMLResponse:
        return _templates.TemplateResponse(
            request,
            "console.html",
            {
                "model_id": model.id,
                "fast_model_id": router.fast.id if router is not None else None,
                "demo_mode": demo_mode,
                "public_host": public_host,
                "demo_page_url": demo_page_url,
                "max_turns_per_session": max_turns_per_session,
                "max_turns_per_day": max_turns_per_day,
                "example_questions": list(example_questions),
            },
        )

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "model": model.id}

    def daily_limit_reached() -> HTTPException:
        return HTTPException(
            status_code=429,
            detail={
                "code": "daily_turn_limit",
                "message": f"This server has answered all {max_turns_per_day} of its turns "
                "for today (UTC). Try again tomorrow.",
            },
        )

    @app.post("/turns")
    async def start_turn(body: StartTurn) -> TurnStarted:
        # Checked before a session is made, so a refused turn leaves none behind.
        if max_turns_per_day is not None and store().turns_today() >= max_turns_per_day:
            raise daily_limit_reached()
        if body.session_id is None:
            session_id = store().create_session()
        elif store().has_session(body.session_id):
            session_id = body.session_id
        else:
            raise HTTPException(status_code=404, detail="unknown session")

        async with session_locks.setdefault(session_id, asyncio.Lock()):
            previous = latest.get(session_id)
            if previous is not None and previous.task is not None:
                # A new turn cancels the running one, and waits until it has ended,
                # so the new turn starts from the history the old one left.
                previous.task.cancel()
                await asyncio.wait([previous.task])
            try:
                turn_id = store().begin_turn(
                    session_id,
                    max_turns=max_turns_per_session,
                    max_turns_per_day=max_turns_per_day,
                )
            except DailyTurnLimitReached:
                raise daily_limit_reached() from None
            except TurnLimitReached:
                raise HTTPException(
                    status_code=429,
                    detail={
                        "code": "turn_limit",
                        "message": f"This session has used all {max_turns_per_session} "
                        "of its turns. Start a new session.",
                    },
                ) from None
            turn = Turn()
            turns[turn_id] = turn
            latest[session_id] = turn
            history = store().history(session_id)
            turn.task = asyncio.create_task(drive(turn, turn_id, session_id, history, body.text))
        return TurnStarted(session_id=session_id, turn_id=turn_id)

    def find(turn_id: str) -> Turn:
        turn = turns.get(turn_id)
        if turn is None:
            raise HTTPException(status_code=404, detail="unknown turn")
        return turn

    @app.get("/turns/{turn_id}/events")
    async def turn_events(
        turn_id: str, last_event_id: Annotated[str | None, Header()] = None
    ) -> StreamingResponse:
        turn = find(turn_id)
        start = _resume_from(last_event_id)

        async def body() -> AsyncIterator[str]:
            async for position, event in turn.follow(start):
                yield _sse(position, event)

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

    app.include_router(proposal_routes(store, registry, demo_mode=demo_mode))
    return app

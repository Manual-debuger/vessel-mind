"""The router (ADR 0001 §3, spec §5.7): picks the fast path or the slow path per turn.

The fast model classifies the turn as {"route": "fast" | "slow"}. On the fast
path the router retrieves first, with the registry's search tool, and the fast
model answers from that context with no tools; the search is kept in the turn's
messages as a search_docs call and its result, as the slow path keeps one. On
the slow path the slow model runs the full tool loop.
"""

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Callable, Sequence

from pydantic import BaseModel, ConfigDict, ValidationError

from vessel_mind.agent.loop import TurnTally, run_tool, run_turn
from vessel_mind.agent.proposals import Proposals
from vessel_mind.core.capabilities import CapabilityRegistry
from vessel_mind.core.chat import (
    AssistantMessage,
    ChatEvent,
    ChatMessage,
    ChatModel,
    ChatModelError,
    ChatRequest,
    ToolCall,
    ToolMessage,
    Usage,
    UserMessage,
    read_reply,
    unfenced,
)
from vessel_mind.core.events import (
    AgentEvent,
    DoneEvent,
    ErrorEvent,
    Route,
    RouteEvent,
    RouteReason,
    ToolResultEvent,
    ToolStartEvent,
)
from vessel_mind.core.telemetry import NoTelemetry, Telemetry

log = logging.getLogger(__name__)

# The tool the fast path retrieves with, and the call id of its tool_start and
# tool_result events, so a client maps [n] the same way on both paths.
SEARCH_TOOL = "search_docs"
FAST_PATH_CALL_ID = "fast-path-search"

ROUTER_SYSTEM = """\
You route a user's message to one of two paths. Reply with JSON only, no other text:
{"route": "fast"} or {"route": "slow"}.

fast: one document search answers the message, or it needs no knowledge at all
(a greeting, a thank-you, a rephrasing of the previous answer).
slow: the message needs a tool other than one search, more than one search (for
example a comparison of two topics), or reasoning beyond the retrieved text
(planning, calculation, code, a multi-step argument).
If you are not sure, choose slow."""

FAST_CONTEXT = """\
Sections from search_docs for the user's latest message, numbered as the search
numbered them. Answer only from them. Cite a section by its number in square
brackets, for example [1] or [2][3], after the sentence it supports. If no
section answers the message, say that the documents do not cover it.

"""


class RouteDecision(BaseModel):
    """What the classification must be."""

    model_config = ConfigDict(extra="forbid", strict=True)

    route: Route


class _Failures:
    """The models that failed before answering, remembered across turns (spec §5.1).

    A model is remembered from its failure until the wait its failure asked for
    (a 429's Retry-After) has passed, or, when it asked for none, for the
    cooldown, in seconds. Until then each turn asks it after the rest of its
    path, not before. A later failure never shortens the wait of an earlier one.
    """

    def __init__(self, cooldown: float, clock: Callable[[], float]) -> None:
        self._cooldown = cooldown
        self._clock = clock
        self._until: dict[str, float] = {}

    def remember(self, model: ChatModel, error: ChatModelError) -> None:
        wait = self._cooldown if error.retry_after is None else error.retry_after
        until = self._clock() + wait
        self._until[model.id] = max(self._until.get(model.id, until), until)

    def failed_last(self, models: Sequence[ChatModel]) -> tuple[ChatModel, ...]:
        """The models with the remembered ones moved to the end, each group in its order."""
        now = self._clock()
        failed = [model for model in models if now < self._until.get(model.id, now)]
        return (*(model for model in models if model not in failed), *failed)


class _FirstThatAnswers:
    """One path's models, in order, as one ChatModel for one turn.

    A model that fails before its first event (its adapter has spent its retries)
    hands the request to the next one. A model that fails after it started
    answering is not replaced: the client has already seen part of its answer.
    Once a model has answered, the turn's later steps start from it: the models
    before it, which failed, are not asked again in this turn.
    asking is the id of the model asked now, or asked last; answered is the id of
    the model that answered last, or None. failures remembers the models that
    failed before their first event, for later turns.
    """

    def __init__(self, models: Sequence[ChatModel], failures: _Failures) -> None:
        self.models = models
        self.failures = failures
        self.id = models[0].id
        self._asking = 0
        self._answered: int | None = None

    @property
    def asking(self) -> str:
        return self.models[self._asking].id

    @property
    def answered(self) -> str | None:
        return None if self._answered is None else self.models[self._answered].id

    def from_answered(self) -> "_FirstThatAnswers":
        """The models from the one that answered last on, for another use in this turn."""
        return _FirstThatAnswers(self.models[self._answered or 0 :], self.failures)

    async def stream(self, req: ChatRequest) -> AsyncIterator[ChatEvent]:
        for number in range(self._answered or 0, len(self.models)):
            self._asking = number
            model = self.models[number]
            started = False
            try:
                async for event in model.stream(req):
                    if not started:
                        started = True
                        self._answered = number
                    yield event
            except ChatModelError as error:
                if not started:
                    self.failures.remember(model, error)
                if started or number == len(self.models) - 1:
                    raise
                log.warning("%s failed, trying %s: %s", model.id, self.models[number + 1].id, error)
                continue
            self._answered = number
            return


class Router:
    """Routes each turn between two models: one configuration's fast and slow paths.

    enabled=False sends every turn to the slow path without asking the fast model:
    the eval's slow-only baseline. timeout bounds the classification in seconds.

    fast_fallbacks and slow_fallbacks are each path's fallback list, in order
    (spec §5.1): when a path's model fails after its retries, the next one is
    asked, and the route event names the model that answered.

    A model that fails before answering is asked after the rest of its path in
    later turns too, until the Retry-After its failure carried has passed, or,
    without one, cooldown seconds on clock (spec §5.1).

    telemetry receives each turn's route decision, with both models' usage, when
    the turn ends: done, in an error, cancelled, or raising. A turn that ends
    while it is classified has no route and records none.
    """

    def __init__(
        self,
        fast: ChatModel,
        slow: ChatModel,
        *,
        enabled: bool = True,
        timeout: float = 10.0,
        max_tokens: int | None = None,
        fast_fallbacks: Sequence[ChatModel] = (),
        slow_fallbacks: Sequence[ChatModel] = (),
        cooldown: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
        telemetry: Telemetry | None = None,
    ) -> None:
        self.telemetry: Telemetry = telemetry if telemetry is not None else NoTelemetry()
        self.fast = fast
        self.slow = slow
        self.fast_fallbacks = tuple(fast_fallbacks)
        self.slow_fallbacks = tuple(slow_fallbacks)
        self.enabled = enabled
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.cooldown = cooldown
        # Per router: shared by the sessions of its configuration.
        self._failures = _Failures(cooldown, clock)

    async def run(
        self,
        history: Sequence[ChatMessage],
        user_text: str,
        *,
        tools: CapabilityRegistry | None = None,
        proposals: Proposals | None = None,
        system: str | None = None,
        max_tokens: int | None = None,
        tally: TurnTally | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """Yields a route event, then the turn's events on that path, ending done or error.

        tally counts only the answering model's steps; the route event carries the
        classification's usage.
        """
        tally = tally if tally is not None else TurnTally()
        registry = tools if tools is not None else CapabilityRegistry()
        fast = self._path(self.fast, self.fast_fallbacks)
        route, reason, usage, router_model = await self._decide_route(fast, history, user_text)
        if route == "fast":
            # The fast path's answer starts from the model that classified: the
            # ones before it failed in this turn.
            model = fast.from_answered()
        else:
            model = self._path(self.slow, self.slow_fallbacks)
        # With a fallback list, which model answers is known only once one has
        # started: the turn's events wait until then, so the route event, which
        # names that model, still comes first.
        waiting = len(model.models) > 1
        held: list[AgentEvent] = []
        decision: RouteEvent | None = None

        def decide(answering: str) -> RouteEvent:
            return RouteEvent(
                route=route,
                reason=reason,
                router_model=router_model,
                router_usage=usage,
                model=answering,
            )

        recorded = False
        try:
            if not waiting:
                decision = decide(model.id)
                yield decision
            async for event in self._answer(
                route,
                model,
                history,
                user_text,
                registry=registry,
                proposals=proposals,
                system=system,
                max_tokens=max_tokens,
                tally=tally,
            ):
                if decision is None:
                    if model.answered is None and not isinstance(event, DoneEvent | ErrorEvent):
                        held.append(event)
                        continue
                    # When every model failed, the route names the one it asked last,
                    # as a cancel does.
                    decision = decide(model.answered or model.asking)
                    yield decision
                    for earlier in held:
                        yield earlier
                if isinstance(event, DoneEvent | ErrorEvent):
                    # Before the event goes out: the caller writes its turn line on it.
                    self._record_route(decision, tally.usage)
                    recorded = True
                yield event
        finally:
            # A turn that is cancelled, or whose path raises, ends without a done or
            # error event (the caller publishes one): its route is recorded here, with
            # the usage spent until then. Before any model answered, the route names
            # the model it was asking.
            if not recorded:
                self._record_route(decision or decide(model.asking), tally.usage)

    def _path(self, first: ChatModel, fallbacks: Sequence[ChatModel]) -> _FirstThatAnswers:
        """One path's models for this turn, the recently failed ones last."""
        return _FirstThatAnswers(self._failures.failed_last((first, *fallbacks)), self._failures)

    def _record_route(self, routed: RouteEvent, answer_usage: Usage) -> None:
        """The turn's route line: its route event and the answering model's usage."""
        self.telemetry.record(
            "route",
            {
                "route": routed.route,
                "reason": routed.reason,
                "router_model": routed.router_model,
                "router_usage": routed.router_usage.model_dump(),
                "model": routed.model,
                "usage": answer_usage.model_dump(),
            },
        )

    async def _answer(
        self,
        route: Route,
        model: ChatModel,
        history: Sequence[ChatMessage],
        user_text: str,
        *,
        registry: CapabilityRegistry,
        proposals: Proposals | None,
        system: str | None,
        max_tokens: int | None,
        tally: TurnTally,
    ) -> AsyncIterator[AgentEvent]:
        """The turn's events on its path, after the route event."""
        if route == "fast":
            context = None
            # The search, as the slow path would have stored it.
            searched: list[ChatMessage] = []
            if registry.get(SEARCH_TOOL) is not None:
                query = {"query": user_text}
                yield ToolStartEvent(call_id=FAST_PATH_CALL_ID, name=SEARCH_TOOL, input=query)
                result, is_error = await run_tool(registry, SEARCH_TOOL, query)
                yield ToolResultEvent(
                    call_id=FAST_PATH_CALL_ID, name=SEARCH_TOOL, result=result, is_error=is_error
                )
                context = FAST_CONTEXT + json.dumps(result, ensure_ascii=False, indent=1)
                call = ToolCall(id=FAST_PATH_CALL_ID, name=SEARCH_TOOL, input=query)
                searched = [
                    AssistantMessage(text="", tool_calls=[call]),
                    ToolMessage(tool_call_id=FAST_PATH_CALL_ID, content=json.dumps(result)),
                ]
            # The turn's user message goes here; the search is stored after it.
            after_user = len(tally.messages) + 1
            # The fast model is offered no tools, so it gets the session's earlier
            # tool calls as the text around them, not as calls it cannot make.
            async for event in run_turn(
                model,
                _as_text(history),
                user_text,
                system="\n\n".join(part for part in (system, context) if part) or None,
                max_tokens=max_tokens,
                tally=tally,
            ):
                if isinstance(event, DoneEvent):
                    # Before done, which is when the caller stores the messages: [n]
                    # in this answer must still resolve in later turns.
                    tally.messages[after_user:after_user] = searched
                yield event
            return
        async for event in run_turn(
            model,
            history,
            user_text,
            tools=registry,
            proposals=proposals,
            system=system,
            max_tokens=max_tokens,
            tally=tally,
        ):
            yield event

    async def _decide_route(
        self, classifier: _FirstThatAnswers, history: Sequence[ChatMessage], user_text: str
    ) -> tuple[Route, RouteReason, Usage, str | None]:
        """The route, how it was reached, the classification's usage, and the model
        that classified (None when the router is off)."""
        if not self.enabled:
            return "slow", "router_off", Usage(), None
        route, reason, usage = await self._ask_fast_model(classifier, history, user_text)
        return route, reason, usage, classifier.answered or classifier.id

    async def _ask_fast_model(
        self, classifier: _FirstThatAnswers, history: Sequence[ChatMessage], user_text: str
    ) -> tuple[Route, RouteReason, Usage]:
        request = ChatRequest(
            system=ROUTER_SYSTEM,
            messages=[*_as_text(history), UserMessage(text=user_text)],
            max_tokens=self.max_tokens,
        )
        try:
            async with asyncio.timeout(self.timeout):
                reply = await read_reply(classifier, request)
        except TimeoutError:
            log.warning(
                "route classification by %s timed out after %ss", self.fast.id, self.timeout
            )
            return "slow", "timeout", Usage()
        except ChatModelError as error:
            log.warning("route classification failed: %s", error)
            return "slow", "model_error", Usage()
        usage = reply.usage
        try:
            decision = RouteDecision.model_validate_json(unfenced(reply.text))
        except ValidationError:
            if reply.reason == "max_tokens":
                # Cut off, often after spending the whole budget on thinking: the
                # budget is too small, not the model's answer wrong.
                log.warning(
                    "route classification by %s was cut off by max_tokens %s",
                    classifier.answered,
                    self.max_tokens,
                )
                return "slow", "max_tokens", usage
            log.warning("route classification by %s was not the expected JSON", classifier.answered)
            return "slow", "invalid", usage
        return decision.route, "classified", usage


def _as_text(history: Sequence[ChatMessage]) -> list[ChatMessage]:
    """The session so far as plain user and assistant text: the classification needs
    what was said, not the tool calls or their results."""
    messages: list[ChatMessage] = []
    for message in history:
        if isinstance(message, UserMessage):
            messages.append(message)
        elif isinstance(message, AssistantMessage) and message.text:
            messages.append(AssistantMessage(text=message.text))
    return messages

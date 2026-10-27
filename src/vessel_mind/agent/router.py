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
import re
from collections.abc import AsyncIterator, Sequence
from typing import Literal

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
    Stop,
    StopReason,
    TextDelta,
    ToolCall,
    ToolMessage,
    Usage,
    UserMessage,
)
from vessel_mind.core.events import (
    AgentEvent,
    DoneEvent,
    ErrorEvent,
    RouteEvent,
    RouteReason,
    ToolResultEvent,
    ToolStartEvent,
)

log = logging.getLogger(__name__)

Route = Literal["fast", "slow"]

# The tool the fast path retrieves with, and the call id of its tool_start and
# tool_result events, so a client maps [n] the same way on both paths.
SEARCH_TOOL = "search_docs"
FAST_PATH_CALL_ID = "fast-path-search"

CLASSIFIER_SYSTEM = """\
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
numbered them. Answer only from them, and cite each claim as [n]. If no section
answers the message, say that the documents do not cover it.

"""


class RouteDecision(BaseModel):
    """What the classification must be."""

    model_config = ConfigDict(extra="forbid", strict=True)

    route: Route


class _FirstThatAnswers:
    """One path's models, in order, as one ChatModel for one turn.

    A model that fails before its first event (its adapter has spent its retries)
    hands the request to the next one. A model that fails after it started
    answering is not replaced: the client has already seen part of its answer.
    answered is the id of the model that answered last, or None.
    """

    def __init__(self, models: Sequence[ChatModel]) -> None:
        self.models = models
        self.id = models[0].id
        self.answered: str | None = None

    async def stream(self, req: ChatRequest) -> AsyncIterator[ChatEvent]:
        for number, model in enumerate(self.models):
            started = False
            try:
                async for event in model.stream(req):
                    if not started:
                        started = True
                        self.answered = model.id
                    yield event
            except ChatModelError as error:
                if started or number == len(self.models) - 1:
                    raise
                log.warning("%s failed, trying %s: %s", model.id, self.models[number + 1].id, error)
                continue
            self.answered = model.id
            return


class Router:
    """Routes each turn between two models: one configuration's fast and slow paths.

    enabled=False sends every turn to the slow path without asking the fast model:
    the eval's slow-only baseline. timeout bounds the classification in seconds.

    fast_fallbacks and slow_fallbacks are each path's fallback list, in order
    (spec §5.1): when a path's model fails after its retries, the next one is
    asked, and the route event names the model that answered.
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
    ) -> None:
        self.fast = fast
        self.slow = slow
        self.fast_fallbacks = tuple(fast_fallbacks)
        self.slow_fallbacks = tuple(slow_fallbacks)
        self.enabled = enabled
        self.timeout = timeout
        self.max_tokens = max_tokens

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
        route, reason, usage, router_model = await self._classify(history, user_text)
        if route == "fast":
            model = _FirstThatAnswers((self.fast, *self.fast_fallbacks))
        else:
            model = _FirstThatAnswers((self.slow, *self.slow_fallbacks))
        # With a fallback list, which model answers is known only once one has
        # started: the turn's events wait until then, so the route event, which
        # names that model, still comes first.
        waiting = len(model.models) > 1
        held: list[AgentEvent] = []
        decision: RouteEvent | None = None

        def decide() -> RouteEvent:
            return RouteEvent(
                route=route,
                reason=reason,
                router_model=router_model,
                router_usage=usage,
                model=model.answered or model.id,
            )

        if not waiting:
            decision = decide()
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
                decision = decide()
                yield decision
                for earlier in held:
                    yield earlier
            if isinstance(event, DoneEvent):
                log.info(
                    "route %s (%s): router %s %s; answer %s %s",
                    route,
                    reason,
                    decision.router_model,
                    usage.model_dump(),
                    decision.model,
                    event.usage.model_dump(),
                )
            yield event

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

    async def _classify(
        self, history: Sequence[ChatMessage], user_text: str
    ) -> tuple[Route, RouteReason, Usage, str | None]:
        """The route, how it was reached, the classification's usage, and the model
        that classified (None when the router is off)."""
        if not self.enabled:
            return "slow", "router_off", Usage(), None
        route, reason, usage, classifier = await self._ask_fast_model(history, user_text)
        return route, reason, usage, classifier.answered or classifier.id

    async def _ask_fast_model(
        self, history: Sequence[ChatMessage], user_text: str
    ) -> tuple[Route, RouteReason, Usage, _FirstThatAnswers]:
        classifier = _FirstThatAnswers((self.fast, *self.fast_fallbacks))
        request = ChatRequest(
            system=CLASSIFIER_SYSTEM,
            messages=[*_as_text(history), UserMessage(text=user_text)],
            max_tokens=self.max_tokens,
        )
        text: list[str] = []
        usage = Usage()
        stop_reason: StopReason = "end"
        try:
            async with asyncio.timeout(self.timeout):
                async for event in classifier.stream(request):
                    match event:
                        case TextDelta(delta=delta):
                            text.append(delta)
                        case Stop(reason=reason, usage=step_usage):
                            usage = step_usage
                            stop_reason = reason
                        case _:
                            pass
        except TimeoutError:
            log.warning(
                "route classification by %s timed out after %ss", self.fast.id, self.timeout
            )
            return "slow", "timeout", usage, classifier
        except ChatModelError as error:
            log.warning("route classification failed: %s", error)
            return "slow", "model_error", usage, classifier
        try:
            decision = RouteDecision.model_validate_json(_unfenced("".join(text)))
        except ValidationError:
            if stop_reason == "max_tokens":
                # Cut off, often after spending the whole budget on thinking: the
                # budget is too small, not the model's answer wrong.
                log.warning(
                    "route classification by %s was cut off by max_tokens %s",
                    classifier.answered,
                    self.max_tokens,
                )
                return "slow", "max_tokens", usage, classifier
            log.warning("route classification by %s was not the expected JSON", classifier.answered)
            return "slow", "invalid", usage, classifier
        return decision.route, "classified", usage, classifier


_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)


def _unfenced(text: str) -> str:
    """The text without a Markdown code fence around it, which small models often add."""
    text = text.strip()
    match = _FENCE.match(text)
    return match.group(1) if match else text


def _as_text(history: Sequence[ChatMessage]) -> list[ChatMessage]:
    """The conversation as plain user and assistant text: the classification needs
    what was said, not the tool calls or their results."""
    messages: list[ChatMessage] = []
    for message in history:
        if isinstance(message, UserMessage):
            messages.append(message)
        elif isinstance(message, AssistantMessage) and message.text:
            messages.append(AssistantMessage(text=message.text))
    return messages

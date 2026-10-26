"""The router (ADR 0001 §3, spec §5.7): picks the fast path or the slow path per turn.

The fast model classifies the turn as {"route": "fast" | "slow"}. On the fast
path the router retrieves first, with the registry's search tool, and the fast
model answers from that context with no tools. On the slow path the slow model
runs the full tool loop.
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
    ChatMessage,
    ChatModel,
    ChatModelError,
    ChatRequest,
    Stop,
    TextDelta,
    Usage,
    UserMessage,
)
from vessel_mind.core.events import (
    AgentEvent,
    DoneEvent,
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


class Router:
    """Routes each turn between two models: one configuration's fast and slow paths.

    enabled=False sends every turn to the slow path without asking the fast model:
    the eval's slow-only baseline. timeout bounds the classification in seconds.
    """

    def __init__(
        self,
        fast: ChatModel,
        slow: ChatModel,
        *,
        enabled: bool = True,
        timeout: float = 10.0,
        max_tokens: int | None = None,
    ) -> None:
        self.fast = fast
        self.slow = slow
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
        route, reason, usage = await self._classify(history, user_text)
        model = self.fast if route == "fast" else self.slow
        decision = RouteEvent(
            route=route,
            reason=reason,
            router_model=self.fast.id if self.enabled else None,
            router_usage=usage,
            model=model.id,
        )
        yield decision

        if route == "fast":
            context = None
            if registry.get(SEARCH_TOOL) is not None:
                query = {"query": user_text}
                yield ToolStartEvent(call_id=FAST_PATH_CALL_ID, name=SEARCH_TOOL, input=query)
                result, is_error = await run_tool(registry, SEARCH_TOOL, query)
                yield ToolResultEvent(
                    call_id=FAST_PATH_CALL_ID, name=SEARCH_TOOL, result=result, is_error=is_error
                )
                context = FAST_CONTEXT + json.dumps(result, ensure_ascii=False, indent=1)
            turn = run_turn(
                self.fast,
                history,
                user_text,
                system="\n\n".join(part for part in (system, context) if part) or None,
                max_tokens=max_tokens,
                tally=tally,
            )
        else:
            turn = run_turn(
                self.slow,
                history,
                user_text,
                tools=registry,
                proposals=proposals,
                system=system,
                max_tokens=max_tokens,
                tally=tally,
            )
        async for event in turn:
            if isinstance(event, DoneEvent):
                log.info(
                    "route %s (%s): router %s %s; answer %s %s",
                    route,
                    reason,
                    decision.router_model,
                    usage.model_dump(),
                    model.id,
                    event.usage.model_dump(),
                )
            yield event

    async def _classify(
        self, history: Sequence[ChatMessage], user_text: str
    ) -> tuple[Route, RouteReason, Usage]:
        """The route, how it was reached, and the classification's usage."""
        if not self.enabled:
            return "slow", "router_off", Usage()
        request = ChatRequest(
            system=CLASSIFIER_SYSTEM,
            messages=[*_as_text(history), UserMessage(text=user_text)],
            max_tokens=self.max_tokens,
        )
        text: list[str] = []
        usage = Usage()
        try:
            async with asyncio.timeout(self.timeout):
                async for event in self.fast.stream(request):
                    match event:
                        case TextDelta(delta=delta):
                            text.append(delta)
                        case Stop(usage=step_usage):
                            usage = step_usage
                        case _:
                            pass
        except TimeoutError:
            log.warning(
                "route classification by %s timed out after %ss", self.fast.id, self.timeout
            )
            return "slow", "timeout", usage
        except ChatModelError as error:
            log.warning("route classification failed: %s", error)
            return "slow", "model_error", usage
        try:
            decision = RouteDecision.model_validate_json(_unfenced("".join(text)))
        except ValidationError:
            log.warning("route classification by %s was not the expected JSON", self.fast.id)
            return "slow", "invalid", usage
        return decision.route, "classified", usage


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

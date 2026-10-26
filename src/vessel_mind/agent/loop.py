"""The agent loop: turns a user message into protocol events over a ChatModel.

This is the tracer version: one model step and no tools.
"""

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field

from vessel_mind.core.chat import (
    ChatMessage,
    ChatModel,
    ChatModelError,
    ChatRequest,
    Stop,
    TextDelta,
    Usage,
    UserMessage,
)
from vessel_mind.core.events import AgentEvent, DoneEvent, ErrorEvent, TextEvent


@dataclass
class TurnTally:
    """What a turn has used so far. The caller reads it when it cancels the turn."""

    usage: Usage = field(default_factory=Usage)
    steps: int = 0


async def run_turn(
    model: ChatModel,
    history: Sequence[ChatMessage],
    user_text: str,
    *,
    system: str | None = None,
    max_tokens: int | None = None,
    tally: TurnTally | None = None,
) -> AsyncIterator[AgentEvent]:
    """Yields text events, then done or error.

    Cancellation is cancelling the consuming task; the caller reports it with
    DoneEvent(reason="cancelled") and the tally.
    """
    tally = tally if tally is not None else TurnTally()
    request = ChatRequest(
        system=system,
        messages=[*history, UserMessage(text=user_text)],
        max_tokens=max_tokens,
    )
    tally.steps += 1
    reason = "end"
    try:
        async for event in model.stream(request):
            match event:
                case TextDelta(delta=delta):
                    yield TextEvent(text=delta)
                case Stop(reason=stop_reason, usage=usage):
                    tally.usage += usage
                    if stop_reason == "max_tokens":
                        reason = "max_tokens"
                case _:
                    pass  # Tool calls arrive only when tools are offered; none are yet.
    except ChatModelError as error:
        yield ErrorEvent(code="model_error", message=str(error))
        return
    yield DoneEvent(reason=reason, usage=tally.usage, steps=tally.steps)

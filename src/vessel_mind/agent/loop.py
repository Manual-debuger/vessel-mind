"""The agent loop: turns a user message into protocol events over a ChatModel.

Each model step may call tools. The loop runs the calls of a step one after
another, sends the results back, and asks again, for at most MAX_STEPS steps.
Only tools labelled `read` execute; the loop never runs a `write` or
`irreversible` tool (ADR 0001 §5). It makes a proposal for one instead.
"""

import json
import logging
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import JsonValue, TypeAdapter

from vessel_mind.agent.proposals import Proposals
from vessel_mind.core.capabilities import CapabilityRegistry, ToolError
from vessel_mind.core.chat import (
    AssistantMessage,
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
    ProposalEvent,
    TextEvent,
    ToolResultEvent,
    ToolStartEvent,
)

log = logging.getLogger(__name__)

_json: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)

# Model steps per turn. The last step offers no tools, so the model has to answer.
MAX_STEPS = 8


@dataclass
class TurnTally:
    """What a turn has used so far. The caller reads it when it cancels the turn."""

    usage: Usage = field(default_factory=Usage)
    steps: int = 0
    # What the turn added to the conversation, in order: the user message, then
    # each assistant message, and after one that called tools, one ToolMessage per
    # call. The caller stores them as history.
    messages: list[ChatMessage] = field(default_factory=list)


async def run_turn(
    model: ChatModel,
    history: Sequence[ChatMessage],
    user_text: str,
    *,
    tools: CapabilityRegistry | None = None,
    proposals: Proposals | None = None,
    system: str | None = None,
    max_tokens: int | None = None,
    tally: TurnTally | None = None,
) -> AsyncIterator[AgentEvent]:
    """Yields text, tool_start, proposal and tool_result events, then done or error.

    A write or irreversible call is stored in proposals and never executed; without
    proposals it is refused.

    Cancellation is cancelling the consuming task; the caller reports it with
    DoneEvent(reason="cancelled") and the tally.
    """
    tally = tally if tally is not None else TurnTally()
    registry = tools if tools is not None else CapabilityRegistry()
    tally.messages.append(UserMessage(text=user_text))
    while True:
        tally.steps += 1
        last_step = tally.steps >= MAX_STEPS
        request = ChatRequest(
            system=system,
            messages=[*history, *tally.messages],
            tools=[] if last_step else registry.specs(),
            max_tokens=max_tokens,
        )
        text: list[str] = []
        calls: list[ToolCall] = []
        stop_reason: StopReason = "end"
        try:
            async for event in model.stream(request):
                match event:
                    case TextDelta(delta=delta):
                        text.append(delta)
                        yield TextEvent(text=delta)
                    case ToolCall():
                        calls.append(event)
                    case Stop(reason=reason, usage=usage):
                        tally.usage += usage
                        stop_reason = reason
        except ChatModelError as error:
            yield ErrorEvent(code="model_error", message=str(error))
            return

        if not calls or last_step:
            # Calls on the last step are not run, and not kept: history must not
            # hold a call without its result.
            tally.messages.append(AssistantMessage(text="".join(text)))
            reason = (
                "max_steps" if calls else ("max_tokens" if stop_reason == "max_tokens" else "end")
            )
            yield DoneEvent(reason=reason, usage=tally.usage, steps=tally.steps)
            return

        tally.messages.append(AssistantMessage(text="".join(text), tool_calls=calls))
        for call in calls:
            yield ToolStartEvent(call_id=call.id, name=call.name, input=call.input)
            result, is_error = None, False
            async for outcome in _dispatch(registry, proposals, call):
                if isinstance(outcome, ProposalEvent):
                    yield outcome
                else:
                    result, is_error = outcome
            yield ToolResultEvent(call_id=call.id, name=call.name, result=result, is_error=is_error)
            tally.messages.append(ToolMessage(tool_call_id=call.id, content=json.dumps(result)))


async def _dispatch(
    registry: CapabilityRegistry, proposals: Proposals | None, call: ToolCall
) -> AsyncIterator[ProposalEvent | tuple[JsonValue, bool]]:
    """Runs one call if it is a registered read tool, or proposes it.

    Yields a ProposalEvent for a proposed call, then always (result, is_error) last.
    """
    entry = registry.get(call.name)
    if entry is None:
        yield {"error": f"Unknown tool '{call.name}'."}, True
        return
    effect = entry.labels.effect
    if effect == "read":
        yield await run_tool(registry, call.name, call.input)
        return
    # A write or irreversible tool needs an approval; it never runs from the loop.
    if proposals is None:
        yield (
            {
                "error": f"'{call.name}' changes something outside the runtime, so it needs "
                "a human approval, and this runtime cannot ask for one. It was not run."
            },
            True,
        )
        return
    proposal = proposals.propose(call, effect)
    yield ProposalEvent(
        proposal_id=proposal.id,
        call_id=call.id,
        name=call.name,
        input=call.input,
        effect=effect,
        expires_at=proposal.expires_at,
    )
    yield {"status": "awaiting_approval", "proposal_id": proposal.id}, False


async def run_tool(
    registry: CapabilityRegistry, name: str, input: dict[str, Any]
) -> tuple[JsonValue, bool]:
    """Executes a registered tool, whatever its effect. Returns (result, is_error).

    The loop calls it for read tools only; an approval calls it for a proposal.
    """
    entry = registry.get(name)
    if entry is None:
        return {"error": f"Unknown tool '{name}'."}, True
    try:
        # Checked here, so a tool that returns something JSON cannot carry fails the
        # call, not the turn.
        return _json.validate_python(await entry.tool.execute(input)), False
    except ToolError as error:
        return {"error": str(error)}, True
    except Exception:
        log.exception("tool %s failed", name)
        return {"error": "The tool failed."}, True

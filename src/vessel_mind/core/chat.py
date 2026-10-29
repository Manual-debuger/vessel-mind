"""The ChatModel seam: the project's own request, message and event types.

These types are not any provider's wire format. An adapter translates them to and
from its provider, so a new adapter never changes the loop (ADR 0001 §2).
"""

import re
from collections.abc import AsyncIterator
from typing import Annotated, Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field


class _Model(BaseModel):
    # Defaults are always serialized, so the serialization schema lists them as required.
    model_config = ConfigDict(
        frozen=True, extra="forbid", json_schema_serialization_defaults_required=True
    )


class Usage(_Model):
    """Token counts for one model step, or summed over a turn."""

    input_tokens: int = 0
    output_tokens: int = 0
    # Reasoning tokens a provider reports only inside its total.
    thinking_tokens: int = 0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            thinking_tokens=self.thinking_tokens + other.thinking_tokens,
        )


class TextDelta(_Model):
    type: Literal["text_delta"] = "text_delta"
    delta: str


class ToolCall(_Model):
    type: Literal["tool_call"] = "tool_call"
    id: str
    name: str
    input: dict[str, Any]
    # Opaque to everything but the adapter that wrote it; sent back unchanged.
    provider_data: dict[str, Any] | None = None


StopReason = Literal["end", "tool_use", "max_tokens", "aborted"]


class Stop(_Model):
    type: Literal["stop"] = "stop"
    reason: StopReason
    usage: Usage


ChatEvent = Annotated[TextDelta | ToolCall | Stop, Field(discriminator="type")]


class UserMessage(_Model):
    role: Literal["user"] = "user"
    text: str


class AssistantMessage(_Model):
    role: Literal["assistant"] = "assistant"
    text: str
    # The calls the model made in this step, each with its provider_data, so the
    # adapter can send them back unchanged on the next step.
    tool_calls: list[ToolCall] = []


class ToolMessage(_Model):
    """The result of one tool call, sent back to the model as text (usually JSON)."""

    role: Literal["tool"] = "tool"
    tool_call_id: str
    content: str


ChatMessage = Annotated[UserMessage | AssistantMessage | ToolMessage, Field(discriminator="role")]


class ToolSpec(_Model):
    """A tool as the model sees it: a name, what it does, and its input's JSON Schema."""

    # The character set every OpenAI-compatible provider accepts for a function name.
    name: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    description: str
    parameters: dict[str, Any]


class ChatRequest(_Model):
    system: str | None = None
    messages: list[ChatMessage]
    # Offered to the model on this step; empty means it can only answer in text.
    tools: list[ToolSpec] = []
    max_tokens: int | None = None


class ChatModelError(Exception):
    """A model call failed after the adapter's own retries. The message holds no secret."""


class ChatModel(Protocol):
    """Streams one model step. Cancellation is cancelling the consuming task."""

    id: str

    def stream(self, req: ChatRequest) -> AsyncIterator[ChatEvent]: ...


class Reply(_Model):
    """One model step's whole text, for a caller that reads it only once it is complete."""

    text: str
    reason: StopReason
    usage: Usage


async def read_reply(model: ChatModel, req: ChatRequest) -> Reply:
    """Streams one step to its end and joins its text. Raises ChatModelError."""
    text: list[str] = []
    reason: StopReason = "end"
    usage = Usage()
    async for event in model.stream(req):
        match event:
            case TextDelta(delta=delta):
                text.append(delta)
            case Stop():
                reason, usage = event.reason, event.usage
            case _:
                pass
    return Reply(text="".join(text), reason=reason, usage=usage)


_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)


def unfenced(text: str) -> str:
    """The text without a Markdown code fence around it, which small models often add."""
    text = text.strip()
    match = _FENCE.match(text)
    return match.group(1) if match else text

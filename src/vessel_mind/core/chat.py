"""The ChatModel seam: the project's own request, message and event types.

These types are not any provider's wire format. An adapter translates them to and
from its provider, so a new adapter never changes the loop (ADR 0001 §2).
"""

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


ChatMessage = Annotated[UserMessage | AssistantMessage, Field(discriminator="role")]


class ChatRequest(_Model):
    system: str | None = None
    messages: list[ChatMessage]
    max_tokens: int | None = None


class ChatModelError(Exception):
    """A model call failed after the adapter's own retries. The message holds no secret."""


class ChatModel(Protocol):
    """Streams one model step. Cancellation is cancelling the consuming task."""

    id: str

    def stream(self, req: ChatRequest) -> AsyncIterator[ChatEvent]: ...

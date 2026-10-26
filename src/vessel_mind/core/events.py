"""The event protocol (ADR 0001 §6): what a turn streams to its client.

The JSON Schema of AgentEvent is committed as schema/events.schema.json, so clients
generate their types from it. Regenerate it with `task schema:export`; a test fails
when the committed file differs.
"""

import datetime
import json
import sys
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter

from vessel_mind.core.chat import Usage


class _Event(BaseModel):
    model_config = ConfigDict(
        frozen=True, extra="forbid", json_schema_serialization_defaults_required=True
    )


class TextEvent(_Event):
    """A piece of the answer, in order. Concatenate them to get the text."""

    type: Literal["text"] = "text"
    text: str


class ToolStartEvent(_Event):
    """The loop is about to run one tool call. A tool_result with the same call_id follows."""

    type: Literal["tool_start"] = "tool_start"
    call_id: str
    name: str
    input: dict[str, Any]


class ToolResultEvent(_Event):
    """The result of one tool call, as the model receives it."""

    type: Literal["tool_result"] = "tool_result"
    call_id: str
    name: str
    # Any JSON value. When is_error is true it is {"error": <message>}.
    result: JsonValue
    is_error: bool


class ProposalEvent(_Event):
    """A write or irreversible call waits for a human approval; it has not run.

    It comes between the call's tool_start and tool_result. Resolve it with
    POST /proposals/{proposal_id}/approve or /reject before expires_at.
    """

    type: Literal["proposal"] = "proposal"
    proposal_id: str
    call_id: str
    name: str
    input: dict[str, Any]
    effect: Literal["write", "irreversible"]
    expires_at: datetime.datetime


class DoneEvent(_Event):
    """The turn ended. Sent once, last, unless the turn ended with an error event."""

    type: Literal["done"] = "done"
    # max_steps: the model still called tools on the turn's last allowed step.
    reason: Literal["end", "max_tokens", "max_steps", "cancelled"]
    # Summed over the turn's model steps. A step cancelled mid-stream adds nothing,
    # because the provider reports usage only when a step ends.
    usage: Usage
    steps: int


class ErrorEvent(_Event):
    """The turn failed. Sent once, last, instead of done."""

    type: Literal["error"] = "error"
    code: Literal["model_error", "internal_error"]
    message: str


AgentEvent = Annotated[
    TextEvent | ToolStartEvent | ProposalEvent | ToolResultEvent | DoneEvent | ErrorEvent,
    Field(discriminator="type"),
]

_adapter: TypeAdapter[Any] = TypeAdapter(AgentEvent)


def event_schema() -> dict[str, Any]:
    # The schema of what the server sends, which is what a client's types describe.
    schema = _adapter.json_schema(mode="serialization")
    schema["title"] = "AgentEvent"
    return schema


def event_schema_text() -> str:
    return json.dumps(event_schema(), indent=2, sort_keys=True) + "\n"


if __name__ == "__main__":
    Path(sys.argv[1]).write_text(event_schema_text(), encoding="utf-8", newline="\n")

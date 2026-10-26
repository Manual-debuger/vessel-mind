"""Capability labels and the registry: a port of Vessel's capabilities.ts.

Every tool, built in or from MCP, is registered with four labels or not at all. A
tool without labels is refused with a warning; labels are never defaulted. The
loop executes only tools whose effect is `read` (ADR 0001 §5).
"""

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, ValidationError

from vessel_mind.core.chat import ToolSpec

log = logging.getLogger(__name__)

# Vessel's resources, plus two this runtime needs:
#   public_corpus: the public documents the runtime retrieves from (search_docs).
#     It is public by definition (ADR 0001 §7), so it is NOT private data.
#   workspace: an external workspace the operator connects, such as Notion.
#     It holds the operator's own pages, so it IS private data.
Resource = Literal[
    "memory", "timers", "voice", "filesystem", "network", "public_corpus", "workspace"
]
Effect = Literal["read", "write", "irreversible"]
TrustLevel = Literal["tainted", "trusted"]

# The resources whose data must not leave: one third of the trifecta.
PRIVATE_RESOURCES: frozenset[str] = frozenset({"memory", "filesystem", "workspace"})


class CapabilityLabels(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    resource: Resource
    effect: Effect
    # The tool can send data out of the runtime.
    exfil: bool
    # The tool's result can carry instructions the operator did not write.
    taints_context: bool


class CapabilityConfigError(Exception):
    """Labels are invalid, or two tools share a name. Raised at startup, never defaulted."""


class ToolError(Exception):
    """A tool failed in a way the model may read. Its message must hold no secret."""


class Tool(Protocol):
    """A tool the loop can call. Labels are given at registration, not by the tool."""

    @property
    def spec(self) -> ToolSpec: ...

    # Returns a JSON value (dict, list, str, number, bool or None). The loop checks
    # it. Raise ToolError for a failure the model may read about.
    async def execute(self, input: dict[str, Any]) -> Any: ...


@dataclass(frozen=True)
class RegisteredTool:
    tool: Tool
    labels: CapabilityLabels

    @property
    def name(self) -> str:
        return self.tool.spec.name


class CapabilityRegistry:
    """The tools of one runtime, each with its labels."""

    def __init__(self, warn: Callable[[str], None] = log.warning) -> None:
        self._warn = warn
        self._tools: dict[str, RegisteredTool] = {}
        self.trifecta_warning: str | None = None

    def register(self, tool: Tool, labels: CapabilityLabels | Mapping[str, Any] | None) -> bool:
        """Registers the tool; returns False, with a warning, when it has no labels.

        Raises CapabilityConfigError for invalid labels or a name already registered.
        """
        name = tool.spec.name
        if labels is None:
            self._warn(f"Capability '{name}' was not registered: labels are required.")
            return False
        if name in self._tools:
            raise CapabilityConfigError(f"Invalid capability config — {name}: registered twice")
        self._tools[name] = RegisteredTool(tool=tool, labels=_parse(name, labels))
        self._check_trifecta()
        return True

    def get(self, name: str) -> RegisteredTool | None:
        return self._tools.get(name)

    def specs(self) -> list[ToolSpec]:
        return [entry.tool.spec for entry in self._tools.values()]

    @property
    def trust_level(self) -> TrustLevel:
        tainted = any(entry.labels.taints_context for entry in self._tools.values())
        return "tainted" if tainted else "trusted"

    def _check_trifecta(self) -> None:
        if self.trifecta_warning is not None:
            return
        tools = list(self._tools.values())
        taint = next((t for t in tools if t.labels.taints_context), None)
        private = next((t for t in tools if t.labels.resource in PRIVATE_RESOURCES), None)
        exfil = next((t for t in tools if t.labels.exfil), None)
        if taint and private and exfil:
            self.trifecta_warning = (
                f"The registry combines tainted input ('{taint.name}'), private data "
                f"('{private.name}'), and exfiltration ('{exfil.name}')."
            )
            self._warn(self.trifecta_warning)


def _parse(name: str, labels: CapabilityLabels | Mapping[str, Any]) -> CapabilityLabels:
    if isinstance(labels, CapabilityLabels):
        return labels
    try:
        return CapabilityLabels.model_validate(dict(labels))
    except ValidationError as error:
        first = error.errors(include_input=False)[0]
        field = ".".join(str(part) for part in first["loc"]) or "labels"
        raise CapabilityConfigError(
            f"Invalid capability config — {name}.{field}: {first['msg']}"
        ) from None
    except TypeError:
        raise CapabilityConfigError(
            f"Invalid capability config — {name}.labels: must be a mapping"
        ) from None

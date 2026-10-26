"""Tools from an external MCP server, started as a subprocess over stdio.

An McpServer is an async context manager: entering it starts the server, lists
its tools and registers each one whose name the config labels. A tool without
labels is refused by the registry with a warning; labels are never defaulted.
"""

import json
import logging
import re
from collections.abc import Callable, Mapping, Sequence
from contextlib import AsyncExitStack
from typing import Any, Self

import anyio
from mcp import ClientSession, StdioServerParameters, stdio_client
from mcp import types as mcp_types

from vessel_mind.core.capabilities import CapabilityLabels, CapabilityRegistry, ToolError
from vessel_mind.core.chat import ToolSpec
from vessel_mind.core.credentials import Credential

log = logging.getLogger(__name__)

# Seconds one tool call may take. An approval runs its call inside the HTTP
# request, so a server that never answers must not hold that request open.
CALL_TIMEOUT = 60.0

# Characters a model API refuses in a function name (core.chat.ToolSpec).
_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")


def model_tool_name(server: str, tool: str) -> str:
    """The name the model sees: '<server>_<tool>', unsafe characters as '_', at most 64."""
    return _UNSAFE.sub("_", f"{server}_{tool}")[:64]


class McpTool:
    """One tool of a running MCP server, as a core Tool."""

    def __init__(
        self, session: ClientSession, listed: mcp_types.Tool, name: str, timeout: float
    ) -> None:
        self._session = session
        self._timeout = timeout
        self.mcp_name = listed.name
        self._spec = ToolSpec(
            name=name,
            description=listed.description or listed.title or listed.name,
            parameters=listed.input_schema,
        )

    @property
    def spec(self) -> ToolSpec:
        return self._spec

    async def execute(self, input: dict[str, Any]) -> Any:
        try:
            with anyio.fail_after(self._timeout):
                result = await self._session.call_tool(self.mcp_name, input)
        except TimeoutError:
            raise ToolError(
                f"'{self.spec.name}' did not answer within {self._timeout:g} seconds."
            ) from None
        texts = [block.text for block in result.content if isinstance(block, mcp_types.TextContent)]
        if result.is_error:
            raise ToolError(" ".join(texts) or f"'{self.spec.name}' failed.")
        if not texts and result.structured_content is not None:
            return result.structured_content
        values = [_json_or_text(text) for text in texts]
        return values[0] if len(values) == 1 else values


def _json_or_text(text: str) -> Any:
    """Servers such as Notion's send JSON as text; it reaches the model as JSON."""
    try:
        return json.loads(text)
    except ValueError:
        return text


class McpServer:
    """A configured MCP server: started on enter, stopped on exit."""

    def __init__(
        self,
        name: str,
        *,
        command: str,
        args: Sequence[str] = (),
        labels: Mapping[str, CapabilityLabels | Mapping[str, Any]],
        registry: CapabilityRegistry,
        credential: Credential | None = None,
        headers_env: str | None = None,
        call_timeout: float = CALL_TIMEOUT,
        warn: Callable[[str], None] = log.warning,
    ) -> None:
        """labels maps the server's own tool names to labels; the model sees each
        registered tool as '<name>_<tool>'.

        The server process inherits only the SDK's safe variables (PATH, HOME and a
        few more), never the runtime's secrets. A credential reaches it as the JSON
        object of its headers in the env var headers_env, for example
        OPENAPI_MCP_HEADERS for Notion's server.
        """
        if (credential is None) != (headers_env is None):
            raise ValueError("credential and headers_env go together")
        self.name = name
        self._command = command
        self._args = list(args)
        self._labels = dict(labels)
        self._registry = registry
        self._credential = credential
        self._headers_env = headers_env
        self._warn = warn
        self._call_timeout = call_timeout
        self._stack: AsyncExitStack | None = None
        # Every tool name the server listed, labelled or not.
        self.listed: list[str] = []

    async def __aenter__(self) -> Self:
        async with AsyncExitStack() as stack:
            env: dict[str, str] = {}
            if self._credential is not None and self._headers_env is not None:
                env[self._headers_env] = json.dumps(self._credential.headers())
            params = StdioServerParameters(command=self._command, args=self._args, env=env)
            read, write = await stack.enter_async_context(stdio_client(params))
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            for listed in await _list_tools(session):
                self.listed.append(listed.name)
                tool = McpTool(
                    session, listed, model_tool_name(self.name, listed.name), self._call_timeout
                )
                self._registry.register(tool, self._labels.get(listed.name))
            for unused in self._labels.keys() - set(self.listed):
                # Most often a typo in the config, which would drop the tool silently.
                self._warn(
                    f"MCP server '{self.name}' lists no tool '{unused}'; its labels are unused."
                )
            self._stack = stack.pop_all()
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._stack is not None:
            stack, self._stack = self._stack, None
            await stack.aclose()


async def _list_tools(session: ClientSession) -> list[mcp_types.Tool]:
    tools: list[mcp_types.Tool] = []
    cursor: str | None = None
    while True:
        params = mcp_types.PaginatedRequestParams(cursor=cursor) if cursor else None
        page = await session.list_tools(params=params)
        tools.extend(page.tools)
        cursor = page.next_cursor
        if not cursor:
            return tools

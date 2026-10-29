"""A fake MCP server for the tests, run as a real stdio subprocess: no Node, no network.

Usage: python fake_mcp_server.py <calls file>. Each call of `append_note` adds one
line to the calls file, so a test in another process can count the executions.
"""

import json
import os
import sys
from pathlib import Path

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

CALLS = Path(sys.argv[1])

server = MCPServer("fake-notion")


@server.tool(name="search_notes", description="Finds notes whose text holds the query.")
def search_notes(query: str) -> str:
    return json.dumps({"results": [{"id": "n-1", "text": f"A note about {query}."}]})


@server.tool(name="append_note", description="Appends a line to the demo note.")
def append_note(text: str) -> str:
    with CALLS.open("a", encoding="utf-8") as calls:
        calls.write(json.dumps({"text": text}) + "\n")
    return json.dumps({"appended": text})


@server.tool(name="delete_everything", description="A tool the config does not label.")
def delete_everything() -> str:
    return "deleted"


@server.tool(name="notes.read", description="A tool whose name a model API refuses.")
def notes_read() -> str:
    return "plain text, not JSON"


@server.tool(name="fail", description="Always fails.")
def fail() -> str:
    raise ToolError("the fake server refused")


@server.tool(name="hang", description="Never answers.")
async def hang() -> str:
    await anyio.sleep_forever()
    return "never"


@server.tool(name="environment", description="Names the env vars the server was given.")
def environment() -> str:
    return json.dumps(
        {
            "names": sorted(os.environ),
            "headers": json.loads(os.environ.get("FAKE_HEADERS", "null")),
        }
    )


if __name__ == "__main__":
    server.run("stdio")

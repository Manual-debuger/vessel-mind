"""MCP tools over stdio, against a fake MCP server in a real subprocess."""

import asyncio
import sys
from pathlib import Path

import pytest

from vessel_mind.agent.loop import run_tool
from vessel_mind.agent.mcp_client import McpServer
from vessel_mind.core.capabilities import CapabilityRegistry
from vessel_mind.core.credentials import Credential

FAKE = Path(__file__).resolve().parent.parent / "fixtures" / "fake_mcp_server.py"

READ = {"resource": "workspace", "effect": "read", "exfil": False, "taints_context": True}
WRITE = {"resource": "workspace", "effect": "write", "exfil": True, "taints_context": True}


def fake_server(tmp_path, registry, labels, **kwargs) -> McpServer:
    return McpServer(
        "fake",
        command=sys.executable,
        args=[str(FAKE), str(tmp_path / "calls.jsonl")],
        labels=labels,
        registry=registry,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_only_the_labelled_tools_are_registered_with_their_labels(tmp_path):
    warnings: list[str] = []
    registry = CapabilityRegistry(warn=warnings.append)

    async with fake_server(
        tmp_path, registry, {"search_notes": READ, "append_note": WRITE}
    ) as server:
        names = [spec.name for spec in registry.specs()]
        listed = server.listed

    assert names == ["fake_search_notes", "fake_append_note"]
    assert "delete_everything" in listed
    assert registry.get("fake_delete_everything") is None
    assert "Capability 'fake_delete_everything' was not registered: labels are required." in (
        warnings
    )
    entry = registry.get("fake_append_note")
    assert entry is not None
    assert entry.labels.effect == "write"
    assert entry.labels.exfil is True
    search = registry.get("fake_search_notes")
    assert search is not None
    assert search.labels.effect == "read"
    assert search.tool.spec.description == "Finds notes whose text holds the query."
    assert search.tool.spec.parameters["properties"]["query"]["type"] == "string"


@pytest.mark.asyncio
async def test_a_label_for_a_tool_the_server_does_not_list_is_warned_about(tmp_path):
    warnings: list[str] = []
    registry = CapabilityRegistry(warn=warnings.append)

    async with fake_server(tmp_path, registry, {"search_notess": READ}, warn=warnings.append):
        pass

    assert "MCP server 'fake' lists no tool 'search_notess'; its labels are unused." in warnings


@pytest.mark.asyncio
async def test_a_read_call_round_trips_and_its_json_text_is_parsed(tmp_path):
    registry = CapabilityRegistry(warn=lambda message: None)

    async with fake_server(tmp_path, registry, {"search_notes": READ}):
        result = await run_tool(registry, "fake_search_notes", {"query": "boats"})

    assert result == ({"results": [{"id": "n-1", "text": "A note about boats."}]}, False)


@pytest.mark.asyncio
async def test_a_name_the_model_api_refuses_is_mapped_and_still_calls_the_tool(tmp_path):
    registry = CapabilityRegistry(warn=lambda message: None)

    async with fake_server(tmp_path, registry, {"notes.read": READ}):
        result = await run_tool(registry, "fake_notes_read", {})

    assert result == ("plain text, not JSON", False)


@pytest.mark.asyncio
async def test_a_tool_error_reaches_the_model_as_an_error_result(tmp_path):
    registry = CapabilityRegistry(warn=lambda message: None)

    async with fake_server(tmp_path, registry, {"fail": READ}):
        result, is_error = await run_tool(registry, "fake_fail", {})

    assert is_error is True
    assert isinstance(result, dict)
    message = result["error"]
    assert isinstance(message, str)
    assert "the fake server refused" in message


@pytest.mark.asyncio
async def test_a_call_the_server_never_answers_fails_after_the_timeout(tmp_path):
    registry = CapabilityRegistry(warn=lambda message: None)

    async with fake_server(tmp_path, registry, {"hang": READ}, call_timeout=0.5):
        result, is_error = await asyncio.wait_for(run_tool(registry, "fake_hang", {}), 10)

    assert is_error is True
    assert result == {"error": "'fake_hang' did not answer within 0.5 seconds."}


@pytest.mark.asyncio
async def test_the_server_gets_its_credential_as_headers_and_no_other_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "parent-secret")
    registry = CapabilityRegistry(warn=lambda message: None)
    credential = Credential("Authorization", "Bearer ntn-test")

    async with fake_server(
        tmp_path,
        registry,
        {"environment": READ},
        credential=credential,
        headers_env="FAKE_HEADERS",
    ):
        result, is_error = await run_tool(registry, "fake_environment", {})

    assert is_error is False
    assert isinstance(result, dict)
    assert result["headers"] == {"Authorization": "Bearer ntn-test"}
    names = result["names"]
    assert isinstance(names, list)
    assert "PATH" in names
    assert "GEMINI_API_KEY" not in names

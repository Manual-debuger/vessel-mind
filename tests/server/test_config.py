"""The app built from config/agent.example.yaml, as `task dev` builds it."""

import json
import sys
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from vessel_mind.agent.loop import run_tool
from vessel_mind.core.capabilities import CapabilityRegistry
from vessel_mind.core.credentials import BearerAuth
from vessel_mind.server.config import ConfigError, load_config
from vessel_mind.server.main import app_from_config, mcp_servers_from_config

REPO = Path(__file__).resolve().parent.parent.parent
EXAMPLE = REPO / "config" / "agent.example.yaml"


def test_the_example_config_serves_the_hosted_slow_path_model(tmp_path, monkeypatch):
    # The example's state file is relative to the working directory.
    monkeypatch.chdir(tmp_path)
    app = app_from_config(EXAMPLE, environ={"GEMINI_API_KEY": "test-key"})

    with TestClient(app) as client:
        health = client.get("/health").json()

    # gemini-3.8-flash allows 20 free requests a day; the author pinned 3.5 instead.
    assert health == {"status": "ok", "model": "gemini-3.5-flash"}


def test_the_app_keeps_sessions_in_the_configured_state_file(tmp_path):
    config = tmp_path / "agent.yaml"
    state = tmp_path / "state" / "sessions.db"
    config.write_text(
        EXAMPLE.read_text(encoding="utf-8")
        + f"\nstate:\n  path: {state.as_posix()}\nsessions:\n  max_turns: 1\n",
        encoding="utf-8",
    )
    app = app_from_config(config, environ={"GEMINI_API_KEY": "test-key"})

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200

    assert state.exists()
    loaded = load_config(config)
    assert loaded.sessions.max_turns == 1


def test_demo_mode_is_off_in_the_example_and_on_when_the_config_says_so(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert load_config(EXAMPLE).demo_mode is False
    config = config_with(tmp_path, "demo_mode: false", "demo_mode: true")

    with TestClient(app_from_config(config, environ={"GEMINI_API_KEY": "test-key"})) as client:
        page = client.get("/").text

    assert 'id="demo-mode"' in page


def test_the_example_config_bounds_turns_per_session_and_names_a_state_file():
    config = load_config(EXAMPLE)

    assert config.sessions.max_turns is not None
    assert config.state.path.name.endswith(".db")


def test_a_missing_key_names_the_env_var_and_nothing_else():
    with pytest.raises(ConfigError, match="GEMINI_API_KEY is unset or empty"):
        app_from_config(EXAMPLE, environ={})


AUTH_LINE = "auth: {scheme: bearer, key_env: GEMINI_API_KEY}"
PASTED = "AIza-not-a-real-key"


def config_with(tmp_path, old: str, new: str) -> Path:
    text = EXAMPLE.read_text(encoding="utf-8")
    assert old in text
    config = tmp_path / "agent.yaml"
    config.write_text(text.replace(old, new), encoding="utf-8")
    return config


def test_the_example_provider_names_its_key_env_var_with_the_bearer_scheme():
    assert AUTH_LINE in EXAMPLE.read_text(encoding="utf-8")


def test_config_holds_the_env_var_name_never_a_key(tmp_path):
    config = config_with(tmp_path, AUTH_LINE, f"api_key: {PASTED}")

    with pytest.raises(ConfigError, match="api_key") as error:
        load_config(config)

    assert PASTED not in str(error.value)


def test_a_key_pasted_in_place_of_the_env_var_name_is_refused_without_echoing_it(tmp_path):
    config = config_with(tmp_path, AUTH_LINE, f"auth: {{scheme: bearer, key_env: {PASTED}}}")

    with pytest.raises(ConfigError, match="NAME of an environment variable") as error:
        load_config(config)

    assert PASTED not in str(error.value)


def test_a_provider_must_state_its_auth_scheme(tmp_path):
    config = config_with(tmp_path, AUTH_LINE, "")

    with pytest.raises(ConfigError, match="auth"):
        load_config(config)


NOTION_PIN = REPO / "deploy" / "notion-mcp"
NOTION_PACKAGE = "node_modules/@notionhq/notion-mcp-server"


def test_the_example_starts_the_pinned_notion_server_with_node_and_is_off_by_default():
    notion = load_config(EXAMPLE).mcp_servers["notion"]
    package = json.loads((NOTION_PIN / "package.json").read_text(encoding="utf-8"))
    lock = json.loads((NOTION_PIN / "package-lock.json").read_text(encoding="utf-8"))

    pinned = package["dependencies"]["@notionhq/notion-mcp-server"]
    locked = lock["packages"][NOTION_PACKAGE]
    entry = f"deploy/notion-mcp/{NOTION_PACKAGE}/{locked['bin']['notion-mcp-server']}"
    assert pinned == "2.5.2"
    assert locked["version"] == pinned
    assert notion.command == "node"
    assert notion.args == [entry]
    assert notion.enabled is False
    assert notion.auth == BearerAuth(key_env="NOTION_TOKEN")
    assert notion.headers_env == "OPENAPI_MCP_HEADERS"


def test_the_example_labels_notion_search_and_read_as_read_and_create_and_update_as_write():
    tools = load_config(EXAMPLE).mcp_servers["notion"].tools
    effects = {name: labels.effect for name, labels in tools.items()}

    assert effects == {
        "API-post-search": "read",
        "API-retrieve-a-page": "read",
        "API-get-block-children": "read",
        "API-post-page": "write",
        "API-patch-page": "write",
        "API-patch-block-children": "write",
    }
    assert all(labels.resource == "workspace" for labels in tools.values())
    assert all(labels.exfil for labels in tools.values() if labels.effect == "write")


NOTION_ENABLED = "    enabled: false"


def test_an_mcp_server_is_never_started_with_a_package_runner(tmp_path):
    config = config_with(tmp_path, "command: node", "command: npx")

    with pytest.raises(ConfigError, match="mcp_servers.notion.command"):
        load_config(config)


def test_an_mcp_server_with_a_credential_names_the_env_var_that_carries_it(tmp_path):
    config = config_with(tmp_path, "    headers_env: OPENAPI_MCP_HEADERS\n", "")

    with pytest.raises(ConfigError, match="headers_env"):
        load_config(config)


def test_invalid_mcp_tool_labels_are_a_config_error(tmp_path):
    config = config_with(tmp_path, "effect: read", "effect: maybe")

    with pytest.raises(ConfigError, match="API-post-search"):
        load_config(config)


def test_an_enabled_server_without_its_token_names_the_env_var(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = config_with(tmp_path, NOTION_ENABLED, "    enabled: true")

    with pytest.raises(ConfigError, match="mcp server 'notion': NOTION_TOKEN is unset or empty"):
        app_from_config(config, environ={"GEMINI_API_KEY": "test-key"})


FAKE = REPO / "tests" / "fixtures" / "fake_mcp_server.py"


@pytest.mark.asyncio
async def test_configured_mcp_servers_register_their_labelled_tools(tmp_path):
    raw = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    raw["mcp_servers"]["fake"] = {
        "command": sys.executable,
        "args": [str(FAKE), str(tmp_path / "calls.jsonl")],
        "auth": {"scheme": "bearer", "key_env": "FAKE_TOKEN"},
        "headers_env": "FAKE_HEADERS",
        "tools": {
            "environment": {
                "resource": "workspace",
                "effect": "read",
                "exfil": False,
                "taints_context": True,
            }
        },
    }
    config = tmp_path / "agent.yaml"
    config.write_text(yaml.safe_dump(raw), encoding="utf-8")
    registry = CapabilityRegistry(warn=lambda message: None)

    servers = mcp_servers_from_config(load_config(config), registry, {"FAKE_TOKEN": "t-1"})
    # The disabled Notion server is not started.
    assert [server.name for server in servers] == ["fake"]
    async with servers[0]:
        result, is_error = await run_tool(registry, "fake_environment", {})

    assert [spec.name for spec in registry.specs()] == ["fake_environment"]
    assert is_error is False
    assert isinstance(result, dict)
    assert result["headers"] == {"Authorization": "Bearer t-1"}

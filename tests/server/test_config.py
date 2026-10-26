"""The app built from config/agent.example.yaml, as `task dev` builds it."""

import json
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from vessel_mind.agent.loop import run_tool
from vessel_mind.core.capabilities import CapabilityRegistry
from vessel_mind.core.credentials import BearerAuth
from vessel_mind.rag.corpus import Document
from vessel_mind.server.config import ConfigError, load_config
from vessel_mind.server.main import app_from_config, build_router, mcp_servers_from_config

REPO = Path(__file__).resolve().parent.parent.parent
EXAMPLE = REPO / "config" / "agent.example.yaml"


class FakeEmbedder:
    model_id = "fake/constant"
    dim = 2

    def __init__(self) -> None:
        self.embedded: list[str] = []

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        self.embedded.extend(texts)
        return [[1.0, 0.0] for _ in texts]

    def embed_query(self, text: str) -> list[float]:
        return [1.0, 0.0]


DOCS = [
    Document(
        source_name="fixture",
        repo="example/fixture",
        commit="c" * 40,
        path="README.md",
        text="# Fixture\n\nA document.\n",
    )
]


def offline(embedder: FakeEmbedder | None = None) -> dict:
    """The startup-ingest seams, faked: no model loads and no corpus is fetched."""
    return {"embedder": embedder or FakeEmbedder(), "load_documents": lambda: DOCS}


def test_the_example_config_serves_the_hosted_slow_path_model(tmp_path, monkeypatch):
    # The example's state file and index are relative to the working directory.
    monkeypatch.chdir(tmp_path)
    app = app_from_config(EXAMPLE, environ={"GEMINI_API_KEY": "test-key"}, **offline())

    with TestClient(app) as client:
        health = client.get("/health").json()

    # gemini-3.8-flash allows 20 free requests a day; the author pinned 3.5 instead.
    assert health == {"status": "ok", "model": "gemini-3.5-flash"}


def test_the_app_keeps_sessions_in_the_configured_state_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = tmp_path / "agent.yaml"
    state = tmp_path / "state" / "sessions.db"
    config.write_text(
        EXAMPLE.read_text(encoding="utf-8")
        + f"\nstate:\n  path: {state.as_posix()}\nsessions:\n  max_turns: 1\n",
        encoding="utf-8",
    )
    app = app_from_config(config, environ={"GEMINI_API_KEY": "test-key"}, **offline())

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200

    assert state.exists()
    loaded = load_config(config)
    assert loaded.sessions.max_turns == 1


def test_demo_mode_is_off_in_the_example_and_on_when_the_config_says_so(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert load_config(EXAMPLE).demo_mode is False
    config = config_with(tmp_path, "demo_mode: false", "demo_mode: true")

    app = app_from_config(config, environ={"GEMINI_API_KEY": "test-key"}, **offline())
    with TestClient(app) as client:
        page = client.get("/").text

    assert 'id="demo-mode"' in page


PUBLIC = REPO / "config" / "agent.public.example.yaml"


def test_the_public_host_flag_shows_the_free_tier_notice(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert load_config(EXAMPLE).public_host is False
    config = public_config(tmp_path)

    app = app_from_config(config, environ={"GEMINI_API_KEY": "test-key"}, **offline())
    with TestClient(app) as client:
        page = client.get("/").text

    assert 'id="free-tier"' in page
    assert 'id="demo-mode"' in page


def public_config(tmp_path, *, demo_mode: bool = True, max_turns: bool = True) -> Path:
    """The example with public_host on, and demo mode and the turn limit as given."""
    text = EXAMPLE.read_text(encoding="utf-8")
    for old, new in [
        ("public_host: false", "public_host: true"),
        ("demo_mode: false", f"demo_mode: {str(demo_mode).lower()}"),
        ("sessions:\n  max_turns: 30\n", "sessions:\n  max_turns: 30\n" if max_turns else ""),
    ]:
        assert old in text
        text = text.replace(old, new)
    config = tmp_path / "agent.yaml"
    config.write_text(text, encoding="utf-8")
    return config


def test_the_public_host_must_run_in_demo_mode(tmp_path):
    config = public_config(tmp_path, demo_mode=False)

    with pytest.raises(ConfigError, match="public_host requires demo_mode"):
        load_config(config)


def test_the_public_host_must_limit_the_turns_of_a_session(tmp_path):
    config = public_config(tmp_path, max_turns=False)

    with pytest.raises(ConfigError, match="public_host requires sessions.max_turns"):
        load_config(config)


def test_the_public_example_runs_routed_in_demo_mode_with_a_turn_limit():
    config = load_config(PUBLIC)

    assert config.configuration == "routed"
    assert (config.public_host, config.demo_mode) == (True, True)
    assert config.sessions.max_turns is not None
    # In the container, 127.0.0.1 is the container itself, not llama-server's host.
    assert "127.0.0.1" not in config.providers["local"].base_url


def test_the_public_example_keeps_notions_write_tools_labelled_so_proposals_show():
    public = load_config(PUBLIC).mcp_servers["notion"]
    example = load_config(EXAMPLE).mcp_servers["notion"]

    assert public.enabled is True
    assert public.tools == example.tools
    assert (public.command, public.args) == (example.command, example.args)


def test_the_example_config_bounds_turns_per_session_and_names_a_state_file():
    config = load_config(EXAMPLE)

    assert config.sessions.max_turns is not None
    assert config.state.path.name.endswith(".db")


def test_the_example_config_runs_hybrid_candidates_top_30_then_top_5():
    retrieval = load_config(EXAMPLE).retrieval

    assert (retrieval.candidates, retrieval.candidate_k, retrieval.top_k) == ("hybrid", 30, 5)
    assert retrieval.index == Path("data/index.sqlite")
    assert retrieval.index != load_config(EXAMPLE).state.path


@pytest.mark.parametrize("stage", ["vector", "keyword", "hybrid"])
def test_the_candidate_stage_is_selectable_in_config(tmp_path, stage):
    config = config_with(tmp_path, "candidates: hybrid", f"candidates: {stage}")

    assert load_config(config).retrieval.candidates == stage


def test_an_unknown_candidate_stage_is_refused(tmp_path):
    config = config_with(tmp_path, "candidates: hybrid", "candidates: bm25")

    with pytest.raises(ConfigError, match=r"retrieval\.candidates"):
        load_config(config)


def test_top_k_may_not_exceed_the_candidates(tmp_path):
    config = config_with(tmp_path, "top_k: 5", "top_k: 31")

    with pytest.raises(ConfigError, match="top_k"):
        load_config(config)


def test_the_app_ingests_the_corpus_into_the_configured_index_at_startup(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    index = tmp_path / "idx" / "index.sqlite"
    config = config_with(tmp_path, "index: data/index.sqlite", f"index: {index.as_posix()}")
    embedder = FakeEmbedder()

    with TestClient(app_from_config(config, environ={"GEMINI_API_KEY": "k"}, **offline(embedder))):
        pass

    assert index.exists()
    assert len(embedder.embedded) == 1  # the one chunk of the fake corpus


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


def test_the_example_defines_three_configurations_and_hosted_is_the_default():
    config = load_config(EXAMPLE)

    assert config.configuration == "hosted"
    assert sorted(config.configurations) == ["hosted", "local", "routed"]
    hosted, local, routed = (config.configurations[n] for n in ("hosted", "local", "routed"))
    assert (hosted.fast.model, hosted.slow.model) == ("gemini-3.5-flash-lite", "gemini-3.5-flash")
    # routed pairs the local small model with the hosted slow model.
    assert routed.fast == local.fast
    assert routed.slow == hosted.slow
    assert {local.fast.provider, local.slow.provider} == {"local"}
    assert config.providers["local"].auth.scheme == "none"


def test_the_router_is_on_in_the_example_with_a_classification_timeout():
    router = load_config(EXAMPLE).router

    assert router.enabled is True
    assert router.timeout > 0


@pytest.mark.parametrize(
    ("configuration", "fast", "slow"),
    [
        ("hosted", "gemini-3.5-flash-lite", "gemini-3.5-flash"),
        ("routed", "local-small", "gemini-3.5-flash"),
    ],
)
def test_the_router_pairs_the_selected_configurations_models(configuration, fast, slow):
    config = load_config(EXAMPLE)

    router = build_router(config, {"GEMINI_API_KEY": "k"}, configuration=configuration)

    assert (router.fast.id, router.slow.id, router.enabled) == (fast, slow, True)


def test_the_local_configuration_needs_no_key():
    router = build_router(load_config(EXAMPLE), {}, configuration="local")

    assert (router.fast.id, router.slow.id) == ("local-small", "local-large")


def test_the_router_can_be_turned_off_for_the_slow_only_baseline(tmp_path):
    assert (
        build_router(load_config(EXAMPLE), {"GEMINI_API_KEY": "k"}, enabled=False).enabled is False
    )
    config = config_with(tmp_path, "enabled: true", "enabled: false")
    assert build_router(load_config(config), {"GEMINI_API_KEY": "k"}).enabled is False


def test_an_undefined_configuration_is_a_config_error():
    with pytest.raises(ConfigError, match="'remote' is not defined"):
        build_router(load_config(EXAMPLE), {}, configuration="remote")


def test_app_from_config_serves_the_configuration_it_is_given(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    app = app_from_config(EXAMPLE, environ={}, configuration="local", **offline())

    with TestClient(app) as client:
        health = client.get("/health").json()

    assert health == {"status": "ok", "model": "local-large"}


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
        app_from_config(config, environ={"GEMINI_API_KEY": "test-key"}, **offline())


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

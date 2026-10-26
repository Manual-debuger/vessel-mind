"""The app built from config/agent.example.yaml, as `task dev` builds it."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from vessel_mind.server.config import ConfigError, load_config
from vessel_mind.server.main import app_from_config

EXAMPLE = Path(__file__).resolve().parent.parent.parent / "config" / "agent.example.yaml"


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

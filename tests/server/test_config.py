"""The app built from config/agent.example.yaml, as `task dev` builds it."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from vessel_mind.server.config import ConfigError, load_config
from vessel_mind.server.main import app_from_config

EXAMPLE = Path(__file__).resolve().parent.parent.parent / "config" / "agent.example.yaml"


def test_the_example_config_serves_the_hosted_slow_path_model():
    app = app_from_config(EXAMPLE, environ={"GEMINI_API_KEY": "test-key"})

    with TestClient(app) as client:
        health = client.get("/health").json()

    assert health == {"status": "ok", "model": "gemini-3.8-flash"}


def test_a_missing_key_names_the_env_var_and_nothing_else():
    with pytest.raises(ConfigError, match="GEMINI_API_KEY is not set"):
        app_from_config(EXAMPLE, environ={})


def test_config_holds_the_env_var_name_never_a_key(tmp_path):
    config = tmp_path / "agent.yaml"
    config.write_text(
        EXAMPLE.read_text(encoding="utf-8").replace(
            "api_key_env: GEMINI_API_KEY", "api_key: AIza-not-a-real-key"
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="api_key"):
        load_config(config)

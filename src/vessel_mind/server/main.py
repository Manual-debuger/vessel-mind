"""Builds the app from config and the environment. `task dev` runs app_from_config."""

import os
from collections.abc import Mapping
from pathlib import Path

from fastapi import FastAPI

from vessel_mind.agent.models.openai_compatible import OpenAICompatibleModel
from vessel_mind.server.app import create_app
from vessel_mind.server.config import ConfigError, ModelChoice, load_config

DEFAULT_CONFIG = Path("config/agent.yaml")
EXAMPLE_CONFIG = Path("config/agent.example.yaml")


def _config_path(environ: Mapping[str, str]) -> Path:
    if "VESSEL_MIND_CONFIG" in environ:
        return Path(environ["VESSEL_MIND_CONFIG"])
    return DEFAULT_CONFIG if DEFAULT_CONFIG.exists() else EXAMPLE_CONFIG


def app_from_config(path: Path | None = None, environ: Mapping[str, str] | None = None) -> FastAPI:
    environ = os.environ if environ is None else environ
    config = load_config(path or _config_path(environ))

    # The tracer answers every turn on the slow path; the router comes later.
    choice: ModelChoice = config.active.slow
    provider = config.providers[choice.provider]
    api_key = None
    if provider.api_key_env is not None:
        api_key = environ.get(provider.api_key_env)
        if not api_key:
            raise ConfigError(f"{provider.api_key_env} is not set")

    model = OpenAICompatibleModel(
        id=choice.model,
        base_url=provider.base_url,
        api_key=api_key,
        max_retries=config.retry.max_retries,
        backoff_base=config.retry.backoff_base,
        backoff_max=config.retry.backoff_max,
    )
    return create_app(model, system=config.agent.system, max_tokens=config.agent.max_tokens)

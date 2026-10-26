"""Builds the app from config and the environment. `task dev` runs app_from_config."""

import os
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path

from fastapi import FastAPI

from vessel_mind.agent.models.openai_compatible import OpenAICompatibleModel
from vessel_mind.agent.router import Router
from vessel_mind.core.chat import ChatModel
from vessel_mind.core.credentials import CredentialError, resolve_credential
from vessel_mind.rag.corpus import Document
from vessel_mind.rag.embed import Embedder, FastEmbedEmbedder
from vessel_mind.rag.ingest import corpus_loader
from vessel_mind.server.app import create_app
from vessel_mind.server.config import AgentConfig, ConfigError, ModelChoice, load_config
from vessel_mind.server.retrieval import RetrievalService, search_docs_registry

DEFAULT_CONFIG = Path("config/agent.yaml")
EXAMPLE_CONFIG = Path("config/agent.example.yaml")


def _config_path(environ: Mapping[str, str]) -> Path:
    if "VESSEL_MIND_CONFIG" in environ:
        return Path(environ["VESSEL_MIND_CONFIG"])
    return DEFAULT_CONFIG if DEFAULT_CONFIG.exists() else EXAMPLE_CONFIG


def _model(config: AgentConfig, choice: ModelChoice, environ: Mapping[str, str]) -> ChatModel:
    provider = config.providers[choice.provider]
    try:
        credential = resolve_credential(provider.auth, environ)
    except CredentialError as error:
        raise ConfigError(f"provider {choice.provider!r}: {error}") from None
    return OpenAICompatibleModel(
        id=choice.model,
        base_url=provider.base_url,
        credential=credential,
        max_retries=config.retry.max_retries,
        backoff_base=config.retry.backoff_base,
        backoff_max=config.retry.backoff_max,
    )


def build_router(
    config: AgentConfig,
    environ: Mapping[str, str],
    *,
    configuration: str | None = None,
    enabled: bool | None = None,
) -> Router:
    """The router over one configuration's models: the active one unless named.

    enabled overrides router.enabled; False is the eval's slow-only baseline.
    Raises ConfigError for an undefined configuration or an unset key.
    """
    pair = config.select(configuration)
    settings = config.router
    return Router(
        _model(config, pair.fast, environ),
        _model(config, pair.slow, environ),
        enabled=settings.enabled if enabled is None else enabled,
        timeout=settings.timeout,
        max_tokens=settings.max_tokens,
    )


def app_from_config(
    path: Path | None = None,
    environ: Mapping[str, str] | None = None,
    *,
    configuration: str | None = None,
    router: bool | None = None,
    embedder: Embedder | None = None,
    load_documents: Callable[[], Iterable[Document]] | None = None,
) -> FastAPI:
    """configuration and router override the config file's `configuration` and
    `router.enabled`, as build_router does.

    embedder and load_documents replace the configured embedding model and the
    pinned corpus; tests pass fakes, so no model loads and nothing is fetched."""
    environ = os.environ if environ is None else environ
    config = load_config(path or _config_path(environ))
    turn_router = build_router(config, environ, configuration=configuration, enabled=router)

    settings = config.retrieval
    if embedder is None:
        try:
            embedder = FastEmbedEmbedder(settings.embedding_model, cache_dir=settings.model_cache)
        except ValueError as error:
            raise ConfigError(f"retrieval.embedding_model: {error}") from None
    retrieval = RetrievalService(
        index_path=settings.index,
        load_documents=load_documents
        or corpus_loader(settings.corpus, repo_root=Path(), cache_dir=settings.tarball_cache),
        embedder=embedder,
        candidates=settings.candidates,
        candidate_k=settings.candidate_k,
        top_k=settings.top_k,
    )
    return create_app(
        turn_router.slow,
        router=turn_router,
        tools=search_docs_registry(retrieval),
        system=config.agent.system,
        max_tokens=config.agent.max_tokens,
        state_path=config.state.path,
        max_turns_per_session=config.sessions.max_turns,
        demo_mode=config.demo_mode,
        services=[retrieval],
    )

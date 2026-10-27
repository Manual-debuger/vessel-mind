"""Builds the app from config and the environment. `task dev` runs app_from_config."""

import os
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path

from fastapi import FastAPI

from vessel_mind.agent.mcp_client import McpServer
from vessel_mind.agent.models.openai_compatible import OpenAICompatibleModel
from vessel_mind.agent.router import Router
from vessel_mind.core.capabilities import CapabilityRegistry
from vessel_mind.core.chat import ChatModel
from vessel_mind.core.credentials import CredentialError, resolve_credential
from vessel_mind.rag.corpus import Document
from vessel_mind.rag.embed import Embedder, FastEmbedEmbedder
from vessel_mind.rag.ingest import corpus_loader
from vessel_mind.rag.rerank import make_reranker
from vessel_mind.rag.search import Reranker
from vessel_mind.server.app import create_app
from vessel_mind.server.config import AgentConfig, ConfigError, ModelChoice, load_config
from vessel_mind.server.retrieval import RetrievalService, search_docs_registry

DEFAULT_CONFIG = Path("config/agent.yaml")
EXAMPLE_CONFIG = Path("config/agent.example.yaml")


def _config_path(environ: Mapping[str, str]) -> Path:
    if "VESSEL_MIND_CONFIG" in environ:
        return Path(environ["VESSEL_MIND_CONFIG"])
    return DEFAULT_CONFIG if DEFAULT_CONFIG.exists() else EXAMPLE_CONFIG


def build_model(config: AgentConfig, choice: ModelChoice, environ: Mapping[str, str]) -> ChatModel:
    """One model from config, with its provider's key. Raises ConfigError for an unset key."""
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
        reasoning_effort=choice.reasoning_effort,
    )


def build_router(
    config: AgentConfig,
    environ: Mapping[str, str],
    *,
    configuration: str | None = None,
    enabled: bool | None = None,
    fallbacks: bool = True,
    wrap: Callable[[ChatModel], ChatModel] = lambda model: model,
) -> Router:
    """The router over one configuration's models: the active one unless named.

    enabled overrides router.enabled; False is the eval's slow-only baseline.
    fallbacks=False leaves out the paths' fallback lists. wrap is applied to each
    model, for example to pace its requests. Raises ConfigError for an undefined
    configuration or an unset key.
    """
    selected = config.select(configuration)
    settings = config.router

    def model(choice: ModelChoice) -> ChatModel:
        return wrap(build_model(config, choice, environ))

    return Router(
        model(selected.fast),
        model(selected.slow),
        enabled=settings.enabled if enabled is None else enabled,
        timeout=settings.timeout,
        max_tokens=settings.max_tokens,
        fast_fallbacks=[model(c) for c in selected.fast_fallbacks] if fallbacks else [],
        slow_fallbacks=[model(c) for c in selected.slow_fallbacks] if fallbacks else [],
    )


def app_from_config(
    path: Path | None = None,
    environ: Mapping[str, str] | None = None,
    *,
    configuration: str | None = None,
    router: bool | None = None,
    embedder: Embedder | None = None,
    load_documents: Callable[[], Iterable[Document]] | None = None,
    reranker: Reranker | None = None,
) -> FastAPI:
    """configuration and router override the config file's `configuration` and
    `router.enabled`, as build_router does.

    embedder, load_documents and reranker replace the configured embedding
    model, the pinned corpus and the configured reranker; tests pass fakes, so no
    model loads and nothing is fetched."""
    environ = os.environ if environ is None else environ
    config = load_config(path or _config_path(environ))
    turn_router = build_router(config, environ, configuration=configuration, enabled=router)

    settings = config.retrieval
    if embedder is None:
        try:
            embedder = FastEmbedEmbedder(settings.embedding_model, cache_dir=settings.model_cache)
        except ValueError as error:
            raise ConfigError(f"retrieval.embedding_model: {error}") from None
    if reranker is None:
        try:
            reranker = make_reranker(
                settings.reranker, settings.reranker_model, cache_dir=settings.model_cache
            )
        except ValueError as error:
            raise ConfigError(f"retrieval.reranker_model: {error}") from None
    retrieval = RetrievalService(
        index_path=settings.index,
        load_documents=load_documents
        or corpus_loader(settings.corpus, repo_root=Path(), cache_dir=settings.tarball_cache),
        embedder=embedder,
        candidates=settings.candidates,
        candidate_k=settings.candidate_k,
        top_k=settings.top_k,
        reranker=reranker,
    )
    registry = search_docs_registry(retrieval)
    # The MCP servers register their tools in the same registry when they start.
    mcp_servers = mcp_servers_from_config(config, registry, environ)
    return create_app(
        turn_router.slow,
        router=turn_router,
        tools=registry,
        system=config.agent.system,
        max_tokens=config.agent.max_tokens,
        state_path=config.state.path,
        max_turns_per_session=config.sessions.max_turns,
        max_turns_per_day=config.sessions.max_turns_per_day,
        demo_mode=config.demo_mode,
        public_host=config.public_host,
        services=[retrieval, *mcp_servers],
    )


def mcp_servers_from_config(
    config: AgentConfig, registry: CapabilityRegistry, environ: Mapping[str, str]
) -> list[McpServer]:
    """The enabled MCP servers, not yet started. Each registers its labelled tools
    in the registry when the app starts it."""
    servers: list[McpServer] = []
    for name, server in config.mcp_servers.items():
        if not server.enabled:
            continue
        try:
            credential = resolve_credential(server.auth, environ)
        except CredentialError as error:
            raise ConfigError(f"mcp server {name!r}: {error}") from None
        servers.append(
            McpServer(
                name,
                command=server.command,
                args=server.args,
                labels=server.tools,
                registry=registry,
                credential=credential,
                headers_env=server.headers_env if credential is not None else None,
            )
        )
    return servers

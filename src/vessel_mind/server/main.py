"""Builds the app from config and the environment. `task dev` runs app_from_config."""

import os
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path

from fastapi import FastAPI

from vessel_mind.agent.mcp_client import McpServer
from vessel_mind.agent.models.openai_compatible import OpenAICompatibleModel
from vessel_mind.core.capabilities import CapabilityRegistry
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


def app_from_config(
    path: Path | None = None,
    environ: Mapping[str, str] | None = None,
    *,
    embedder: Embedder | None = None,
    load_documents: Callable[[], Iterable[Document]] | None = None,
) -> FastAPI:
    """embedder and load_documents replace the configured embedding model and the
    pinned corpus; tests pass fakes, so no model loads and nothing is fetched."""
    environ = os.environ if environ is None else environ
    config = load_config(path or _config_path(environ))

    # The tracer answers every turn on the slow path; the router comes later.
    choice: ModelChoice = config.active.slow
    provider = config.providers[choice.provider]
    try:
        credential = resolve_credential(provider.auth, environ)
    except CredentialError as error:
        raise ConfigError(f"provider {choice.provider!r}: {error}") from None

    model = OpenAICompatibleModel(
        id=choice.model,
        base_url=provider.base_url,
        credential=credential,
        max_retries=config.retry.max_retries,
        backoff_base=config.retry.backoff_base,
        backoff_max=config.retry.backoff_max,
    )
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
    registry = search_docs_registry(retrieval)
    # The MCP servers register their tools in the same registry when they start.
    mcp_servers = mcp_servers_from_config(config, registry, environ)
    return create_app(
        model,
        tools=registry,
        system=config.agent.system,
        max_tokens=config.agent.max_tokens,
        state_path=config.state.path,
        max_turns_per_session=config.sessions.max_turns,
        demo_mode=config.demo_mode,
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

"""config/agent.yaml: providers, configurations and agent settings.

Config names the env var that holds a key; it never holds the key itself.
"""

import datetime
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from vessel_mind.core.credentials import ProviderAuth
from vessel_mind.rag.embed import DEFAULT_MODEL
from vessel_mind.rag.rerank import DEFAULT_RERANK_MODEL, RerankerProvider
from vessel_mind.rag.search import CANDIDATE_K, TOP_K, CandidateStage


class ConfigError(Exception):
    """The config file is missing, invalid or refers to an unset env var."""


class _Config(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Provider(_Config):
    """An OpenAI-compatible endpoint."""

    base_url: str
    # Required and never defaulted: a keyless server states {scheme: none}.
    auth: ProviderAuth


class ModelChoice(_Config):
    provider: str
    model: str
    # The date the model id was pinned, so results can be reproduced.
    chosen: datetime.date


class Configuration(_Config):
    """A named, fixed pair of models for the fast path and the slow path."""

    fast: ModelChoice
    slow: ModelChoice


class Retry(_Config):
    max_retries: int = Field(default=3, ge=0)
    backoff_base: float = Field(default=1.0, gt=0)
    backoff_max: float = Field(default=30.0, gt=0)


class Agent(_Config):
    system: str | None = None
    max_tokens: int | None = Field(default=None, gt=0)


class State(_Config):
    # The SQLite state file: sessions, history and proposals. A relative path is
    # relative to the server's working directory. Never the retrieval index's file.
    path: Path = Path("data/state.db")


class Sessions(_Config):
    # The turns one session may start, to protect the free quota. None: no limit.
    max_turns: int | None = Field(default=None, gt=0)


class Retrieval(_Config):
    """The search_docs pipeline (§5.6) and the index it reads. Relative paths are
    relative to the server's working directory."""

    index: Path = Path("data/index.sqlite")
    corpus: Path = Path("eval/corpus.yaml")
    candidates: CandidateStage = "hybrid"
    candidate_k: int = Field(default=CANDIDATE_K, gt=0)
    top_k: int = Field(default=TOP_K, gt=0)
    embedding_model: str = DEFAULT_MODEL
    # cpu: a fastembed cross-encoder reorders the candidates; none: no rerank stage.
    reranker: RerankerProvider = "cpu"
    reranker_model: str = DEFAULT_RERANK_MODEL
    model_cache: Path = Path(".cache/fastembed")
    tarball_cache: Path = Path(".cache/corpus")

    @model_validator(mode="after")
    def _top_k_within_candidates(self) -> "Retrieval":
        if self.top_k > self.candidate_k:
            raise ValueError(f"top_k {self.top_k} exceeds candidate_k {self.candidate_k}")
        return self


class AgentConfig(_Config):
    configuration: Literal["hosted", "local", "routed"]
    providers: dict[str, Provider]
    configurations: dict[str, Configuration]
    retry: Retry = Retry()
    agent: Agent = Agent()
    state: State = State()
    sessions: Sessions = Sessions()
    retrieval: Retrieval = Retrieval()
    # ADR 0002: on the public host, proposals are shown but every approval is refused.
    demo_mode: bool = False

    @model_validator(mode="after")
    def _references_resolve(self) -> "AgentConfig":
        if self.configuration not in self.configurations:
            raise ValueError(f"configuration {self.configuration!r} is not defined")
        for name, configuration in self.configurations.items():
            for path in (configuration.fast, configuration.slow):
                if path.provider not in self.providers:
                    raise ValueError(f"{name}: unknown provider {path.provider!r}")
        return self

    @property
    def active(self) -> Configuration:
        return self.configurations[self.configuration]


def load_config(path: Path) -> AgentConfig:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise ConfigError(f"{path}: {error}") from None
    try:
        return AgentConfig.model_validate(raw)
    except ValidationError as error:
        # The message lists locations and error types, never input values,
        # in case a key was pasted into the file by mistake.
        problems = "; ".join(
            f"{'.'.join(str(part) for part in e['loc'])}: {e['msg']}"
            for e in error.errors(include_input=False)
        )
        raise ConfigError(f"{path}: {problems}") from None

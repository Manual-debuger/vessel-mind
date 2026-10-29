"""config/agent.yaml: providers, configurations and agent settings.

Config names the env var that holds a key; it never holds the key itself.
"""

import datetime
from pathlib import Path, PurePath
from typing import Annotated, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from vessel_mind.agent.models.openai_compatible import ReasoningEffort
from vessel_mind.core.capabilities import CapabilityLabels
from vessel_mind.core.credentials import EnvVarName, NoAuth, ProviderAuth
from vessel_mind.rag.embed import DEFAULT_MODEL
from vessel_mind.rag.rerank import DEFAULT_RERANK_MODEL, RerankerProvider
from vessel_mind.rag.search import CANDIDATE_K, TOP_K, CandidateStage

# The configurations (GLOSSARY.md), each a fixed pair of models.
ConfigurationName = Literal["hosted", "local", "routed"]


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
    # Sent as the request's reasoning_effort when set; "none" turns thinking off.
    # Unset: the provider's default.
    reasoning_effort: ReasoningEffort | None = None


class Configuration(_Config):
    """A named, fixed pair of models for the fast path and the slow path."""

    fast: ModelChoice
    slow: ModelChoice
    # Each path's fallback list (spec §5.1), tried in order when the path's model
    # still fails after its retries. The route event names the model that answered.
    fast_fallbacks: list[ModelChoice] = []
    slow_fallbacks: list[ModelChoice] = []

    def models(self) -> list[ModelChoice]:
        """Every model the configuration may call."""
        return [self.fast, self.slow, *self.fast_fallbacks, *self.slow_fallbacks]


class RouterSettings(_Config):
    """The router (§5.7). enabled: false sends every turn to the slow path."""

    enabled: bool = True
    # Seconds the fast model may take to classify a turn before it goes slow.
    timeout: float = Field(default=10.0, gt=0)
    # The classification's token budget. Gemini counts thinking tokens in it.
    max_tokens: int | None = Field(default=None, gt=0)
    # Seconds a model that failed before answering is asked after its fallbacks,
    # when its failure carried no Retry-After (spec §5.1).
    cooldown: float = Field(default=300.0, gt=0)


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
    path: Path = Path("data/state/state.db")


class TelemetrySettings(_Config):
    # The JSONL file each turn's route decision and summary are appended to. A
    # relative path is relative to the server's working directory. None: none.
    path: Path | None = Path("data/state/telemetry.jsonl")


class Sessions(_Config):
    # The turns one session may start, to protect the free quota. None: no limit.
    max_turns: int | None = Field(default=None, gt=0)
    # The turns all sessions together may start in one UTC day: a new session
    # does not reset it. None: no limit.
    max_turns_per_day: int | None = Field(default=None, gt=0)


class Console(_Config):
    """What the console shows a visitor beside the question box."""

    # The Notion page the agent reads, published to the web, so a visitor can
    # check its answers. None: no link.
    demo_page_url: Annotated[str, Field(pattern=r"^https://\S+$")] | None = None
    # Questions a visitor can click to fill the question box.
    example_questions: list[Annotated[str, Field(min_length=1, max_length=8000)]] = Field(
        default=[], max_length=10
    )


# Commands that fetch a package at start instead of running a pinned one.
_PACKAGE_RUNNERS = frozenset({"npx", "npm", "pnpm", "pnpx", "yarn", "bunx", "uvx"})


class McpServerConfig(_Config):
    """An MCP server the runtime starts over stdio, and the labels of its tools."""

    # Started as `command args...`, never through a package runner: the package is
    # pinned by a committed lockfile (deploy/notion-mcp/) and run with node.
    command: str = Field(min_length=1)
    args: list[str] = []
    # False keeps the server in the file without starting it.
    enabled: bool = True
    # Required and never defaulted, as for providers.
    auth: ProviderAuth
    # The env var that carries the credential's headers, as a JSON object, to the
    # server process. Required unless auth is {scheme: none}.
    headers_env: EnvVarName | None = None
    # The server's own tool names and their labels. A tool not named here is not
    # registered: labels are never defaulted.
    tools: dict[str, CapabilityLabels] = {}

    @field_validator("command")
    @classmethod
    def _no_package_runner(cls, command: str) -> str:
        if PurePath(command).name.removesuffix(".cmd").lower() in _PACKAGE_RUNNERS:
            raise ValueError("start the pinned package with node, never a package runner")
        return command

    @model_validator(mode="after")
    def _credential_has_a_carrier(self) -> "McpServerConfig":
        if isinstance(self.auth, NoAuth) != (self.headers_env is None):
            raise ValueError("headers_env is required with a credential, and only with one")
        return self


class Retrieval(_Config):
    """The search_docs pipeline (§5.6) and the index it reads. Relative paths are
    relative to the server's working directory."""

    index: Path = Path("data/index.sqlite")
    corpus: Path = Path("eval/corpus.yaml")
    candidates: CandidateStage = "hybrid"
    # The reranker takes at most CANDIDATE_K (spec §5.6): a larger value is refused here,
    # not by the first search.
    candidate_k: int = Field(default=CANDIDATE_K, gt=0, le=CANDIDATE_K)
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


class ModelPrice(_Config):
    """A model's paid list price on a date (spec §5.8). Nobody paid it: the eval
    multiplies tokens by it so configurations can be compared on cost."""

    input_usd_per_mtok: float = Field(ge=0)
    # Thinking tokens are billed as output tokens.
    output_usd_per_mtok: float = Field(ge=0)
    # The date the price was read from the source.
    as_of: datetime.date
    source: str = Field(pattern=r"^https://")


class EvalSettings(_Config):
    """The end-to-end eval (`task eval -- --config NAME`)."""

    # Any OpenAI-compatible model from `providers`: changing it is a config change.
    judge: ModelChoice
    # The judge's token budget per answer; None leaves it to the provider.
    judge_max_tokens: int | None = Field(default=None, gt=0)
    # Seconds between the starts of two requests, judge included, to stay inside
    # the free tier's requests-per-minute limit.
    request_interval: float = Field(default=7.0, ge=0)
    # A run stops after this many questions fail in a row (a daily quota is
    # spent); the next run resumes where it stopped.
    max_consecutive_failures: int = Field(default=3, gt=0)
    # Paid list prices by model id. A model with no price is reported as such,
    # never estimated.
    prices: dict[str, ModelPrice] = {}
    # Providers that run on the author's own hardware: their models cost $0,
    # and the results say that GPU electricity is not measured.
    self_hosted_providers: list[str] = []
    # Configurations that are not run, with the reason the results show.
    not_run: dict[ConfigurationName, str] = {}


class AgentConfig(_Config):
    configuration: ConfigurationName
    providers: dict[str, Provider]
    configurations: dict[str, Configuration]
    router: RouterSettings = RouterSettings()
    retry: Retry = Retry()
    agent: Agent = Agent()
    state: State = State()
    sessions: Sessions = Sessions()
    telemetry: TelemetrySettings = TelemetrySettings()
    retrieval: Retrieval = Retrieval()
    # ADR 0002: on the public host, proposals are shown but every approval is refused.
    demo_mode: bool = False
    # The app is served on the public hostname (ADR 0002): the console shows the
    # free-tier notice. It requires demo mode, a per-session and a daily turn limit.
    public_host: bool = False
    console: Console = Console()
    # MCP servers by name; the model sees each tool as '<name>_<tool>'.
    mcp_servers: dict[
        Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,19}$")], McpServerConfig
    ] = {}
    # The end-to-end eval's judge, prices and pacing. Only the eval reads it.
    eval: EvalSettings | None = None

    @model_validator(mode="after")
    def _references_resolve(self) -> "AgentConfig":
        if self.configuration not in self.configurations:
            raise ValueError(f"configuration {self.configuration!r} is not defined")
        for name, configuration in self.configurations.items():
            for choice in configuration.models():
                if choice.provider not in self.providers:
                    raise ValueError(f"{name}: unknown provider {choice.provider!r}")
        if self.eval is not None:
            if self.eval.judge.provider not in self.providers:
                raise ValueError(f"eval.judge: unknown provider {self.eval.judge.provider!r}")
            for name in self.eval.self_hosted_providers:
                if name not in self.providers:
                    raise ValueError(f"eval.self_hosted_providers: unknown provider {name!r}")
            for name in self.eval.not_run:
                if name not in self.configurations:
                    raise ValueError(f"eval.not_run: configuration {name!r} is not defined")
        return self

    @model_validator(mode="after")
    def _public_host_is_guarded(self) -> "AgentConfig":
        # ADR 0002: on the public hostname no approval runs and the free quota is
        # protected per session. A missing guard is a config error, not a default.
        if self.public_host and not self.demo_mode:
            raise ValueError("public_host requires demo_mode: true")
        if self.public_host and self.sessions.max_turns is None:
            raise ValueError("public_host requires sessions.max_turns")
        if self.public_host and self.sessions.max_turns_per_day is None:
            raise ValueError("public_host requires sessions.max_turns_per_day")
        return self

    def select(self, name: str | None = None) -> Configuration:
        """The named configuration, or the active one when name is None."""
        name = self.configuration if name is None else name
        if name not in self.configurations:
            raise ConfigError(f"configuration {name!r} is not defined")
        return self.configurations[name]


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

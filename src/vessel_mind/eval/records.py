"""What an end-to-end run stores: one file per row, eval/results/end_to_end-<row>.json.

A row is a configuration with the router on (`hosted`, `local`, `routed`) or
off (`slow-only` is `hosted` with the router off). The file is rewritten after
every question, so a run stopped by a quota or by Ctrl-C resumes where it
stopped. A row that is not run stores its reason instead.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from vessel_mind.core.chat import Usage
from vessel_mind.core.events import Route, RouteReason
from vessel_mind.eval.golden import Lang
from vessel_mind.eval.judge import Verdict
from vessel_mind.server.config import ConfigurationName, ModelPrice

FILE_PREFIX = "end_to_end-"


@dataclass(frozen=True)
class Row:
    """A row of table 2 (GLOSSARY.md): a configuration, with its router on or off."""

    configuration: ConfigurationName
    router: bool = True

    @property
    def name(self) -> str:
        """The row's name in the table and in its file name. Table 2 calls hosted's
        router-off row plain `slow-only`; any other is NAME-slow-only."""
        if self.router:
            return self.configuration
        return "slow-only" if self.configuration == "hosted" else f"{self.configuration}-slow-only"

    @property
    def command(self) -> str:
        """The command that runs the row."""
        return f"task eval -- --config {self.configuration}" + (
            "" if self.router else " --no-router"
        )


# The rows of table 2, in order. Any other stored row follows them.
ROW_ORDER = (Row("hosted"), Row("hosted", router=False), Row("local"), Row("routed"))


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Turn(_Model):
    """One question's turn, read from its protocol events."""

    route: Route | None = None
    reason: RouteReason | None = None
    router_model: str | None = None
    answer_model: str | None = None
    # router_usage is the classification's; answer_usage is done.usage.
    router_usage: Usage = Usage()
    answer_usage: Usage = Usage()
    answer: str = ""
    # Each successful search_docs call's sources, best first, in call order.
    searches: tuple[tuple[str, ...], ...] = ()
    # The distinct sources the answer cites ([n] mapped by the citation rule).
    citations: tuple[str, ...] = ()
    # Wall time of the turn, less the time it waited for the pacer.
    latency_ms: float = 0.0
    # Model requests the turn made (classification included).
    requests: int = 0
    # Set when the turn failed: the question counts as failed, and is run again
    # by the next run of the row.
    error: str | None = None


class Judged(_Model):
    """The judge's verdicts on one answer, or why it could not give them."""

    model: str
    verdicts: tuple[Verdict, ...] | None = None
    error: str | None = None


class QuestionRecord(_Model):
    id: str
    lang: Lang
    question: str
    key_points: tuple[str, ...]
    expected_sources: tuple[str, ...]
    expected_route: Route
    turn: Turn
    judge: Judged | None = None

    @property
    def failed(self) -> bool:
        return self.turn.error is not None

    @property
    def judged(self) -> bool:
        return self.judge is not None and self.judge.verdicts is not None

    @property
    def score(self) -> float | None:
        """The answer score: the share of key points the judge marks covered."""
        if self.judge is None or self.judge.verdicts is None:
            return None
        verdicts = self.judge.verdicts
        return sum(v.covered for v in verdicts) / len(verdicts)


class RowResults(_Model):
    status: Literal["run"] = "run"
    row: str
    configuration: ConfigurationName
    router: bool
    fast_model: str
    slow_model: str
    generated: date
    golden_path: str
    golden_sha256: str
    questions_total: int
    top_k: int
    judge_model: str
    # The list prices in config when the row ran, by model id.
    prices: dict[str, ModelPrice]
    # Models on the author's own hardware: $0, electricity not measured.
    self_hosted_models: tuple[str, ...]
    records: tuple[QuestionRecord, ...]
    # Why the last run stopped before the end, if it did.
    stopped: str | None = None


class NotRun(_Model):
    status: Literal["not_run"] = "not_run"
    row: str
    configuration: ConfigurationName
    fast_model: str
    slow_model: str
    reason: str
    router: bool = True


StoredRow = Annotated[RowResults | NotRun, Field(discriminator="status")]
_stored: TypeAdapter[RowResults | NotRun] = TypeAdapter(StoredRow)


def row_path(results_dir: Path, row: str) -> Path:
    return results_dir / f"{FILE_PREFIX}{row}.json"


def read_row(path: Path) -> RowResults | NotRun:
    return _stored.validate_json(path.read_bytes())


def write_row(results_dir: Path, row: RowResults | NotRun) -> Path:
    """Writes the row's file whole, through a temporary file, so an interrupted
    write never leaves half a file."""
    results_dir.mkdir(parents=True, exist_ok=True)
    path = row_path(results_dir, row.row)
    partial = path.with_suffix(".json.partial")
    partial.write_text(row.model_dump_json(indent=2) + "\n", encoding="utf-8")
    partial.replace(path)
    return path


def load_rows(results_dir: Path) -> dict[str, RowResults | NotRun]:
    """Every stored row, in table order."""
    found = {
        row.row: row
        for row in (read_row(p) for p in sorted(results_dir.glob(f"{FILE_PREFIX}*.json")))
    }
    return {row.name: found[row.name] for row in ordered(row_of(r) for r in found.values())}


def row_of(stored: RowResults | NotRun) -> Row:
    return Row(stored.configuration, stored.router)


def ordered(rows: Iterable[Row]) -> list[Row]:
    """Rows in table order, each once."""
    rows = set(rows)
    rest = sorted((r for r in rows if r not in ROW_ORDER), key=lambda r: r.name)
    return [r for r in ROW_ORDER if r in rows] + rest

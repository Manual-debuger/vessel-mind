"""The frozen golden set (eval/golden.yaml; changes follow eval/golden-changes.md).

The file holds two lists, ``questions`` (English and unanswerable) and
``zh_hant_questions`` (Traditional Chinese). The eval reads both, in file
order. An expected source is ``<source name>/<path>``, without a heading
anchor; an unanswerable question has none.
"""

import hashlib
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from vessel_mind.core.events import Route

Lang = Literal["en", "zh-Hant"]


class GoldenSetError(Exception):
    """The golden set cannot be read, or it does not match the corpus."""


class GoldenQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z]+-\d+$")
    question: str = Field(min_length=1)
    lang: Lang
    expected_sources: tuple[str, ...]
    key_points: tuple[str, ...] = Field(min_length=1, max_length=4)
    expected_route: Route
    rationale: str
    drafted_by: str | None = None
    reviewed_by_author: bool | None = None

    @property
    def answerable(self) -> bool:
        return bool(self.expected_sources)


class _File(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    questions: tuple[GoldenQuestion, ...] = ()
    zh_hant_questions: tuple[GoldenQuestion, ...] = ()


@dataclass(frozen=True)
class GoldenSet:
    path: Path
    sha256: str  # of the file's bytes: results name the exact set they measured
    questions: tuple[GoldenQuestion, ...]

    def check_sources(self, documents: set[str]) -> None:
        """Raise GoldenSetError when an expected source is not a corpus document
        (``<source name>/<path>``) at the pin."""
        missing = sorted(
            f"{q.id}: {source}"
            for q in self.questions
            for source in q.expected_sources
            if source not in documents
        )
        if missing:
            raise GoldenSetError(
                f"{self.path}: expected sources missing from the pinned corpus: "
                + "; ".join(missing)
            )


def load_golden(path: Path) -> GoldenSet:
    try:
        data = path.read_bytes()
        parsed = _File.model_validate(yaml.safe_load(data))
    except (OSError, yaml.YAMLError) as exc:
        raise GoldenSetError(f"{path}: {exc}") from exc
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in e['loc'])}: {e['msg']}"
            for e in exc.errors(include_input=False)
        )
        raise GoldenSetError(f"{path}: {problems}") from None
    questions = parsed.questions + parsed.zh_hant_questions
    repeated = sorted(id for id, n in Counter(q.id for q in questions).items() if n > 1)
    if repeated:
        raise GoldenSetError(f"{path}: repeated question ids: {', '.join(repeated)}")
    return GoldenSet(path=path, sha256=hashlib.sha256(data).hexdigest(), questions=questions)

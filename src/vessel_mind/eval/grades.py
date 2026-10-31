"""The author's hand grades of sampled answers, and their agreement with the judge.

After a run, ``task eval -- --grade`` picks GRADE_SAMPLE judged answers from
the stored rows, at random with the fixed seed GRADE_SEED, shows the author
each one (never the judge's verdicts) and asks, per key point, whether the
answer covers it. Each graded answer is one line of eval/human_grades.jsonl,
kept with the SHA-256 of the answer it grades: a grade of an answer that a
later run replaced is not counted.
"""

import hashlib
import random
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, date, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from vessel_mind.eval.records import NotRun, QuestionRecord, RowResults, load_rows

GRADES_FILE = "human_grades.jsonl"
GRADE_SAMPLE = 10
GRADE_SEED = 15


class HumanGrade(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    row: str
    id: str
    answer_sha256: str
    # One per key point, in the golden set's order.
    covered: tuple[bool, ...]
    graded: date


def grades_path_for(results_dir: Path) -> Path:
    """eval/human_grades.jsonl, beside eval/results/."""
    return results_dir.parent / GRADES_FILE


def answer_sha256(answer: str) -> str:
    return hashlib.sha256(answer.encode("utf-8")).hexdigest()


def load_grades(path: Path) -> list[HumanGrade]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [HumanGrade.model_validate_json(line) for line in lines if line.strip()]


def sample_answers(
    rows: Mapping[str, RowResults | NotRun], *, n: int = GRADE_SAMPLE, seed: int = GRADE_SEED
) -> list[tuple[str, QuestionRecord]]:
    """n judged answers, (row, record), chosen at random with a fixed seed."""
    candidates = sorted(
        (
            (row.row, record)
            for row in rows.values()
            if isinstance(row, RowResults)
            for record in row.records
            if record.judged
        ),
        key=lambda pair: (pair[0], pair[1].id),
    )
    # A reproducible sample, not a secret: the seed is fixed on purpose.
    return random.Random(seed).sample(candidates, min(n, len(candidates)))  # noqa: S311


def grade(
    results_dir: Path,
    *,
    ask: Callable[[str], str] = input,
    show: Callable[[str], None] = print,
    grades_path: Path | None = None,
    today: date | None = None,
) -> int:
    """Asks the author to grade each sampled answer not graded yet; appends each
    grade as it is given. Answering q stops. Returns the number graded now."""
    path = grades_path or grades_path_for(results_dir)
    done = {(g.row, g.id, g.answer_sha256) for g in load_grades(path)}
    sample = sample_answers(load_rows(results_dir))
    todo = [
        (r, rec) for r, rec in sample if (r, rec.id, answer_sha256(rec.turn.answer)) not in done
    ]
    show(f"{len(sample)} sampled answers; {len(todo)} to grade.")
    graded = 0
    for number, (row, record) in enumerate(todo, start=1):
        show("")
        show(f"--- {number} of {len(todo)}: row {row}, question {record.id} ---")
        show(f"Question: {record.question}")
        show("Answer:")
        show(record.turn.answer or "(empty)")
        covered: list[bool] = []
        for n, key_point in enumerate(record.key_points, start=1):
            show(f"Key point {n}: {key_point}")
            answer = _yes_no(ask, "Does the answer cover it? [y/n, q to stop] ")
            if answer is None:
                return graded
            covered.append(answer)
        line = HumanGrade(
            row=row,
            id=record.id,
            answer_sha256=answer_sha256(record.turn.answer),
            covered=tuple(covered),
            graded=today or datetime.now(UTC).date(),
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as file:
            file.write(line.model_dump_json() + "\n")
        graded += 1
    return graded


def _yes_no(ask: Callable[[str], str], prompt: str) -> bool | None:
    while True:
        reply = ask(prompt).strip().lower()
        if reply in ("y", "yes"):
            return True
        if reply in ("n", "no"):
            return False
        if reply in ("q", "quit"):
            return None


def agreement_lines(
    rows: Mapping[str, RowResults | NotRun], grades: list[HumanGrade]
) -> Iterable[str]:
    yield "### Judge–human agreement"
    yield ""
    if not grades:
        yield (
            f"*Not graded yet*: after a run, `task eval -- --grade` shows the author "
            f"{GRADE_SAMPLE} answers chosen at random (seed {GRADE_SEED}) and stores the "
            f"author's grades in `eval/{GRADES_FILE}`."
        )
        return
    records = {
        (row.row, record.id): record
        for row in rows.values()
        if isinstance(row, RowResults)
        for record in row.records
    }
    pairs: list[tuple[bool, bool]] = []
    judge_scores: list[float] = []
    human_scores: list[float] = []
    stale = 0
    for g in grades:
        record = records.get((g.row, g.id))
        if (
            record is None
            or record.judge is None
            or record.judge.verdicts is None
            or answer_sha256(record.turn.answer) != g.answer_sha256
            or len(g.covered) != len(record.judge.verdicts)
        ):
            stale += 1
            continue
        judged = [v.covered for v in record.judge.verdicts]
        pairs.extend(zip(judged, g.covered, strict=True))
        judge_scores.append(sum(judged) / len(judged))
        human_scores.append(sum(g.covered) / len(g.covered))
    if pairs:
        agree = sum(j == h for j, h in pairs)
        yield (
            f"The author graded {len(human_scores)} answers ({len(pairs)} key points) by hand, "
            f"chosen at random (seed {GRADE_SEED}) from the judged answers. Judge and author "
            f"agree on {agree} of {len(pairs)} key points ({agree / len(pairs):.2f}). Mean "
            f"answer score on these answers: judge {sum(judge_scores) / len(judge_scores):.2f}, "
            f"author {sum(human_scores) / len(human_scores):.2f}."
        )
    else:
        yield "No stored grade matches a current answer."
    if stale:
        yield ""
        graded = {(g.row, g.id, g.answer_sha256) for g in grades}
        ungraded = any(
            (row, record.id, answer_sha256(record.turn.answer)) not in graded
            for row, record in sample_answers(rows)
        )
        hint = " Run `task eval -- --grade` to grade the new sample." if ungraded else ""
        yield (
            f"{stale} stored grades are for answers a later run replaced; they are not counted."
            + hint
        )

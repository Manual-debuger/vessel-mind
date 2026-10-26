"""Table 2, end to end (spec §5.8): each golden question as one turn through the router.

A turn runs exactly as the server runs it (agent.router over the search_docs
registry), from an empty history. The judge then grades the answer's key
points. Every request takes a slot from one Pacer, so a run stays inside the
free tier's request rate, and the row's file is rewritten after each question:

- a question whose turn failed is run again by the next run of the row;
- an answer the judge could not grade is graded again, without a new turn;
- after max_consecutive_failures questions fail in a row (a daily quota is
  spent), the run stops, and the next run resumes there.
"""

import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from vessel_mind.agent.router import Router
from vessel_mind.core.capabilities import CapabilityRegistry
from vessel_mind.core.chat import Usage
from vessel_mind.core.events import (
    AgentEvent,
    DoneEvent,
    ErrorEvent,
    RouteEvent,
    TextEvent,
)
from vessel_mind.eval.citations import cited_sources, search_sources
from vessel_mind.eval.golden import GoldenQuestion, GoldenSet
from vessel_mind.eval.judge import Judge, JudgeError
from vessel_mind.eval.pacing import Pacer
from vessel_mind.eval.records import (
    Graded,
    NotRun,
    QuestionRecord,
    RowResults,
    Turn,
    read_row,
    row_path,
    write_row,
)
from vessel_mind.eval.results import write_results
from vessel_mind.server.config import ModelPrice


class ResumeError(Exception):
    """The stored row was measured with other inputs; resuming would mix them."""


@dataclass(frozen=True)
class RowSpec:
    name: str
    configuration: str
    router: bool
    fast_model: str
    slow_model: str
    prices: Mapping[str, ModelPrice]
    self_hosted_models: tuple[str, ...]


def store_not_run(results_dir: Path, not_run: NotRun) -> Path:
    """Records that a row is not run, and why; then regenerates results.md.
    Stored results of that row are replaced: they no longer describe the config."""
    write_row(results_dir, not_run)
    return write_results(results_dir)


async def run_end_to_end(
    *,
    golden: GoldenSet,
    spec: RowSpec,
    router: Router,
    tools: CapabilityRegistry,
    judge: Judge,
    pacer: Pacer,
    results_dir: Path,
    top_k: int,
    system: str | None = None,
    max_tokens: int | None = None,
    clock=time.perf_counter,
    today: date | None = None,
    fresh: bool = False,
    max_consecutive_failures: int = 3,
) -> RowResults:
    """Runs the row's missing and failed questions, stores the row and regenerates
    results.md. Raises ResumeError when the stored row used other inputs and
    fresh is False."""
    records: dict[str, QuestionRecord] = {}
    path = row_path(results_dir, spec.name)
    if not fresh and path.exists():
        stored = read_row(path)
        if isinstance(stored, RowResults):
            _check_resumable(stored, golden, spec, judge.model.id, top_k)
            records = {r.id: r for r in stored.records}

    def row(stopped: str | None = None) -> RowResults:
        return RowResults(
            row=spec.name,
            configuration=spec.configuration,
            router=spec.router,
            fast_model=spec.fast_model,
            slow_model=spec.slow_model,
            generated=today or datetime.now(UTC).date(),
            golden_path=golden.path.as_posix(),
            golden_sha256=golden.sha256,
            questions_total=len(golden.questions),
            top_k=top_k,
            judge_model=judge.model.id,
            prices=dict(spec.prices),
            self_hosted_models=spec.self_hosted_models,
            records=tuple(records[q.id] for q in golden.questions if q.id in records),
            stopped=stopped,
        )

    failures = 0
    stopped: str | None = None
    for question in golden.questions:
        record = records.get(question.id)
        if record is None or record.failed:
            turn = await _turn(question, router, tools, pacer, system, max_tokens, clock)
            record = _record(question, turn)
        if not record.failed and not record.judged:
            record = record.model_copy(update={"judge": await _grade(record, judge)})
        records[question.id] = record
        write_row(results_dir, row())
        if record.failed or not record.judged:
            failures += 1
            if failures >= max_consecutive_failures:
                cause = record.turn.error or (record.judge.error if record.judge else None)
                stopped = (
                    f"stopped after {failures} questions in a row failed (last: {cause}); "
                    "run the same command again to resume"
                )
                break
        else:
            failures = 0

    results = row(stopped)
    write_row(results_dir, results)
    write_results(results_dir)
    return results


def _check_resumable(
    stored: RowResults, golden: GoldenSet, spec: RowSpec, judge_model: str, top_k: int
) -> None:
    changed = [
        what
        for what, before, now in (
            ("golden set", stored.golden_sha256, golden.sha256),
            ("fast model", stored.fast_model, spec.fast_model),
            ("slow model", stored.slow_model, spec.slow_model),
            ("router", stored.router, spec.router),
            ("judge", stored.judge_model, judge_model),
            ("top_k", stored.top_k, top_k),
        )
        if before != now
    ]
    if changed:
        raise ResumeError(
            f"row {spec.name!r}: the stored results used another {', '.join(changed)}; "
            "run with --fresh to discard them and start the row again"
        )


async def _turn(
    question: GoldenQuestion,
    router: Router,
    tools: CapabilityRegistry,
    pacer: Pacer,
    system: str | None,
    max_tokens: int | None,
    clock,
) -> Turn:
    # Wait for a free slot before the clock starts: the classification has a
    # timeout, and pacing is not the turn's latency.
    await pacer.ready()
    waited, requests = pacer.waited, pacer.requests
    events: list[AgentEvent] = []
    error: str | None = None
    start = clock()
    try:
        async for event in router.run(
            [], question.question, tools=tools, system=system, max_tokens=max_tokens
        ):
            events.append(event)
    except Exception as exc:  # noqa: BLE001 - one failed question must not end the run
        error = f"{type(exc).__name__}: {exc}"
    elapsed = clock() - start - (pacer.waited - waited)

    route = next((e for e in events if isinstance(e, RouteEvent)), None)
    done = next((e for e in events if isinstance(e, DoneEvent)), None)
    failure = next((e for e in events if isinstance(e, ErrorEvent)), None)
    if error is None and failure is not None:
        error = f"{failure.code}: {failure.message}"
    if error is None and done is None:
        error = "the turn ended without done"
    return Turn(
        route=route.route if route else None,
        reason=route.reason if route else None,
        router_model=route.router_model if route else None,
        answer_model=route.model if route else None,
        router_usage=route.router_usage if route else Usage(),
        answer_usage=done.usage if done else Usage(),
        answer="".join(e.text for e in events if isinstance(e, TextEvent)),
        searches=search_sources(events),
        citations=cited_sources(events),
        latency_ms=elapsed * 1000,
        requests=pacer.requests - requests,
        error=error,
    )


def _record(question: GoldenQuestion, turn: Turn) -> QuestionRecord:
    return QuestionRecord(
        id=question.id,
        lang=question.lang,
        question=question.question,
        key_points=question.key_points,
        expected_sources=question.expected_sources,
        expected_route=question.expected_route,
        turn=turn,
    )


async def _grade(record: QuestionRecord, judge: Judge) -> Graded:
    try:
        verdicts = await judge.grade(record.question, record.key_points, record.turn.answer)
    except JudgeError as error:
        return Graded(model=judge.model.id, error=str(error))
    return Graded(model=judge.model.id, verdicts=verdicts)

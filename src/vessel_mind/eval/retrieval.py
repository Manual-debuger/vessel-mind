"""Table 1, the retrieval ablation (spec §5.8): no LLM calls.

Each row runs the same pipeline the server runs (rag.search), with one stage
changed: the candidate stage, and whether the cross-encoder reranks. Every
question is searched once per row and timed; recall and MRR are scored on the
questions that have expected sources, latency on all of them. The first
question is searched once per row before timing, so a model's load is not
counted as a search.
"""

import time
from collections.abc import Callable, Sequence
from datetime import date

from pydantic import BaseModel, ConfigDict

from vessel_mind.eval.golden import GoldenQuestion, Lang
from vessel_mind.eval.metrics import percentile, recall_at_k, reciprocal_rank
from vessel_mind.rag.embed import Embedder
from vessel_mind.rag.search import CandidateStage, Reranker, SearchPipeline
from vessel_mind.rag.store import IndexStore

# (row name, candidate stage, reranked)
ROWS: tuple[tuple[str, CandidateStage, bool], ...] = (
    ("vector", "vector", False),
    ("keyword", "keyword", False),
    ("hybrid", "hybrid", False),
    ("hybrid + rerank", "hybrid", True),
)
LANGS: tuple[Lang, ...] = ("en", "zh-Hant")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Pin(_Model):
    name: str
    repo: str
    commit: str


class LanguageScores(_Model):
    questions: int  # with expected sources: the ones recall and MRR average over
    timed: int  # every question in the language, unanswerable ones too
    recall_at_k: float | None
    mrr: float | None
    latency_ms_p50: float | None
    latency_ms_p95: float | None


class RowScores(_Model):
    name: str
    stage: CandidateStage
    reranked: bool
    languages: dict[Lang, LanguageScores]


class QuestionResult(_Model):
    id: str
    lang: Lang
    row: str
    sources: tuple[str, ...]  # the top k, best first
    recall: float | None
    reciprocal_rank: float | None
    latency_ms: float


class RetrievalAblation(_Model):
    """What --retrieval-only stores in eval/results/retrieval.json."""

    generated: date
    golden_path: str
    golden_sha256: str
    corpus: tuple[Pin, ...]
    chunks: int
    embedding_model: str
    reranker_model: str
    candidate_k: int
    top_k: int
    rows: tuple[RowScores, ...]
    questions: tuple[QuestionResult, ...]


def run_rows(
    questions: Sequence[GoldenQuestion],
    store: IndexStore,
    embedder: Embedder,
    reranker: Reranker,
    *,
    candidate_k: int,
    top_k: int,
    clock: Callable[[], float] = time.perf_counter,
) -> tuple[tuple[RowScores, ...], tuple[QuestionResult, ...]]:
    rows: list[RowScores] = []
    results: list[QuestionResult] = []
    for name, stage, reranked in ROWS:
        pipeline = SearchPipeline(
            store,
            embedder,
            candidates=stage,
            candidate_k=candidate_k,
            top_k=top_k,
            reranker=reranker if reranked else None,
        )
        if questions:
            pipeline.search(questions[0].question)  # warm-up, not timed
        row_results: list[QuestionResult] = []
        for q in questions:
            start = clock()
            found = pipeline.search(q.question)
            elapsed_ms = (clock() - start) * 1000
            sources = tuple(r.source for r in found)
            row_results.append(
                QuestionResult(
                    id=q.id,
                    lang=q.lang,
                    row=name,
                    sources=sources,
                    recall=recall_at_k(sources, q.expected_sources, k=top_k)
                    if q.answerable
                    else None,
                    reciprocal_rank=reciprocal_rank(sources, q.expected_sources, k=top_k)
                    if q.answerable
                    else None,
                    latency_ms=elapsed_ms,
                )
            )
        rows.append(
            RowScores(
                name=name,
                stage=stage,
                reranked=reranked,
                languages={
                    lang: _scores(r for r in row_results if r.lang == lang) for lang in LANGS
                },
            )
        )
        results.extend(row_results)
    return tuple(rows), tuple(results)


def _scores(results) -> LanguageScores:
    results = list(results)
    scored = [r for r in results if r.recall is not None]
    latencies = [r.latency_ms for r in results]

    def mean(values: list[float]) -> float | None:
        return sum(values) / len(values) if values else None

    return LanguageScores(
        questions=len(scored),
        timed=len(results),
        recall_at_k=mean([r.recall for r in scored if r.recall is not None]),
        mrr=mean([r.reciprocal_rank for r in scored if r.reciprocal_rank is not None]),
        latency_ms_p50=percentile(latencies, 50) if latencies else None,
        latency_ms_p95=percentile(latencies, 95) if latencies else None,
    )

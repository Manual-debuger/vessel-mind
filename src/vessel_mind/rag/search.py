"""The retrieval pipeline (docs/specs/a-agent-track.md §5.6).

    candidates = vector | keyword | hybrid (RRF, k = 60)   -> top candidate_k (30)
    results    = candidates[:top_k]                       -> top k (default 5)

A reranker (a later stage) goes between the two lines: it reorders the
candidates and never adds or drops one.

Every candidate stage scores with reciprocal rank fusion, so a score means the
same thing in every stage: ``vector`` and ``keyword`` fuse one ranking,
``hybrid`` fuses both. Higher is better.
"""

from collections.abc import Hashable, Iterable, Sequence
from dataclasses import dataclass
from typing import Literal

from vessel_mind.rag.embed import Embedder
from vessel_mind.rag.store import IndexStore, StoredChunk

CandidateStage = Literal["vector", "keyword", "hybrid"]

RRF_K = 60
CANDIDATE_K = 30
TOP_K = 5


def rrf_fuse[T: Hashable](rankings: Iterable[Sequence[T]], k: int = RRF_K) -> list[tuple[T, float]]:
    """Reciprocal rank fusion: each item scores the sum of 1 / (k + rank) over the
    rankings it appears in, rank counted from 1. Best first; a tie keeps the
    order in which the items were first seen."""
    scores: dict[T, float] = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking, start=1):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank)
    # sorted is stable, and a dict keeps first-seen order.
    return sorted(scores.items(), key=lambda pair: pair[1], reverse=True)


@dataclass(frozen=True)
class SearchResult:
    source: str  # <source name>/<path>#<anchor>, the citation target
    url: str  # the section on GitHub, at the pinned commit
    heading_path: tuple[str, ...]
    text: str
    score: float  # the candidate stage's fused score
    rerank_score: float | None = None  # set only by a reranker


def _result(chunk: StoredChunk, score: float) -> SearchResult:
    url = f"https://github.com/{chunk.repo}/blob/{chunk.commit}/{chunk.path}"
    if chunk.anchor:
        url += f"#{chunk.anchor}"
    return SearchResult(
        source=chunk.source,
        url=url,
        heading_path=chunk.heading_path,
        text=chunk.text,
        score=score,
    )


class SearchPipeline:
    """Synchronous and not thread-safe, like the IndexStore it reads: the caller
    serializes calls."""

    def __init__(
        self,
        store: IndexStore,
        embedder: Embedder,
        *,
        candidates: CandidateStage = "hybrid",
        candidate_k: int = CANDIDATE_K,
        top_k: int = TOP_K,
    ) -> None:
        if candidate_k < 1 or top_k < 1:
            raise ValueError("candidate_k and top_k must be at least 1")
        self._store = store
        self._embedder = embedder
        self.stage: CandidateStage = candidates
        self.candidate_k = candidate_k
        self.top_k = top_k

    def candidates(self, query: str) -> list[SearchResult]:
        """The candidate stage: at most candidate_k results, best first."""
        rankings: list[list[int]] = []
        if self.stage in ("vector", "hybrid"):
            vector = self._embedder.embed_query(query)
            rankings.append([cid for cid, _ in self._store.nearest(vector, self.candidate_k)])
        if self.stage in ("keyword", "hybrid"):
            rankings.append([cid for cid, _ in self._store.keyword(query, self.candidate_k)])
        fused = rrf_fuse(rankings)[: self.candidate_k]
        return [_result(self._store.chunk(cid), score) for cid, score in fused]

    def search(self, query: str, top_k: int | None = None) -> list[SearchResult]:
        return self.candidates(query)[: top_k or self.top_k]

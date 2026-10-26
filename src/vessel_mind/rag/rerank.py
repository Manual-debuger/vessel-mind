"""Rerankers: cross-encoders behind the pipeline's Reranker protocol (spec §5.6).

Providers, chosen in config (retrieval.reranker):

- ``cpu`` (default): a cross-encoder through fastembed. No GPU and no key.
- ``none``: no rerank stage; results keep their fused order.

The spec also names a ``llama-server`` provider (its rerank endpoint). It is
cut, first in the spec's cut order: it could not be checked against a running
server in this sprint, and Ollama has no confirmed rerank endpoint either.
A later provider implements CrossEncoder and plugs into CrossEncoderReranker.
"""

from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal, Protocol

from vessel_mind.rag.search import CANDIDATE_K, Reranker, SearchResult

RerankerProvider = Literal["cpu", "none"]

# Multilingual (BGE-M3 base: Chinese and English), Apache-2.0, 0.57 GB as INT8
# ONNX. Questions may be in Traditional Chinese over English documents (§5.6).
# The FP32 export (BAAI/bge-reranker-v2-m3) is 2.27 GB; jina's multilingual
# reranker is CC-BY-NC.
DEFAULT_RERANK_MODEL = "BAAI/bge-reranker-v2-m3-int8"


class CrossEncoder(Protocol):
    """Scores each text's relevance to a query; higher is more relevant."""

    @property
    def model_id(self) -> str: ...

    def score(self, query: str, texts: Sequence[str]) -> list[float]: ...


class CrossEncoderReranker:
    """Reorders candidates by a cross-encoder's score, best first. A tie keeps
    the fused order. It refuses more than max_candidates: reranking the corpus
    instead of the top candidates makes latency grow with the corpus."""

    def __init__(self, encoder: CrossEncoder, *, max_candidates: int = CANDIDATE_K) -> None:
        self._encoder = encoder
        self._max_candidates = max_candidates

    @property
    def model_id(self) -> str:
        return self._encoder.model_id

    def rerank(self, query: str, candidates: Sequence[SearchResult]) -> list[SearchResult]:
        if len(candidates) > self._max_candidates:
            raise ValueError(
                f"the reranker takes at most {self._max_candidates} candidates, "
                f"not {len(candidates)}"
            )
        if not candidates:
            return []
        scores = self._encoder.score(query, [_passage(c) for c in candidates])
        if len(scores) != len(candidates):
            raise ValueError(f"{len(scores)} scores for {len(candidates)} candidates")
        scored = [
            replace(c, rerank_score=float(s)) for c, s in zip(candidates, scores, strict=True)
        ]
        return sorted(scored, key=lambda r: r.rerank_score or 0.0, reverse=True)


def _passage(result: SearchResult) -> str:
    """The text the cross-encoder reads: the heading path, a blank line, the text,
    like the text the chunk was embedded from."""
    if not result.heading_path:
        return result.text
    return " > ".join(result.heading_path) + "\n\n" + result.text


class FastEmbedCrossEncoder:
    """A fastembed cross-encoder on CPU. The model loads (and downloads, once) on
    the first score call, so building one costs nothing."""

    def __init__(self, model_id: str = DEFAULT_RERANK_MODEL, *, cache_dir: Path | None = None):
        from fastembed.rerank.cross_encoder import TextCrossEncoder

        supported = {m["model"] for m in TextCrossEncoder.list_supported_models()}
        if model_id not in supported:
            raise ValueError(f"fastembed does not support the reranker model {model_id!r}")
        self._model_id = model_id
        self._cache_dir = cache_dir
        self._model: Any = None

    @property
    def model_id(self) -> str:
        return self._model_id

    def _loaded(self) -> Any:
        if self._model is None:
            from fastembed.rerank.cross_encoder import TextCrossEncoder

            cache = str(self._cache_dir) if self._cache_dir is not None else None
            self._model = TextCrossEncoder(self._model_id, cache_dir=cache)
        return self._model

    def score(self, query: str, texts: Sequence[str]) -> list[float]:
        return [float(s) for s in self._loaded().rerank(query, list(texts))]


def make_reranker(
    provider: RerankerProvider,
    model_id: str = DEFAULT_RERANK_MODEL,
    *,
    cache_dir: Path | None = None,
) -> Reranker | None:
    """The configured reranker; None for ``none``. Raises ValueError for a model
    fastembed does not support. Loads no model."""
    if provider == "none":
        return None
    return CrossEncoderReranker(FastEmbedCrossEncoder(model_id, cache_dir=cache_dir))

"""Retrieval in the server: startup ingest, the open index, and the search_docs tool.

agent never imports rag; the server builds the tool here and registers it in
the registry it gives the loop.
"""

import asyncio
import logging
import threading
from collections.abc import Callable, Iterable
from pathlib import Path
from types import TracebackType
from typing import Self

from vessel_mind.core.capabilities import CapabilityLabels, CapabilityRegistry, ToolError
from vessel_mind.rag.corpus import Document
from vessel_mind.rag.embed import Embedder
from vessel_mind.rag.ingest import ensure_index
from vessel_mind.rag.search import (
    CANDIDATE_K,
    TOP_K,
    CandidateStage,
    Reranker,
    SearchPipeline,
    SearchResult,
)
from vessel_mind.rag.search_docs import SearchDocsTool
from vessel_mind.rag.store import IndexStore

log = logging.getLogger(__name__)

# The corpus is public, pinned and reviewed (ADR 0001 §7): not private data, and
# not third-party instructions. Set taints_context to True if that changes.
SEARCH_DOCS_LABELS = CapabilityLabels(
    resource="public_corpus", effect="read", exfil=False, taints_context=False
)


class RetrievalService:
    """An async context manager for the app's lifespan. Entering it ingests the
    corpus when the index is missing or unfinished, then opens the index."""

    def __init__(
        self,
        *,
        index_path: Path,
        load_documents: Callable[[], Iterable[Document]],
        embedder: Embedder,
        candidates: CandidateStage = "hybrid",
        candidate_k: int = CANDIDATE_K,
        top_k: int = TOP_K,
        reranker: Reranker | None = None,
    ) -> None:
        self._index_path = index_path
        self._load_documents = load_documents
        self._embedder = embedder
        self._candidates: CandidateStage = candidates
        self._candidate_k = candidate_k
        self._top_k = top_k
        self._reranker = reranker
        self._store: IndexStore | None = None
        self._pipeline: SearchPipeline | None = None
        # One search at a time: the store's connection and the embedder are shared.
        self._lock = threading.Lock()
        self.tool = SearchDocsTool(self.search)

    async def __aenter__(self) -> Self:
        # Blocking: the first start may download the corpus and the embedding model.
        report = await asyncio.to_thread(
            ensure_index, self._index_path, self._load_documents, self._embedder
        )
        if report is not None:
            log.info("retrieval index %s: %s", self._index_path, report)
        self._store = IndexStore.open(self._index_path, self._embedder)
        self._pipeline = SearchPipeline(
            self._store,
            self._embedder,
            candidates=self._candidates,
            candidate_k=self._candidate_k,
            top_k=self._top_k,
            reranker=self._reranker,
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        with self._lock:
            store, self._store, self._pipeline = self._store, None, None
            if store is not None:
                store.close()

    def search(self, query: str) -> list[SearchResult]:
        """The pipeline's top k for ``query``. Safe to call from any thread."""
        with self._lock:
            if self._pipeline is None:
                raise ToolError("The document index is not open.")
            return self._pipeline.search(query)


def search_docs_registry(retrieval: RetrievalService) -> CapabilityRegistry:
    """A registry holding search_docs with its labels."""
    registry = CapabilityRegistry()
    registry.register(retrieval.tool, SEARCH_DOCS_LABELS)
    return registry

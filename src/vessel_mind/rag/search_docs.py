"""search_docs: the retrieval pipeline as a tool the loop can call.

rag never imports agent: this module implements core's Tool protocol, and the
server registers it with its labels.

Each call numbers its results from 1. The model cites a result as [n], and a
reader maps [n] to the results of the latest search_docs call before that text.
"""

import asyncio
from collections.abc import Callable
from typing import Any

from vessel_mind.core.capabilities import ToolError
from vessel_mind.core.chat import ToolSpec
from vessel_mind.rag.search import SearchResult

DESCRIPTION = (
    "Search the public vessel-mind and VibeConform documents: design specs, ADRs, the "
    "glossary and the VibeConform docs. Returns numbered sections, best first. Cite a "
    "section as [n], for example [1] or [2][3], after the sentence it supports. Each "
    "search numbers its results from 1, so cite only the results of your latest search. "
    "If no result answers the question, say that the documents do not cover it."
)

SPEC = ToolSpec(
    name="search_docs",
    description=DESCRIPTION,
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "What to look for, in English or Chinese.",
            }
        },
        "required": ["query"],
    },
)


class SearchDocsTool:
    """Runs ``search`` in a worker thread: it embeds the query on the CPU and reads
    SQLite, which must not block the event loop. So ``search`` must be safe to call
    from any thread, and concurrently."""

    def __init__(self, search: Callable[[str], list[SearchResult]]) -> None:
        self._search = search

    @property
    def spec(self) -> ToolSpec:
        return SPEC

    async def execute(self, input: dict[str, Any]) -> Any:
        query = input.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ToolError("search_docs needs a non-empty 'query' string.")
        results = await asyncio.to_thread(self._search, query.strip())
        return {"results": [_cited(n, result) for n, result in enumerate(results, start=1)]}


def _cited(n: int, result: SearchResult) -> dict[str, Any]:
    cited: dict[str, Any] = {
        "n": n,
        "source": result.source,
        "url": result.url,
        "heading": " > ".join(result.heading_path),
        "text": result.text,
        "score": result.score,
    }
    if result.rerank_score is not None:
        cited["rerank_score"] = result.rerank_score
    return cited

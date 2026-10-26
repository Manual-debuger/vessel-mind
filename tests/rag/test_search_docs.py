"""The search_docs tool: numbered results the model cites as [n]."""

import pytest

from vessel_mind.core.capabilities import ToolError
from vessel_mind.rag.search import SearchResult
from vessel_mind.rag.search_docs import SearchDocsTool

ROUTER = SearchResult(
    source="vibeconform/docs/router.md#router",
    url="https://github.com/example/vibeconform/blob/abc/docs/router.md#router",
    heading_path=("Design", "Router"),
    text="The router picks a path.",
    score=0.032,
)
SESSION = SearchResult(
    source="vessel-mind/GLOSSARY.md#session",
    url="https://github.com/example/vessel-mind/blob/abc/GLOSSARY.md#session",
    heading_path=("Session",),
    text="One conversation.",
    score=0.016,
    rerank_score=0.9,
)


class FakeSearch:
    def __init__(self, results: list[SearchResult]) -> None:
        self.results = results
        self.queries: list[str] = []

    def __call__(self, query: str) -> list[SearchResult]:
        self.queries.append(query)
        return self.results


@pytest.mark.asyncio
async def test_results_are_numbered_from_one_with_their_source_and_score():
    search = FakeSearch([ROUTER, SESSION])

    result = await SearchDocsTool(search).execute({"query": "  what is the router? "})

    assert search.queries == ["what is the router?"]
    assert result == {
        "results": [
            {
                "n": 1,
                "source": "vibeconform/docs/router.md#router",
                "url": "https://github.com/example/vibeconform/blob/abc/docs/router.md#router",
                "heading": "Design > Router",
                "text": "The router picks a path.",
                "score": 0.032,
            },
            {
                "n": 2,
                "source": "vessel-mind/GLOSSARY.md#session",
                "url": "https://github.com/example/vessel-mind/blob/abc/GLOSSARY.md#session",
                "heading": "Session",
                "text": "One conversation.",
                "score": 0.016,
                "rerank_score": 0.9,
            },
        ]
    }


@pytest.mark.asyncio
async def test_no_match_is_an_empty_list_the_model_can_report():
    assert await SearchDocsTool(FakeSearch([])).execute({"query": "zebra"}) == {"results": []}


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [{}, {"query": "   "}, {"query": 3}])
async def test_a_missing_or_blank_query_is_a_tool_error(bad):
    with pytest.raises(ToolError, match="query"):
        await SearchDocsTool(FakeSearch([])).execute(bad)


def test_the_spec_asks_for_a_query_and_tells_the_model_how_to_cite():
    spec = SearchDocsTool(FakeSearch([])).spec

    assert spec.name == "search_docs"
    assert spec.parameters["required"] == ["query"]
    assert "[n]" in spec.description

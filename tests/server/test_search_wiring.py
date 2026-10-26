"""search_docs wired by the server: startup ingest, labels, and a turn that searches.

The embedder and the corpus are fakes: no test loads a model or reads the network.
"""

import json
from collections.abc import Sequence
from dataclasses import replace

from fastapi.testclient import TestClient

from vessel_mind.core.chat import ChatRequest, Stop, TextDelta, ToolCall, ToolMessage, Usage
from vessel_mind.rag.corpus import Document
from vessel_mind.rag.search import SearchResult
from vessel_mind.rag.store import IndexStore
from vessel_mind.server.app import create_app
from vessel_mind.server.retrieval import RetrievalService, search_docs_registry

VOCAB = ("session", "turn", "proposal", "approval", "corpus", "router")


class BagOfWords:
    model_id = "fake/bag-of-words"
    dim = len(VOCAB)

    def __init__(self) -> None:
        self.embedded: list[str] = []

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        self.embedded.extend(texts)
        return [self._vector(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)

    def _vector(self, text: str) -> list[float]:
        words = text.lower().split()
        return [float(words.count(term)) + 0.01 for term in VOCAB]


CORPUS = [
    Document(
        source_name="vessel-mind",
        repo="example/vessel-mind",
        commit="b" * 40,
        path="GLOSSARY.md",
        text="# Proposal\n\nA proposal waits for an approval.\n\n# Router\n\nThe router picks.\n",
    )
]


def service(index, embedder=None, loader=lambda: CORPUS, **settings) -> RetrievalService:
    return RetrievalService(
        index_path=index,
        load_documents=loader,
        embedder=embedder or BagOfWords(),
        **settings,
    )


class StepModel:
    id = "steps"

    def __init__(self, *steps) -> None:
        self.steps = list(steps)
        self.requests: list[ChatRequest] = []

    async def stream(self, req: ChatRequest):
        self.requests.append(req)
        for event in self.steps[len(self.requests) - 1]:
            yield event


def read_events(client: TestClient, turn_id: str) -> list[dict]:
    with client.stream("GET", f"/turns/{turn_id}/events") as response:
        return [
            json.loads(line.removeprefix("data: "))
            for line in response.iter_lines()
            if line.startswith("data: ")
        ]


def test_search_docs_is_a_public_corpus_read_tool_that_neither_exfiltrates_nor_taints(tmp_path):
    registry = search_docs_registry(service(tmp_path / "index.sqlite"))

    registered = registry.get("search_docs")

    assert registered is not None
    assert registered.labels.model_dump() == {
        "resource": "public_corpus",
        "effect": "read",
        "exfil": False,
        "taints_context": False,
    }
    assert registry.trust_level == "trusted"


def test_startup_ingests_a_missing_index_and_a_turn_searches_it(tmp_path):
    index = tmp_path / "data" / "index.sqlite"
    retrieval = service(index, candidates="hybrid", top_k=2)
    model = StepModel(
        [
            ToolCall(id="c1", name="search_docs", input={"query": "proposal approval"}),
            Stop(reason="tool_use", usage=Usage(input_tokens=5)),
        ],
        [TextDelta(delta="It waits for an approval [1]."), Stop(reason="end", usage=Usage())],
    )
    app = create_app(model, tools=search_docs_registry(retrieval), services=[retrieval])

    with TestClient(app) as client:
        started = client.post("/turns", json={"text": "What is a proposal?"}).json()
        events = read_events(client, started["turn_id"])

    assert [t.name for t in model.requests[0].tools] == ["search_docs"]
    [result] = [e for e in events if e["type"] == "tool_result"]
    assert result["is_error"] is False
    first = result["result"]["results"][0]
    assert first["n"] == 1
    assert first["source"] == "vessel-mind/GLOSSARY.md#proposal"
    assert (
        first["url"]
        == f"https://github.com/example/vessel-mind/blob/{'b' * 40}/GLOSSARY.md#proposal"
    )
    assert len(result["result"]["results"]) == 2
    # The model reads the same numbered results it is told to cite.
    tool_message = model.requests[1].messages[-1]
    assert isinstance(tool_message, ToolMessage)
    assert json.loads(tool_message.content) == result["result"]
    with IndexStore.open(index, BagOfWords()) as store:
        assert store.count_chunks() == 2


def test_a_restart_on_a_complete_index_reads_no_corpus_and_embeds_no_document(tmp_path):
    index = tmp_path / "index.sqlite"
    with TestClient(create_app(StepModel(), services=[service(index)])):
        pass

    def unreachable() -> list[Document]:
        raise AssertionError("a complete index must not reload the corpus")

    restarted = BagOfWords()
    with TestClient(create_app(StepModel(), services=[service(index, restarted, unreachable)])):
        pass

    assert restarted.embedded == []


class Reversing:
    """A fake reranker: reverses the candidates and scores them by position."""

    model_id = "fake/reversing"

    def rerank(self, query: str, candidates: Sequence[SearchResult]) -> list[SearchResult]:
        n = len(candidates)
        return [replace(c, rerank_score=float(n - i)) for i, c in enumerate(reversed(candidates))]


def test_the_service_runs_its_reranker_after_the_candidate_stage(tmp_path):
    index = tmp_path / "index.sqlite"
    plain = service(index, candidates="keyword")
    with TestClient(create_app(StepModel(), services=[plain])):
        fused = plain.search("proposal router")

    reranked = service(index, candidates="keyword", reranker=Reversing())
    with TestClient(create_app(StepModel(), services=[reranked])):
        results = reranked.search("proposal router")

    assert all(r.rerank_score is None for r in fused)
    assert [r.source for r in results] == [r.source for r in reversed(fused)]
    assert [r.rerank_score for r in results] == [2.0, 1.0]


def test_the_configured_candidate_stage_is_the_one_that_runs(tmp_path):
    # "picks" is outside the embedder's vocabulary: only keyword search can find it,
    # and vector search ranks every chunk.
    index = tmp_path / "index.sqlite"
    keyword = service(index, candidates="keyword")
    with TestClient(create_app(StepModel(), services=[keyword])):
        assert [r.source for r in keyword.search("picks")] == ["vessel-mind/GLOSSARY.md#router"]

    vector = service(index, candidates="vector")
    with TestClient(create_app(StepModel(), services=[vector])):
        assert len(vector.search("picks")) == 2

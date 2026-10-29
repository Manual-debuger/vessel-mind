"""The reranker stage (spec §5.6): it reorders the candidates and nothing else.

The cross-encoder is a fake scorer: tests never load or download a model.
"""

from collections.abc import Sequence

import pytest

from vessel_mind.rag.corpus import Document
from vessel_mind.rag.ingest import ingest
from vessel_mind.rag.rerank import CrossEncoderReranker, make_reranker
from vessel_mind.rag.search import SearchPipeline, SearchResult
from vessel_mind.rag.store import IndexStore


class Constant:
    """Every chunk is equally near every query, so the vector stage keeps
    the index order and returns every chunk up to candidate_k."""

    model_id = "fake/constant"
    dim = 2

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [[1.0, 0.0] for _ in texts]

    def embed_query(self, text: str) -> list[float]:
        return [1.0, 0.0]


class WordCount:
    """A fake cross-encoder: the score is how often the query's first word occurs."""

    model_id = "fake/word-count"

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str]]] = []

    def score(self, query: str, texts: Sequence[str]) -> list[float]:
        self.calls.append((query, list(texts)))
        word = query.split()[0]
        return [float(text.count(word)) for text in texts]


def _doc(path: str, text: str) -> Document:
    return Document(
        source_name="fixture", repo="example/fixture", commit="a" * 40, path=path, text=text
    )


CORPUS = [
    _doc("a.md", "# A\n\nNothing here.\n"),
    _doc("b.md", "# B\n\nOne apple.\n"),
    _doc("c.md", "# C\n\napple apple apple.\n"),
    _doc("d.md", "# D\n\napple apple.\n"),
]


@pytest.fixture
def store(tmp_path):
    embedder = Constant()
    with IndexStore.open(tmp_path / "index.sqlite", embedder) as opened:
        ingest(CORPUS, opened, embedder)
        yield opened


def test_the_reranker_reorders_the_candidates_and_sets_the_rerank_score(store):
    pipeline = SearchPipeline(
        store, Constant(), candidates="vector", top_k=4, reranker=CrossEncoderReranker(WordCount())
    )

    before = pipeline.candidates("apple")
    after = pipeline.search("apple")

    assert [r.source for r in after] == [
        "fixture/c.md#c",
        "fixture/d.md#d",
        "fixture/b.md#b",
        "fixture/a.md#a",
    ]
    assert [r.rerank_score for r in after] == [3.0, 2.0, 1.0, 0.0]
    # Only reordered: the same candidates, each keeping its fused score.
    assert {(r.source, r.score) for r in after} == {(r.source, r.score) for r in before}


def test_the_reranker_scores_the_heading_path_with_the_text(store):
    scorer = WordCount()
    pipeline = SearchPipeline(
        store, Constant(), candidates="vector", top_k=1, reranker=CrossEncoderReranker(scorer)
    )

    pipeline.search("apple")

    [(_, texts)] = scorer.calls
    # The same text the chunk was embedded from: heading path, blank line, chunk.
    assert "C\n\n# C\n\napple apple apple." in texts


def test_the_reranker_receives_the_candidates_not_the_corpus(tmp_path):
    embedder = Constant()
    corpus = [_doc(f"doc{i:02}.md", f"# Doc {i}\n\napple {i}.\n") for i in range(40)]
    scorer = WordCount()
    with IndexStore.open(tmp_path / "index.sqlite", embedder) as store:
        ingest(corpus, store, embedder)
        pipeline = SearchPipeline(
            store, embedder, candidates="hybrid", reranker=CrossEncoderReranker(scorer)
        )

        results = pipeline.search("apple")

    # 40 chunks match; the candidate stage keeps 30 and only those are scored.
    [(_, texts)] = scorer.calls
    assert len(texts) == 30
    assert len(results) == 5


def test_a_reranker_given_more_than_30_candidates_refuses():
    reranker = CrossEncoderReranker(WordCount())

    with pytest.raises(ValueError, match="at most 30"):
        reranker.rerank("apple", [_result(i) for i in range(31)])


def test_a_scorer_that_returns_the_wrong_number_of_scores_is_an_error():
    class Short(WordCount):
        def score(self, query: str, texts: Sequence[str]) -> list[float]:
            return super().score(query, texts)[:-1]

    with pytest.raises(ValueError, match="2 scores for 3 candidates"):
        CrossEncoderReranker(Short()).rerank("apple", [_result(i) for i in range(3)])


def test_without_a_reranker_results_carry_no_rerank_score(store):
    pipeline = SearchPipeline(store, Constant(), candidates="vector")

    assert all(r.rerank_score is None for r in pipeline.search("apple"))


def test_the_pipeline_refuses_a_reranker_that_drops_a_candidate(store):
    class Dropping:
        model_id = "fake/dropping"

        def rerank(self, query: str, candidates: Sequence[SearchResult]) -> list[SearchResult]:
            return list(candidates)[1:]

    pipeline = SearchPipeline(store, Constant(), candidates="vector", reranker=Dropping())

    with pytest.raises(ValueError, match="added or dropped"):
        pipeline.search("apple")


def test_the_cpu_provider_is_a_multilingual_fastembed_cross_encoder_that_loads_nothing_yet():
    reranker = make_reranker("cpu")

    assert reranker is not None
    assert reranker.model_id == "BAAI/bge-reranker-v2-m3-int8"


def test_the_none_provider_has_no_rerank_stage():
    assert make_reranker("none") is None


def test_a_reranker_model_fastembed_does_not_support_is_refused():
    with pytest.raises(ValueError, match="reranker model 'no/such-model'"):
        make_reranker("cpu", "no/such-model")


def _result(i: int) -> SearchResult:
    return SearchResult(
        source=f"fixture/{i}.md", url="https://github.com/x", heading_path=(), text="", score=0.0
    )

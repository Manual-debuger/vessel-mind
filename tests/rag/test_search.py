"""Retrieval: reciprocal rank fusion and the candidate stage (spec §5.6).

The embedder is a fake bag of words: tests never load a model.
"""

from collections.abc import Sequence

import pytest

from vessel_mind.rag.corpus import Document
from vessel_mind.rag.ingest import ingest
from vessel_mind.rag.search import SearchPipeline, rrf_fuse
from vessel_mind.rag.store import IndexStore


def test_rrf_ranks_an_item_found_by_both_lists_above_items_found_by_one():
    vector = [1, 2, 3]
    keyword = [3, 1, 4]

    fused = rrf_fuse([vector, keyword], k=60)

    # Worked by hand: 1 = 1/61 + 1/62, 3 = 1/63 + 1/61, 2 = 1/62, 4 = 1/63.
    assert [item for item, _ in fused] == [1, 3, 2, 4]
    assert [score for _, score in fused] == pytest.approx(
        [0.0325221, 0.0322664, 0.0161290, 0.0158730], abs=1e-6
    )


def test_rrf_breaks_a_tie_by_the_earlier_list():
    # 7 and 8 both score 1/61: 7 comes first because its list comes first.
    assert [item for item, _ in rrf_fuse([[7], [8]])] == [7, 8]


VOCAB = ("session", "turn", "proposal", "approval", "corpus", "router")


class BagOfWords:
    model_id = "fake/bag-of-words"
    dim = len(VOCAB)

    def __init__(self) -> None:
        self.queries: list[str] = []

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        self.queries.append(text)
        return self._vector(text)

    def _vector(self, text: str) -> list[float]:
        words = text.lower().split()
        return [float(words.count(term)) + 0.01 for term in VOCAB]


def _doc(path: str, text: str) -> Document:
    return Document(
        source_name="fixture", repo="example/fixture", commit="a" * 40, path=path, text=text
    )


# "zebra" is outside the embedder's vocabulary, so only keyword search finds it;
# "router" is in both.
CORPUS = [
    _doc("GLOSSARY.md", "# Session\n\nA session holds every turn.\n"),
    _doc("docs/router.md", "# Router\n\nThe router router picks a path.\n"),
    _doc("docs/zebra.md", "# Zebra stripes\n\nA zebra is not a proposal.\n"),
]


@pytest.fixture
def store(tmp_path):
    embedder = BagOfWords()
    with IndexStore.open(tmp_path / "index.sqlite", embedder) as opened:
        ingest(CORPUS, opened, embedder)
        yield opened


def test_a_result_carries_its_source_with_the_heading_anchor_and_the_fused_score(store):
    pipeline = SearchPipeline(store, BagOfWords(), candidates="hybrid")

    [first, *_] = pipeline.search("router")

    assert first.source == "fixture/docs/router.md#router"
    assert first.url == f"https://github.com/example/fixture/blob/{'a' * 40}/docs/router.md#router"
    assert first.heading_path == ("Router",)
    assert "picks a path" in first.text
    # First in both the vector and the keyword list.
    assert first.score == pytest.approx(2 / 61)
    assert first.rerank_score is None


def test_keyword_candidates_find_a_word_the_embedder_cannot_see(store):
    keyword = SearchPipeline(store, BagOfWords(), candidates="keyword")

    assert [r.source for r in keyword.search("zebra")] == ["fixture/docs/zebra.md#zebra-stripes"]


def test_vector_candidates_never_run_keyword_search(store):
    vector = SearchPipeline(store, BagOfWords(), candidates="vector", top_k=1)

    # Every chunk is a vector neighbour; the nearest one for "session" is the glossary.
    assert [r.source for r in vector.search("session")] == ["fixture/GLOSSARY.md#session"]


def test_keyword_candidates_embed_no_query(store):
    embedder = BagOfWords()

    SearchPipeline(store, embedder, candidates="keyword").search("router")

    assert embedder.queries == []


def test_the_pipeline_returns_at_most_top_k_of_at_most_candidate_k_candidates(store):
    pipeline = SearchPipeline(store, BagOfWords(), candidates="hybrid", candidate_k=2, top_k=5)

    assert len(pipeline.candidates("router session zebra")) == 2
    assert len(pipeline.search("router session zebra")) == 2
    assert len(pipeline.search("router session zebra", top_k=1)) == 1

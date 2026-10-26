"""Ingest into the retrieval index: content hashing, startup ingest, a separate file.

The embedder is a fake that counts what it embeds: tests never load a model.
"""

import sqlite3
from collections.abc import Sequence

import pytest

from vessel_mind.rag.corpus import Document
from vessel_mind.rag.ingest import ensure_index, ingest, main
from vessel_mind.rag.store import IndexStore, NotAnIndexError

VOCAB = ("session", "turn", "proposal", "approval", "corpus", "router")


class CountingEmbedder:
    """Bag of words over a tiny vocabulary, so nearest-neighbour results are predictable."""

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


def _doc(path: str, text: str) -> Document:
    return Document(
        source_name="fixture", repo="example/fixture", commit="0" * 40, path=path, text=text
    )


CORPUS = [
    _doc("GLOSSARY.md", "# Session\n\nA session holds every turn of one session.\n"),
    _doc("docs/approval.md", "# Approval\n\nAn approval executes one proposal proposal.\n"),
]


def test_ingest_stores_chunks_that_vector_and_keyword_search_can_find(tmp_path):
    embedder = CountingEmbedder()
    with IndexStore.open(tmp_path / "index.sqlite", embedder) as store:
        report = ingest(CORPUS, store, embedder)

        assert report.added == 2
        assert store.count_chunks() == 2
        [(nearest_id, _)] = store.nearest(embedder.embed_query("proposal"), k=1)
        assert store.chunk(nearest_id).source == "fixture/docs/approval.md#approval"
        [(keyword_id, _)] = store.keyword("session", k=1)
        stored = store.chunk(keyword_id)
        assert stored.source == "fixture/GLOSSARY.md#session"
        assert stored.heading_path == ("Session",)
        assert stored.repo == "example/fixture"
        assert stored.commit == "0" * 40


def test_re_ingesting_unchanged_files_is_a_no_op(tmp_path):
    embedder = CountingEmbedder()
    with IndexStore.open(tmp_path / "index.sqlite", embedder) as store:
        ingest(CORPUS, store, embedder)
        embedded_before = len(embedder.embedded)

        report = ingest(CORPUS, store, embedder)

        assert len(embedder.embedded) == embedded_before
        assert (report.added, report.updated, report.unchanged, report.removed) == (0, 0, 2, 0)
        assert store.count_chunks() == 2


def test_a_changed_file_is_re_embedded_alone_and_its_old_chunks_are_gone(tmp_path):
    embedder = CountingEmbedder()
    with IndexStore.open(tmp_path / "index.sqlite", embedder) as store:
        ingest(CORPUS, store, embedder)
        embedder.embedded.clear()
        changed = [CORPUS[0], _doc("docs/approval.md", "# Router\n\nThe router picks a path.\n")]

        report = ingest(changed, store, embedder)

        assert (report.updated, report.unchanged) == (1, 1)
        assert len(embedder.embedded) == 1
        assert store.keyword("approval", k=5) == []
        assert store.count_chunks() == 2


def test_a_file_dropped_from_the_corpus_is_removed_from_the_index(tmp_path):
    embedder = CountingEmbedder()
    with IndexStore.open(tmp_path / "index.sqlite", embedder) as store:
        ingest(CORPUS, store, embedder)

        report = ingest(CORPUS[:1], store, embedder)

        assert report.removed == 1
        assert store.count_chunks() == 1
        assert store.keyword("approval", k=5) == []


def test_keyword_search_treats_query_punctuation_as_text(tmp_path):
    embedder = CountingEmbedder()
    with IndexStore.open(tmp_path / "index.sqlite", embedder) as store:
        ingest(CORPUS, store, embedder)

        assert store.keyword('what is a "session"? (AND OR*)', k=5) != []


def test_the_app_ingests_at_startup_when_the_index_is_missing(tmp_path):
    embedder = CountingEmbedder()
    index = tmp_path / "data" / "index.sqlite"

    report = ensure_index(index, lambda: CORPUS, embedder)

    assert report is not None and report.added == 2
    with IndexStore.open(index, embedder) as store:
        assert store.count_chunks() == 2


def test_a_restart_with_an_existing_index_reads_no_corpus_and_embeds_nothing(tmp_path):
    index = tmp_path / "index.sqlite"
    ensure_index(index, lambda: CORPUS, CountingEmbedder())
    restarted = CountingEmbedder()

    def load_documents() -> list[Document]:
        raise AssertionError("an existing index must not reload the corpus")

    assert ensure_index(index, load_documents, restarted) is None
    assert restarted.embedded == []


def test_an_interrupted_ingest_is_finished_at_the_next_startup(tmp_path):
    index = tmp_path / "index.sqlite"
    embedder = CountingEmbedder()

    def failing_load() -> list[Document]:
        raise RuntimeError("network down")

    with pytest.raises(RuntimeError):
        ensure_index(index, failing_load, embedder)

    report = ensure_index(index, lambda: CORPUS, embedder)

    assert report is not None and report.added == 2


def test_changing_the_embedding_model_rebuilds_the_index(tmp_path):
    index = tmp_path / "index.sqlite"
    ensure_index(index, lambda: CORPUS, CountingEmbedder())

    class OtherModel(CountingEmbedder):
        model_id = "fake/other"

    other = OtherModel()
    report = ensure_index(index, lambda: CORPUS, other)

    assert report is not None and report.added == 2
    assert len(other.embedded) == 2


def test_the_index_refuses_a_file_that_holds_other_tables_such_as_the_state_file(tmp_path):
    state = tmp_path / "state.sqlite"
    db = sqlite3.connect(state)
    db.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY)")
    db.commit()
    db.close()

    with pytest.raises(NotAnIndexError):
        IndexStore.open(state, CountingEmbedder())

    db = sqlite3.connect(state)
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    db.close()
    assert tables == {"sessions"}


def test_the_ingest_entry_point_refuses_to_run_when_a_source_cannot_be_pinned(tmp_path, capsys):
    corpus = tmp_path / "corpus.yaml"
    corpus.write_text(
        f"""
sources:
  - name: self
    repo: example/self
    commit: "{"f" * 40}"
    fetch: git-archive
    include: [README.md]
""",
        encoding="utf-8",
    )
    index = tmp_path / "index.sqlite"

    code = main(["--corpus", str(corpus), "--index", str(index), "--repo-root", str(tmp_path)])

    assert code == 2
    assert "ingest refused" in capsys.readouterr().err
    assert not index.exists()

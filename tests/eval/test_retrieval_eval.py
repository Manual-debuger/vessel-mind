"""task eval -- --retrieval-only: table 1, the retrieval ablation (spec §5.8).

The embedder, the cross-encoder and the clock are fakes: no model loads,
nothing is downloaded, and latencies are exact.
"""

import json
from collections.abc import Sequence
from datetime import date
from pathlib import Path

import pytest

from vessel_mind.eval.golden import GoldenSetError, load_golden
from vessel_mind.eval.run import main, run_retrieval_only
from vessel_mind.rag.corpus import Document
from vessel_mind.rag.rerank import CrossEncoderReranker

REPO = Path(__file__).resolve().parent.parent.parent
VOCAB = ("session", "router")


class BagOfWords:
    model_id = "fake/bag-of-words"
    dim = len(VOCAB)

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)

    def _vector(self, text: str) -> list[float]:
        words = text.lower().split()
        return [float(words.count(term)) + 0.01 for term in VOCAB]


class PrefersSessions:
    """A fake cross-encoder that always ranks the session chunk first."""

    model_id = "fake/prefers-sessions"

    def __init__(self) -> None:
        self.calls = 0

    def score(self, query: str, texts: Sequence[str]) -> list[float]:
        self.calls += 1
        return [1.0 if "session" in text.lower() else 0.0 for text in texts]


class Clock:
    """Each reading is 5 ms after the last, so every search takes 5 ms."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        self.now += 0.005
        return self.now


def _doc(path: str, text: str) -> Document:
    return Document(
        source_name="fixture", repo="example/fixture", commit="a" * 40, path=path, text=text
    )


DOCS = [
    _doc("GLOSSARY.md", "# Session\n\nA session holds turns.\n"),
    _doc("router.md", "# Router\n\nThe router picks a path.\n"),
    _doc("zebra.md", "# Zebra\n\nA zebra is striped.\n"),
]

GOLDEN = """
questions:
  - id: en-01
    question: "router"
    lang: en
    expected_sources: [fixture/router.md]
    key_points: ["It picks a path."]
    expected_route: fast
    rationale: "One retrieval."
  - id: en-02
    question: "zebra"
    lang: en
    expected_sources: [fixture/zebra.md]
    key_points: ["It is striped."]
    expected_route: fast
    rationale: "One retrieval."
  - id: un-01
    question: "unicorn"
    lang: en
    expected_sources: []
    key_points: ["Not covered."]
    expected_route: fast
    rationale: "One retrieval."
zh_hant_questions:
  - id: zh-01
    question: "斑馬 zebra 是什麼？"
    lang: zh-Hant
    expected_sources: [fixture/zebra.md]
    key_points: ["有條紋。"]
    expected_route: fast
    rationale: "一次檢索。"
"""


def _golden(tmp_path: Path, text: str = GOLDEN):
    path = tmp_path / "golden.yaml"
    path.write_text(text, encoding="utf-8")
    return load_golden(path)


def _run(tmp_path: Path, golden=None, scorer=None):
    scorer = scorer or PrefersSessions()
    return run_retrieval_only(
        golden=golden or _golden(tmp_path),
        load_documents=lambda: DOCS,
        index_path=tmp_path / "index.sqlite",
        make_models=lambda: (BagOfWords(), CrossEncoderReranker(scorer)),
        results_dir=tmp_path / "results",
        candidate_k=3,
        top_k=1,
        clock=Clock(),
        today=date(2026, 10, 29),
    )


def test_each_row_is_scored_per_language_on_the_answerable_questions(tmp_path):
    ablation = _run(tmp_path)

    rows = {row.name: row for row in ablation.rows}
    assert list(rows) == ["vector", "keyword", "hybrid", "hybrid + rerank"]
    hybrid = rows["hybrid"].languages
    # Two answerable English questions; the unanswerable one is timed but not scored.
    assert (hybrid["en"].questions, hybrid["en"].timed) == (2, 3)
    assert (hybrid["zh-Hant"].questions, hybrid["zh-Hant"].timed) == (1, 1)
    assert hybrid["en"].recall_at_k == 1.0
    assert hybrid["en"].mrr == 1.0
    assert hybrid["zh-Hant"].recall_at_k == 1.0
    # The fake cross-encoder puts the session chunk first, so top 1 misses everything.
    reranked = rows["hybrid + rerank"].languages
    assert reranked["en"].recall_at_k == 0.0
    assert reranked["zh-Hant"].mrr == 0.0
    assert hybrid["en"].latency_ms_p50 == pytest.approx(5.0)
    assert hybrid["en"].latency_ms_p95 == pytest.approx(5.0)


def test_only_the_rerank_row_runs_the_cross_encoder_and_the_results_name_the_models(tmp_path):
    scorer = PrefersSessions()

    ablation = _run(tmp_path, scorer=scorer)

    # One warm-up and four timed searches, all in the rerank row.
    assert scorer.calls == 5
    assert ablation.embedding_model == "fake/bag-of-words"
    assert ablation.reranker_model == "fake/prefers-sessions"
    assert [(p.name, p.commit) for p in ablation.corpus] == [("fixture", "a" * 40)]
    assert ablation.golden_sha256 == _golden(tmp_path).sha256


def test_results_md_is_generated_with_table_1_split_by_language(tmp_path):
    _run(tmp_path)

    text = (tmp_path / "results" / "results.md").read_text(encoding="utf-8")

    assert text.startswith("<!-- Generated by `task eval`. Do not edit")
    assert "## 1. Retrieval ablation" in text
    assert "### English (2 questions with expected sources, 3 timed)" in text
    assert "### Traditional Chinese (1 question with expected sources, 1 timed)" in text
    assert "| Retrieval | recall@1 | MRR@1 | p50 ms | p95 ms |" in text
    assert "| hybrid | 1.00 | 1.00 | 5.0 | 5.0 |" in text
    assert "| hybrid + rerank | 0.00 | 0.00 | 5.0 | 5.0 |" in text
    assert "fake/prefers-sessions" in text
    assert "## 2. End to end" in text


def test_results_md_is_rendered_from_the_stored_results_alone(tmp_path):
    from vessel_mind.eval.results import render_results

    _run(tmp_path)
    results = tmp_path / "results"
    stored = json.loads((results / "retrieval.json").read_text(encoding="utf-8"))

    assert stored["rows"][0]["name"] == "vector"
    assert render_results(results) == (results / "results.md").read_text(encoding="utf-8")


def test_an_expected_source_missing_from_the_corpus_refuses_the_run(tmp_path):
    golden = _golden(tmp_path, GOLDEN.replace("fixture/router.md", "fixture/gone.md"))

    with pytest.raises(GoldenSetError, match="en-01: fixture/gone.md"):
        _run(tmp_path, golden=golden)

    assert not (tmp_path / "results").exists()


def test_the_eval_refuses_to_run_when_a_source_cannot_be_pinned(tmp_path, capsys):
    corpus = tmp_path / "corpus.yaml"
    corpus.write_text(
        f"""
sources:
  - name: fixture
    repo: example/fixture
    commit: "{"f" * 40}"
    fetch: git-archive
    include: [README.md]
""",
        encoding="utf-8",
    )
    results = tmp_path / "results"

    code = main(
        [
            "--retrieval-only",
            "--config-file",
            str(REPO / "config" / "agent.example.yaml"),
            "--golden",
            str(REPO / "eval" / "golden.yaml"),
            "--corpus",
            str(corpus),
            "--repo-root",
            str(tmp_path),
            "--index",
            str(tmp_path / "index.sqlite"),
            "--results-dir",
            str(results),
        ]
    )

    assert code == 2
    assert "eval refused" in capsys.readouterr().err
    assert not results.exists()
    assert not (tmp_path / "index.sqlite").exists()


def test_the_eval_needs_a_mode(capsys):
    with pytest.raises(SystemExit) as exit:
        main([])

    assert exit.value.code == 2
    assert "--retrieval-only" in capsys.readouterr().err

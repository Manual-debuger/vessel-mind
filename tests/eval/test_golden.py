"""The frozen golden set (eval/golden.yaml), as the eval reads it."""

from collections import Counter
from pathlib import Path

import pytest

from vessel_mind.eval.golden import GoldenSetError, load_golden

GOLDEN = Path(__file__).resolve().parent.parent.parent / "eval" / "golden.yaml"


def test_the_frozen_golden_set_loads_both_lists_with_their_languages():
    golden = load_golden(GOLDEN)

    langs = Counter(q.lang for q in golden.questions)
    assert langs == {"en": 25, "zh-Hant": 10}
    assert [q.id for q in golden.questions][:2] == ["en-01", "en-02"]
    assert golden.questions[-1].id == "zh-10"
    unanswerable = sorted(q.id for q in golden.questions if not q.answerable)
    assert unanswerable == ["un-01", "un-02", "un-03", "zh-10"]
    assert len(golden.sha256) == 64


def test_a_question_keeps_its_expected_sources_and_route():
    golden = load_golden(GOLDEN)

    [en04] = [q for q in golden.questions if q.id == "en-04"]
    assert en04.expected_sources == (
        "vessel-mind/docs/adr/0002-demo-exposure-cloudflare-tunnel-access.md",
    )
    assert en04.expected_route == "fast"
    assert len(en04.key_points) == 3


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "golden.yaml"
    path.write_text(body, encoding="utf-8")
    return path


QUESTION = """
  - id: {id}
    question: "Q?"
    lang: {lang}
    expected_sources: [vessel-mind/GLOSSARY.md]
    key_points: ["A point."]
    expected_route: fast
    rationale: "One retrieval."
"""


def test_a_repeated_question_id_is_refused(tmp_path):
    path = _write(
        tmp_path,
        "questions:"
        + QUESTION.format(id="en-01", lang="en")
        + QUESTION.format(id="en-01", lang="en"),
    )

    with pytest.raises(GoldenSetError, match="en-01"):
        load_golden(path)


def test_an_unknown_language_is_refused(tmp_path):
    path = _write(tmp_path, "questions:" + QUESTION.format(id="fr-01", lang="fr"))

    with pytest.raises(GoldenSetError, match="lang"):
        load_golden(path)


def test_an_unreadable_file_is_refused(tmp_path):
    with pytest.raises(GoldenSetError):
        load_golden(tmp_path / "missing.yaml")

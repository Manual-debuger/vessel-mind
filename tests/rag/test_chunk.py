"""Markdown chunks by heading: each keeps its source path, heading path and anchor."""

from vessel_mind.rag.chunk import chunk_markdown, estimate_tokens
from vessel_mind.rag.corpus import Document


def _doc(text: str, path: str = "docs/specs/a-agent-track.md") -> Document:
    return Document(
        source_name="vessel-mind",
        repo="Manual-debuger/vessel-mind",
        commit="0" * 40,
        path=path,
        text=text,
    )


def _words(n: int) -> str:
    return " ".join(f"word{i}" for i in range(n))


def test_each_chunk_keeps_its_source_path_heading_path_and_anchor():
    text = (
        "# A1–A4 — Agent track\n\n"
        + _words(450)
        + "\n\n## 5.1 `ChatModel` seam (A1)\n\n"
        + _words(450)
        + "\n"
    )

    chunks = chunk_markdown(_doc(text))

    assert [c.anchor for c in chunks] == ["a1a4--agent-track", "51-chatmodel-seam-a1"]
    second = chunks[1]
    assert second.heading_path == ("A1–A4 — Agent track", "5.1 ChatModel seam (A1)")
    assert second.source_name == "vessel-mind"
    assert second.path == "docs/specs/a-agent-track.md"
    assert second.source == "vessel-mind/docs/specs/a-agent-track.md#51-chatmodel-seam-a1"
    assert second.text.startswith("## 5.1 `ChatModel` seam (A1)")


def test_small_subsections_merge_into_their_parent_chunk():
    text = "# Glossary\n\nIntro.\n\n## Session\n\nOne conversation.\n\n## Turn\n\nOne message.\n"

    chunks = chunk_markdown(_doc(text, path="GLOSSARY.md"))

    assert len(chunks) == 1
    assert chunks[0].anchor == "glossary"
    assert "One conversation." in chunks[0].text
    assert "One message." in chunks[0].text


def test_a_section_never_merges_into_a_sibling_or_a_shallower_heading():
    text = "## Session\n\nOne conversation.\n\n## Turn\n\nOne message.\n"

    chunks = chunk_markdown(_doc(text, path="GLOSSARY.md"))

    assert [c.anchor for c in chunks] == ["session", "turn"]


def test_a_long_section_splits_into_parts_with_the_same_anchor():
    paragraphs = "\n\n".join(_words(200) for _ in range(8))
    text = "# Retrieval\n\n" + paragraphs + "\n"

    chunks = chunk_markdown(_doc(text))

    assert len(chunks) > 1
    assert {c.anchor for c in chunks} == {"retrieval"}
    assert all(estimate_tokens(c.text) <= 800 for c in chunks)
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))


def test_a_hash_line_inside_a_code_fence_is_not_a_heading():
    text = "# Setup\n\n```sh\n# install\nuv sync\n```\n"

    chunks = chunk_markdown(_doc(text))

    assert [c.anchor for c in chunks] == ["setup"]


def test_repeated_headings_get_numbered_anchors_like_github():
    text = "## Example\n\n" + _words(500) + "\n\n## Example\n\n" + _words(500) + "\n"

    chunks = chunk_markdown(_doc(text))

    assert [c.anchor for c in chunks] == ["example", "example-1"]


def test_text_before_the_first_heading_has_no_anchor():
    text = "Badges and a tagline.\n\n# Title\n\nBody.\n"

    chunks = chunk_markdown(_doc(text, path="README.md"))

    assert chunks[0].anchor == ""
    assert chunks[0].source == "vessel-mind/README.md"
    assert chunks[1].anchor == "title"

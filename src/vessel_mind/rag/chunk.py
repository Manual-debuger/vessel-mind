"""Markdown chunking by heading (docs/specs/a-agent-track.md §5.6).

A chunk is one heading's section, with its heading path and the GitHub
anchor of that heading, so a citation points to a section, not a file.
Short subsections merge into their parent's chunk while it is under the
target; a section over the maximum splits at paragraph breaks, and every
part keeps the section's anchor.
"""

import math
import re
from dataclasses import dataclass

from vessel_mind.rag.corpus import Document

TARGET_TOKENS = 500
MAX_TOKENS = 800

_HEADING = re.compile(r"^(#{1,6})[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*$")
_FENCE = re.compile(r"^[ ]{0,3}(`{3,}|~{3,})")
_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_TOKEN = re.compile(r"[㐀-鿿豈-﫿]|\w+|[^\w\s]")


@dataclass(frozen=True)
class Chunk:
    source_name: str
    path: str
    ordinal: int
    anchor: str
    heading_path: tuple[str, ...]
    text: str

    @property
    def source(self) -> str:
        return source_ref(self.source_name, self.path, self.anchor)

    @property
    def embedding_text(self) -> str:
        return " > ".join(self.heading_path) + "\n\n" + self.text


def source_ref(source_name: str, path: str, anchor: str) -> str:
    """Citation form: ``<source name>/<path>#<anchor>``, no ``#`` when there is no heading."""
    base = f"{source_name}/{path}"
    return f"{base}#{anchor}" if anchor else base


def estimate_tokens(text: str) -> int:
    """A tokenizer-free estimate: words and punctuation, plus one per CJK character."""
    return math.ceil(len(_TOKEN.findall(text)) * 1.3)


@dataclass
class _Section:
    level: int  # 0 for text before the first heading
    heading_path: tuple[str, ...]
    anchor: str
    lines: list[str]

    @property
    def text(self) -> str:
        return "\n".join(self.lines).strip("\n")


def chunk_markdown(
    document: Document, *, target_tokens: int = TARGET_TOKENS, max_tokens: int = MAX_TOKENS
) -> list[Chunk]:
    merged: list[_Section] = []
    for section in _sections(document.text):
        if not section.text.strip():
            continue
        current = merged[-1] if merged else None
        if (
            current is not None
            and current.level > 0
            and section.level > current.level
            and estimate_tokens(current.text) < target_tokens
            and estimate_tokens(current.text) + estimate_tokens(section.text) <= max_tokens
        ):
            current.lines.extend(["", *section.lines])
        else:
            merged.append(section)

    chunks: list[Chunk] = []
    for section in merged:
        for part in _split(section.text, max_tokens):
            chunks.append(
                Chunk(
                    source_name=document.source_name,
                    path=document.path,
                    ordinal=len(chunks),
                    anchor=section.anchor,
                    heading_path=section.heading_path,
                    text=part,
                )
            )
    return chunks


def _sections(text: str) -> list[_Section]:
    sections = [_Section(level=0, heading_path=(), anchor="", lines=[])]
    stack: list[tuple[int, str]] = []
    seen: dict[str, int] = {}
    fence: str | None = None
    for line in text.splitlines():
        fence_match = _FENCE.match(line)
        if fence_match:
            marker = fence_match.group(1)
            if fence is None:
                fence = marker
            elif marker[0] == fence[0] and len(marker) >= len(fence):
                fence = None
        heading = _HEADING.match(line) if fence is None and not fence_match else None
        if heading is None:
            sections[-1].lines.append(line)
            continue
        level = len(heading.group(1))
        title = _plain(heading.group(2))
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, title))
        sections.append(
            _Section(
                level=level,
                heading_path=tuple(t for _, t in stack),
                anchor=_anchor(title, seen),
                lines=[line],
            )
        )
    return sections


def _plain(heading: str) -> str:
    return _LINK.sub(r"\1", heading).replace("`", "").strip()


def _anchor(title: str, seen: dict[str, int]) -> str:
    """GitHub's heading id: lower case, punctuation dropped, spaces to hyphens, repeats numbered."""
    slug = re.sub(r"[^\w\- ]", "", title.lower()).replace(" ", "-")
    count = seen.get(slug, 0)
    seen[slug] = count + 1
    return slug if count == 0 else f"{slug}-{count}"


def _split(text: str, max_tokens: int) -> list[str]:
    if estimate_tokens(text) <= max_tokens:
        return [text]
    parts: list[str] = []
    current: list[str] = []
    for paragraph in _pieces(text, max_tokens):
        candidate = "\n\n".join([*current, paragraph])
        if current and estimate_tokens(candidate) > max_tokens:
            parts.append("\n\n".join(current))
            current = [paragraph]
        else:
            current.append(paragraph)
    if current:
        parts.append("\n\n".join(current))
    return parts


def _pieces(text: str, max_tokens: int) -> list[str]:
    """Paragraphs, with any paragraph over the maximum cut into word runs that fit."""
    pieces: list[str] = []
    for paragraph in re.split(r"\n[ \t]*\n", text):
        if not paragraph.strip():
            continue
        if estimate_tokens(paragraph) <= max_tokens:
            pieces.append(paragraph)
            continue
        run: list[str] = []
        for word in paragraph.split(" "):
            if run and estimate_tokens(" ".join([*run, word])) > max_tokens:
                pieces.append(" ".join(run))
                run = []
            run.append(word)
        if run:
            pieces.append(" ".join(run))
    return pieces

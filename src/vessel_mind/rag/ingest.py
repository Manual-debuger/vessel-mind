"""Ingest the pinned corpus into the retrieval index (``task ingest``).

Each document is hashed; an unchanged document is a no-op, a changed one is
re-chunked and re-embedded alone, and one dropped from the corpus is removed.
``ensure_index`` is the startup hook: it ingests only when the index is
missing or an earlier ingest did not finish.
"""

import argparse
import hashlib
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from vessel_mind.rag.chunk import chunk_markdown
from vessel_mind.rag.corpus import CorpusPinError, Document, load_corpus, read_corpus
from vessel_mind.rag.embed import DEFAULT_MODEL, Embedder, FastEmbedEmbedder
from vessel_mind.rag.store import IndexStore

DEFAULT_CORPUS = Path("eval/corpus.yaml")
DEFAULT_INDEX = Path("data/index.sqlite")
DEFAULT_TARBALL_CACHE = Path(".cache/corpus")
DEFAULT_MODEL_CACHE = Path(".cache/fastembed")


@dataclass(frozen=True)
class IngestReport:
    added: int
    updated: int
    unchanged: int
    removed: int
    chunks_embedded: int


def ingest(documents: Iterable[Document], store: IndexStore, embedder: Embedder) -> IngestReport:
    added = updated = unchanged = embedded = 0
    seen: list[tuple[str, str]] = []
    store.set_complete(False)
    for document in documents:
        seen.append((document.source_name, document.path))
        content_hash = hashlib.sha256(document.text.encode("utf-8")).hexdigest()
        previous = store.content_hash(document.source_name, document.path)
        if previous == content_hash:
            unchanged += 1
            continue
        chunks = chunk_markdown(document)
        vectors = embedder.embed_documents([chunk.embedding_text for chunk in chunks])
        store.replace_document(document, content_hash, chunks, vectors)
        embedded += len(chunks)
        if previous is None:
            added += 1
        else:
            updated += 1
    removed = store.remove_documents_except(seen)
    store.set_complete(True)
    return IngestReport(added, updated, unchanged, removed, embedded)


def ensure_index(
    index_path: Path, load_documents: Callable[[], Iterable[Document]], embedder: Embedder
) -> IngestReport | None:
    """Ingest at startup when the index is missing or unfinished; otherwise do nothing.

    Returns None when the index was already complete: the corpus is not read
    and nothing is embedded. Blocking: call it from a worker thread in an
    async app.
    """
    with IndexStore.open(index_path, embedder) as store:
        if store.is_complete():
            return None
        return ingest(load_documents(), store, embedder)


def corpus_loader(
    corpus_file: Path = DEFAULT_CORPUS,
    *,
    repo_root: Path = Path(),
    cache_dir: Path = DEFAULT_TARBALL_CACHE,
) -> Callable[[], list[Document]]:
    """The loader ``ensure_index`` takes, reading the pinned corpus. Raises CorpusPinError."""

    def load() -> list[Document]:
        return read_corpus(load_corpus(corpus_file), repo_root=repo_root, cache_dir=cache_dir)

    return load


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="task ingest", description="Ingest the pinned corpus into the retrieval index."
    )
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--index", type=Path, default=DEFAULT_INDEX)
    parser.add_argument("--repo-root", type=Path, default=Path())
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_TARBALL_CACHE)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--model-cache-dir", type=Path, default=DEFAULT_MODEL_CACHE)
    args = parser.parse_args(argv)

    load = corpus_loader(args.corpus, repo_root=args.repo_root, cache_dir=args.cache_dir)
    try:
        documents = load()
    except CorpusPinError as exc:
        print(f"ingest refused: {exc}", file=sys.stderr)
        return 2
    embedder = FastEmbedEmbedder(args.model, cache_dir=args.model_cache_dir)
    with IndexStore.open(args.index, embedder) as store:
        report = ingest(documents, store, embedder)
    print(
        f"{args.index}: {report.added} added, {report.updated} updated, "
        f"{report.unchanged} unchanged, {report.removed} removed; "
        f"{report.chunks_embedded} chunks embedded with {embedder.model_id}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

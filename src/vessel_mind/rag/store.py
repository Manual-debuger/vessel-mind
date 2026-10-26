"""The retrieval index: one SQLite file with sqlite-vec vectors and FTS5 text.

The index is separate from the server's state file (§5.3): it can be rebuilt
from the corpus, the state cannot. So the store refuses a file that holds any
table it does not own, and never writes into one.

Schema (``chunks.id`` is the rowid in both virtual tables):

    meta(key, value)                   embedding_model, embedding_dim, complete
    documents(id, source_name, repo, commit_sha, path, content_hash)
    chunks(id, document_id, ordinal, anchor, heading_path JSON, text)
    chunks_fts   fts5(heading, text)   heading = the heading path, " > " joined
    chunks_vec   vec0(embedding float[dim] distance_metric=cosine)
"""

import json
import re
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Self

import sqlite_vec

from vessel_mind.rag.chunk import Chunk, source_ref
from vessel_mind.rag.corpus import Document
from vessel_mind.rag.embed import Embedder

_OWN_TABLES = {"meta", "documents", "chunks", "chunks_fts", "chunks_vec"}
_WORD = re.compile(r"\w+")


class NotAnIndexError(Exception):
    """The file holds tables the retrieval index does not own, such as the state file's."""


@dataclass(frozen=True)
class StoredChunk:
    id: int
    source_name: str
    repo: str
    commit: str
    path: str
    ordinal: int
    anchor: str
    heading_path: tuple[str, ...]
    text: str

    @property
    def source(self) -> str:
        return source_ref(self.source_name, self.path, self.anchor)


class IndexStore:
    def __init__(self, db: sqlite3.Connection) -> None:
        self._db = db

    @classmethod
    def open(cls, path: Path, embedder: Embedder) -> Self:
        """Open or create the index at ``path`` for ``embedder``'s model.

        An index built with another model or dimension is emptied, so the next
        ingest rebuilds it.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(path)
        try:
            foreign = sorted(
                name
                for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
                if not _owned(name)
            )
            if foreign:
                raise NotAnIndexError(
                    f"{path} is not a retrieval index: it holds the tables {', '.join(foreign)}"
                )
            db.enable_load_extension(True)
            sqlite_vec.load(db)
            db.enable_load_extension(False)
            db.execute("PRAGMA foreign_keys = ON")
            store = cls(db)
            store._prepare(embedder.model_id, embedder.dim)
        except BaseException:
            db.close()
            raise
        return store

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._db.close()

    def _prepare(self, model_id: str, dim: int) -> None:
        with self._db:
            self._db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
            built = dict(self._db.execute("SELECT key, value FROM meta").fetchall())
            if built and (built.get("embedding_model"), built.get("embedding_dim")) != (
                model_id,
                str(dim),
            ):
                for table in ("chunks_vec", "chunks_fts", "chunks", "documents"):
                    self._db.execute(f"DROP TABLE IF EXISTS {table}")
                self._db.execute("DELETE FROM meta")
            self._db.executescript(
                f"""
                CREATE TABLE IF NOT EXISTS documents (
                    id INTEGER PRIMARY KEY,
                    source_name TEXT NOT NULL,
                    repo TEXT NOT NULL,
                    commit_sha TEXT NOT NULL,
                    path TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    UNIQUE (source_name, path)
                );
                CREATE TABLE IF NOT EXISTS chunks (
                    id INTEGER PRIMARY KEY,
                    document_id INTEGER NOT NULL REFERENCES documents (id) ON DELETE CASCADE,
                    ordinal INTEGER NOT NULL,
                    anchor TEXT NOT NULL,
                    heading_path TEXT NOT NULL,
                    text TEXT NOT NULL
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(heading, text);
                CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vec
                    USING vec0(embedding float[{int(dim)}] distance_metric=cosine);
                """
            )
            self._db.executemany(
                "INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)",
                [("embedding_model", model_id), ("embedding_dim", str(dim)), ("complete", "0")],
            )

    # Ingest bookkeeping

    def is_complete(self) -> bool:
        row = self._db.execute("SELECT value FROM meta WHERE key = 'complete'").fetchone()
        return row is not None and row[0] == "1"

    def set_complete(self, complete: bool) -> None:
        with self._db:
            self._db.execute(
                "UPDATE meta SET value = ? WHERE key = 'complete'", ("1" if complete else "0",)
            )

    def content_hash(self, source_name: str, path: str) -> str | None:
        row = self._db.execute(
            "SELECT content_hash FROM documents WHERE source_name = ? AND path = ?",
            (source_name, path),
        ).fetchone()
        return None if row is None else row[0]

    def replace_document(
        self,
        document: Document,
        content_hash: str,
        chunks: Sequence[Chunk],
        vectors: Sequence[Sequence[float]],
    ) -> None:
        if len(chunks) != len(vectors):
            raise ValueError(f"{len(chunks)} chunks but {len(vectors)} vectors")
        with self._db:
            self._delete_document(document.source_name, document.path)
            cursor = self._db.execute(
                "INSERT INTO documents (source_name, repo, commit_sha, path, content_hash)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    document.source_name,
                    document.repo,
                    document.commit,
                    document.path,
                    content_hash,
                ),
            )
            document_id = cursor.lastrowid
            for chunk, vector in zip(chunks, vectors, strict=True):
                chunk_id = self._db.execute(
                    "INSERT INTO chunks (document_id, ordinal, anchor, heading_path, text)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (
                        document_id,
                        chunk.ordinal,
                        chunk.anchor,
                        json.dumps(chunk.heading_path, ensure_ascii=False),
                        chunk.text,
                    ),
                ).lastrowid
                self._db.execute(
                    "INSERT INTO chunks_fts (rowid, heading, text) VALUES (?, ?, ?)",
                    (chunk_id, " > ".join(chunk.heading_path), chunk.text),
                )
                self._db.execute(
                    "INSERT INTO chunks_vec (rowid, embedding) VALUES (?, ?)",
                    (chunk_id, sqlite_vec.serialize_float32(list(vector))),
                )

    def remove_documents_except(self, keep: Iterable[tuple[str, str]]) -> int:
        """Remove every document whose (source name, path) is not in ``keep``."""
        wanted = set(keep)
        stale = [
            (source_name, path)
            for source_name, path in self._db.execute("SELECT source_name, path FROM documents")
            if (source_name, path) not in wanted
        ]
        with self._db:
            for source_name, path in stale:
                self._delete_document(source_name, path)
        return len(stale)

    def _delete_document(self, source_name: str, path: str) -> None:
        ids = "SELECT c.id FROM chunks c JOIN documents d ON d.id = c.document_id"
        ids += " WHERE d.source_name = ? AND d.path = ?"
        self._db.execute(f"DELETE FROM chunks_fts WHERE rowid IN ({ids})", (source_name, path))  # noqa: S608 -- constant SQL, values are bound
        self._db.execute(f"DELETE FROM chunks_vec WHERE rowid IN ({ids})", (source_name, path))  # noqa: S608 -- constant SQL, values are bound
        self._db.execute(
            "DELETE FROM documents WHERE source_name = ? AND path = ?", (source_name, path)
        )

    # Reads

    def count_chunks(self) -> int:
        return self._db.execute("SELECT count(*) FROM chunks").fetchone()[0]

    def chunk(self, chunk_id: int) -> StoredChunk:
        row = self._db.execute(
            "SELECT c.id, d.source_name, d.repo, d.commit_sha, d.path,"
            " c.ordinal, c.anchor, c.heading_path, c.text"
            " FROM chunks c JOIN documents d ON d.id = c.document_id WHERE c.id = ?",
            (chunk_id,),
        ).fetchone()
        if row is None:
            raise KeyError(chunk_id)
        return StoredChunk(
            id=row[0],
            source_name=row[1],
            repo=row[2],
            commit=row[3],
            path=row[4],
            ordinal=row[5],
            anchor=row[6],
            heading_path=tuple(json.loads(row[7])),
            text=row[8],
        )

    def nearest(self, vector: Sequence[float], k: int) -> list[tuple[int, float]]:
        """The ``k`` chunks nearest to ``vector``: (chunk id, cosine distance), nearest first."""
        return self._db.execute(
            "SELECT rowid, distance FROM chunks_vec WHERE embedding MATCH ? AND k = ?"
            " ORDER BY distance",
            (sqlite_vec.serialize_float32(list(vector)), k),
        ).fetchall()

    def keyword(self, query: str, k: int) -> list[tuple[int, float]]:
        """The ``k`` best FTS5 matches for any word of ``query``: (chunk id, bm25), best first.

        bm25 is lower for a better match. Each word is quoted, so query
        punctuation never reaches FTS5's query syntax.
        """
        words = _WORD.findall(query)
        if not words:
            return []
        match = " OR ".join('"' + word.replace('"', '""') + '"' for word in words)
        return self._db.execute(
            "SELECT rowid, bm25(chunks_fts) FROM chunks_fts WHERE chunks_fts MATCH ?"
            " ORDER BY bm25(chunks_fts) LIMIT ?",
            (match, k),
        ).fetchall()


def _owned(table: str) -> bool:
    return (
        table in _OWN_TABLES
        or table.startswith(("chunks_fts_", "chunks_vec_"))
        or table.startswith("sqlite_")
    )

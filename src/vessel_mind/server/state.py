"""The state file: sessions, their turns and their history, in SQLite.

It is separate from the retrieval index. The index can be rebuilt from the corpus;
the history cannot.

A message is stored as the JSON of its ChatMessage, so a new message kind (a tool
call, a tool result) needs no schema change: it needs only a new member of the
ChatMessage union.
"""

import datetime
import sqlite3
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal

from pydantic import TypeAdapter

from vessel_mind.core.chat import ChatMessage

# A turn's status. "running" is the only status that is not final. A turn still
# running when the state file is opened was cut off by a crash: it becomes
# "interrupted" and is never resumed.
TurnStatus = Literal["running", "done", "cancelled", "error", "interrupted"]

# Each entry moves the schema from version i to i + 1 (PRAGMA user_version).
# Append to this list; never edit an entry that has shipped.
MIGRATIONS: list[str] = [
    """
    CREATE TABLE sessions (
        id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL
    );
    CREATE TABLE turns (
        id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL REFERENCES sessions(id),
        status TEXT NOT NULL
            CHECK (status IN ('running', 'done', 'cancelled', 'error', 'interrupted')),
        started_at TEXT NOT NULL,
        ended_at TEXT
    );
    CREATE INDEX turns_by_session ON turns(session_id);
    CREATE TABLE messages (
        session_id TEXT NOT NULL REFERENCES sessions(id),
        seq INTEGER NOT NULL,
        turn_id TEXT NOT NULL REFERENCES turns(id),
        role TEXT NOT NULL,
        body TEXT NOT NULL,
        PRIMARY KEY (session_id, seq)
    );
    """,
]

_messages: TypeAdapter[Any] = TypeAdapter(ChatMessage)


class StateError(Exception):
    """The state file cannot be used, for example because newer code wrote it."""


class UnknownSession(Exception):
    """No session has this id."""


class TurnLimitReached(Exception):
    """The session has started as many turns as it may."""


def _now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()


class SessionStore:
    """Sessions, turns and history in one SQLite file. Open it with SessionStore.open."""

    def __init__(self, db: sqlite3.Connection) -> None:
        self._db = db

    @classmethod
    def open(cls, path: Path | str) -> "SessionStore":
        """Opens the file, migrates it, and marks turns left running as interrupted.

        ":memory:" gives a store that lasts as long as the process.
        """
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        # The store is used from the event loop's thread only, which may differ from
        # the thread that opened it (the test client runs the app in its own thread).
        db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("PRAGMA journal_mode = WAL")
        store = cls(db)
        store._migrate()
        store._recover()
        return store

    def close(self) -> None:
        self._db.close()

    @property
    def schema_version(self) -> int:
        return self._db.execute("PRAGMA user_version").fetchone()[0]

    def _migrate(self) -> None:
        version = self.schema_version
        if version > len(MIGRATIONS):
            raise StateError(
                f"the state file has schema version {version}; "
                f"this code knows versions up to {len(MIGRATIONS)}"
            )
        for target, script in enumerate(MIGRATIONS[version:], start=version + 1):
            # executescript commits first, so the script carries its own transaction.
            self._db.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {target};\nCOMMIT;")

    def _recover(self) -> None:
        self._db.execute(
            "UPDATE turns SET status = 'interrupted', ended_at = ? WHERE status = 'running'",
            (_now(),),
        )

    def create_session(self) -> str:
        session_id = uuid.uuid4().hex
        self._db.execute(
            "INSERT INTO sessions (id, created_at) VALUES (?, ?)", (session_id, _now())
        )
        return session_id

    def has_session(self, session_id: str) -> bool:
        row = self._db.execute("SELECT 1 FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return row is not None

    def history(self, session_id: str) -> list[ChatMessage]:
        """The session's messages, oldest first."""
        if not self.has_session(session_id):
            raise UnknownSession(session_id)
        rows = self._db.execute(
            "SELECT body FROM messages WHERE session_id = ? ORDER BY seq", (session_id,)
        )
        return [_messages.validate_json(body) for (body,) in rows]

    def turn_count(self, session_id: str) -> int:
        """Every turn the session started, whatever its status."""
        return self._db.execute(
            "SELECT count(*) FROM turns WHERE session_id = ?", (session_id,)
        ).fetchone()[0]

    def begin_turn(self, session_id: str, *, max_turns: int | None = None) -> str:
        """Records a running turn and returns its id.

        Raises UnknownSession, or TurnLimitReached when the session has already
        started max_turns turns.
        """
        with self._transaction():
            if not self.has_session(session_id):
                raise UnknownSession(session_id)
            if max_turns is not None and self.turn_count(session_id) >= max_turns:
                raise TurnLimitReached(session_id)
            turn_id = uuid.uuid4().hex
            self._db.execute(
                "INSERT INTO turns (id, session_id, status, started_at) "
                "VALUES (?, ?, 'running', ?)",
                (turn_id, session_id, _now()),
            )
        return turn_id

    def finish_turn(
        self,
        turn_id: str,
        status: TurnStatus,
        messages: Sequence[ChatMessage] = (),
    ) -> None:
        """Ends a turn and appends its messages to the session's history, in one transaction."""
        with self._transaction():
            row = self._db.execute(
                "SELECT session_id FROM turns WHERE id = ?", (turn_id,)
            ).fetchone()
            if row is None:
                raise KeyError(turn_id)
            session_id = row[0]
            self._db.execute(
                "UPDATE turns SET status = ?, ended_at = ? WHERE id = ?",
                (status, _now(), turn_id),
            )
            (seq,) = self._db.execute(
                "SELECT coalesce(max(seq), 0) FROM messages WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            self._db.executemany(
                "INSERT INTO messages (session_id, seq, turn_id, role, body) "
                "VALUES (?, ?, ?, ?, ?)",
                [
                    (session_id, seq + i, turn_id, m.role, _messages.dump_json(m).decode())
                    for i, m in enumerate(messages, start=1)
                ],
            )

    def turn_status(self, turn_id: str) -> TurnStatus | None:
        row = self._db.execute("SELECT status FROM turns WHERE id = ?", (turn_id,)).fetchone()
        return None if row is None else row[0]

    def _transaction(self) -> "_Transaction":
        return _Transaction(self._db)


class _Transaction:
    """BEGIN IMMEDIATE ... COMMIT, or ROLLBACK when the block raises."""

    def __init__(self, db: sqlite3.Connection) -> None:
        self._db = db

    def __enter__(self) -> None:
        self._db.execute("BEGIN IMMEDIATE")

    def __exit__(self, kind: object, *_: object) -> None:
        self._db.execute("ROLLBACK" if kind is not None else "COMMIT")

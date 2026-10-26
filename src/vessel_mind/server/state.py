"""The state file: sessions, their turns and their history, in SQLite.

It is separate from the retrieval index. The index can be rebuilt from the corpus;
the history cannot.

A message is stored as the JSON of its ChatMessage, so a new message kind (a tool
call, a tool result) needs no schema change: it needs only a new member of the
ChatMessage union.
"""

import datetime
import json
import sqlite3
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import JsonValue, TypeAdapter

from vessel_mind.core.chat import ChatMessage, ToolCall

Clock = Callable[[], datetime.datetime]

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
    # 2: proposals. Times are ISO-8601 UTC with microseconds, so they compare as text.
    """
    CREATE TABLE proposals (
        id TEXT PRIMARY KEY,
        turn_id TEXT NOT NULL REFERENCES turns(id),
        call_id TEXT NOT NULL,
        name TEXT NOT NULL,
        input TEXT NOT NULL,
        effect TEXT NOT NULL CHECK (effect IN ('write', 'irreversible')),
        status TEXT NOT NULL
            CHECK (status IN ('pending', 'approved', 'rejected', 'expired', 'withdrawn')),
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        resolved_at TEXT,
        result TEXT,
        is_error INTEGER
    );
    CREATE INDEX proposals_by_turn ON proposals(turn_id);
    """,
]

# A proposal's status. "pending" is the only status that is not final.
#   approved:  an approval claimed it; it executes once. result is set when it has run.
#   rejected:  a human rejected it; it never runs.
#   expired:   nobody decided in time, or the server restarted while it was pending.
#   withdrawn: its turn was cancelled or failed, so the conversation never kept the call.
ProposalStatus = Literal["pending", "approved", "rejected", "expired", "withdrawn"]
ProposalEffect = Literal["write", "irreversible"]

_messages: TypeAdapter[Any] = TypeAdapter(ChatMessage)
_json: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)


@dataclass(frozen=True)
class StoredProposal:
    id: str
    turn_id: str
    call_id: str
    name: str
    input: dict[str, Any]
    effect: ProposalEffect
    status: ProposalStatus
    expires_at: datetime.datetime
    # Set once an approved proposal has executed.
    result: JsonValue | None
    is_error: bool | None


class UnknownProposal(Exception):
    """No proposal has this id."""


class StateError(Exception):
    """The state file cannot be used, for example because newer code wrote it."""


class UnknownSession(Exception):
    """No session has this id."""


class TurnLimitReached(Exception):
    """The session has started as many turns as it may."""


def _utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def _text(when: datetime.datetime) -> str:
    return when.astimezone(datetime.UTC).isoformat(timespec="microseconds")


class SessionStore:
    """Sessions, turns, history and proposals in one SQLite file. Open it with SessionStore.open."""

    def __init__(self, db: sqlite3.Connection, clock: Clock = _utc_now) -> None:
        self._db = db
        self._clock = clock

    @classmethod
    def open(cls, path: Path | str, *, clock: Clock | None = None) -> "SessionStore":
        """Opens the file, migrates it, marks turns left running as interrupted and
        expires every pending proposal.

        ":memory:" gives a store that lasts as long as the process. clock gives the
        current time (an aware datetime); tests pass a fake one.
        """
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        # The store is used from the event loop's thread only, which may differ from
        # the thread that opened it (the test client runs the app in its own thread).
        db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("PRAGMA journal_mode = WAL")
        store = cls(db, clock or _utc_now)
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

    def _now(self) -> str:
        return _text(self._clock())

    def _recover(self) -> None:
        now = self._now()
        with self._transaction():
            self._db.execute(
                "UPDATE turns SET status = 'interrupted', ended_at = ? WHERE status = 'running'",
                (now,),
            )
            # Nobody can have seen a pending proposal's card since the restart, and
            # the approval would no longer match what the person was shown.
            self._db.execute(
                "UPDATE proposals SET status = 'expired', resolved_at = ? WHERE status = 'pending'",
                (now,),
            )

    def create_session(self) -> str:
        session_id = uuid.uuid4().hex
        self._db.execute(
            "INSERT INTO sessions (id, created_at) VALUES (?, ?)", (session_id, self._now())
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
                (turn_id, session_id, self._now()),
            )
        return turn_id

    def finish_turn(
        self,
        turn_id: str,
        status: TurnStatus,
        messages: Sequence[ChatMessage] = (),
    ) -> None:
        """Ends a turn and appends its messages to the session's history, in one transaction.

        A turn that ends other than "done" writes no history, so the conversation
        never kept its calls: its pending proposals are withdrawn.
        """
        with self._transaction():
            row = self._db.execute(
                "SELECT session_id FROM turns WHERE id = ?", (turn_id,)
            ).fetchone()
            if row is None:
                raise KeyError(turn_id)
            session_id = row[0]
            now = self._now()
            self._db.execute(
                "UPDATE turns SET status = ?, ended_at = ? WHERE id = ?",
                (status, now, turn_id),
            )
            if status != "done":
                self._db.execute(
                    "UPDATE proposals SET status = 'withdrawn', resolved_at = ? "
                    "WHERE turn_id = ? AND status = 'pending'",
                    (now, turn_id),
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

    def create_proposal(
        self,
        turn_id: str,
        call: ToolCall,
        effect: ProposalEffect,
        ttl: datetime.timedelta,
    ) -> StoredProposal:
        """Stores a pending proposal for one call of a running turn."""
        proposal_id = uuid.uuid4().hex
        now = self._clock()
        self._db.execute(
            "INSERT INTO proposals (id, turn_id, call_id, name, input, effect, status, "
            "created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
            (
                proposal_id,
                turn_id,
                call.id,
                call.name,
                json.dumps(call.input),
                effect,
                _text(now),
                _text(now + ttl),
            ),
        )
        return self.proposal(proposal_id)

    def proposal(self, proposal_id: str) -> StoredProposal:
        """The proposal as it stands now. Raises UnknownProposal."""
        with self._transaction():
            self._expire_due(proposal_id)
            return self._read_proposal(proposal_id)

    def claim_proposal(self, proposal_id: str) -> tuple[StoredProposal, bool]:
        """Marks a pending, unexpired proposal approved, at most once.

        Returns the proposal and True only to the one caller that claimed it, which
        must then execute it and record_result. Raises UnknownProposal.
        """
        with self._transaction():
            self._expire_due(proposal_id)
            claimed = self._db.execute(
                "UPDATE proposals SET status = 'approved', resolved_at = ? "
                "WHERE id = ? AND status = 'pending'",
                (self._now(), proposal_id),
            ).rowcount
            return self._read_proposal(proposal_id), claimed == 1

    def record_result(self, proposal_id: str, result: JsonValue, is_error: bool) -> StoredProposal:
        """Stores what an approved proposal returned when it executed."""
        with self._transaction():
            self._db.execute(
                "UPDATE proposals SET result = ?, is_error = ? WHERE id = ? AND status = 'approved'",
                (json.dumps(result), int(is_error), proposal_id),
            )
            return self._read_proposal(proposal_id)

    def reject_proposal(self, proposal_id: str) -> StoredProposal:
        """Marks a pending, unexpired proposal rejected. Returns it as it stands.

        Raises UnknownProposal.
        """
        with self._transaction():
            self._expire_due(proposal_id)
            self._db.execute(
                "UPDATE proposals SET status = 'rejected', resolved_at = ? "
                "WHERE id = ? AND status = 'pending'",
                (self._now(), proposal_id),
            )
            return self._read_proposal(proposal_id)

    def _expire_due(self, proposal_id: str) -> None:
        now = self._now()
        self._db.execute(
            "UPDATE proposals SET status = 'expired', resolved_at = ? "
            "WHERE id = ? AND status = 'pending' AND expires_at <= ?",
            (now, proposal_id, now),
        )

    def _read_proposal(self, proposal_id: str) -> StoredProposal:
        row = self._db.execute(
            "SELECT id, turn_id, call_id, name, input, effect, status, expires_at, result, "
            "is_error FROM proposals WHERE id = ?",
            (proposal_id,),
        ).fetchone()
        if row is None:
            raise UnknownProposal(proposal_id)
        id_, turn_id, call_id, name, input_, effect, status, expires_at, result, is_error = row
        return StoredProposal(
            id=id_,
            turn_id=turn_id,
            call_id=call_id,
            name=name,
            input=json.loads(input_),
            effect=effect,
            status=status,
            expires_at=datetime.datetime.fromisoformat(expires_at),
            result=None if result is None else _json.validate_json(result),
            is_error=None if is_error is None else bool(is_error),
        )

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

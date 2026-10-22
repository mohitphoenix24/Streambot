"""SQLite persistence: conversations and messages.

Design notes (the "why" behind the schema):

* conversations and messages are SEPARATE tables. A conversation is one thing
  with a title; it owns MANY messages. Putting both in one table would repeat the
  title on every row and make "list my conversations" scan every message.

* messages.conversation_id is a FOREIGN KEY. The database itself refuses a
  message that points at a conversation which does not exist, and
  ON DELETE CASCADE removes a conversation's messages with it. Integrity is
  enforced by the database, not by hoping every code path remembers.

* Token usage and cost live on the ASSISTANT message row. Tokens are consumed by
  one specific model call, and that call produces exactly one assistant message.
  So "what did this reply cost?" is a column read, and "what did this
  conversation cost?" is a SUM over its rows. User rows leave those columns NULL.

* messages.status exists because a reply is a PROCESS, not an instant. While
  tokens stream in, the row is 'streaming'; it ends as 'complete', 'cancelled'
  (user pressed Stop) or 'error'. Without it you could not tell a finished
  answer from one that died halfway. See the state diagram in README.md.

* NULL means "unknown", never 0. cost_usd NULL = "no price configured", which is
  honest; 0.0 would be a lie.

Every method opens a short-lived connection. SQLite calls here are
sub-millisecond single-row operations, so running them directly inside async
code is fine for this project (the upgrade path is aiosqlite or a thread pool).
WAL mode lets readers proceed while a writer is active.
"""
from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

STATUSES = ("streaming", "complete", "cancelled", "error")
ROLES = ("user", "assistant")

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    -- Rolling memory: a summary of the oldest messages, and the id of the last message
    -- it covers. Everything after that message is still sent verbatim.
    summary             TEXT,
    summary_through_id  TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    id                TEXT PRIMARY KEY,
    conversation_id   TEXT NOT NULL
                      REFERENCES conversations(id) ON DELETE CASCADE,
    role              TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content           TEXT NOT NULL DEFAULT '',
    status            TEXT NOT NULL
                      CHECK (status IN ('streaming', 'complete', 'cancelled', 'error')),
    -- Usage REPORTED by the provider (NULL = unavailable; never estimated):
    input_tokens        INTEGER,   -- includes the cached part
    cached_input_tokens INTEGER,   -- subset of input_tokens, billed at the cached rate
    output_tokens       INTEGER,   -- includes reasoning tokens
    reasoning_tokens    INTEGER,   -- subset of output_tokens (observability only)
    total_tokens        INTEGER,
    cost_usd            REAL,      -- from usage x prices.toml; NULL = unpriced/unknown
    stop_reason       TEXT,
    -- Beyond the required fields: measured facts about each reply.
    model             TEXT,     -- REQUESTED model (what we asked for, from OPENAI_MODEL)
    served_model      TEXT,     -- model id OpenAI reports it actually served (may be a snapshot)
    ttft_ms           REAL,     -- time to first token (NULL if none arrived)
    latency_ms        REAL,     -- total time until the reply finished
    sources           TEXT,     -- JSON list of web sources cited by this reply (NULL = none)
    web_searches      INTEGER,  -- how many web searches this reply ran
    created_at        TEXT NOT NULL
);

-- Model calls that are not a chat reply (titles, summaries), so their cost is not lost.
-- No foreign key on purpose: spend history must survive deleting the conversation.
CREATE TABLE IF NOT EXISTS llm_calls (
    id                  TEXT PRIMARY KEY,
    conversation_id     TEXT,
    purpose             TEXT NOT NULL CHECK (purpose IN ('title', 'summary')),
    model               TEXT NOT NULL,
    served_model        TEXT,
    input_tokens        INTEGER,
    cached_input_tokens INTEGER,
    output_tokens       INTEGER,
    reasoning_tokens    INTEGER,
    total_tokens        INTEGER,
    cost_usd            REAL,
    created_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_llm_calls_created ON llm_calls (created_at);

-- Loading a conversation = "all messages for conversation X, oldest first".
CREATE INDEX IF NOT EXISTS idx_messages_conversation
    ON messages (conversation_id, created_at);

-- Sidebar = "conversations, most recently active first".
CREATE INDEX IF NOT EXISTS idx_conversations_updated
    ON conversations (updated_at DESC);

-- Tiny partial index: only 'streaming' rows are indexed, so crash recovery
-- ("which replies were in flight?") is instant however big the table gets.
CREATE INDEX IF NOT EXISTS idx_messages_streaming
    ON messages (status) WHERE status = 'streaming';
"""

# Columns update_message() is allowed to touch. Column names cannot be bound as
# SQL parameters, so we whitelist them rather than interpolating caller input.
_UPDATABLE = {
    "content", "status", "input_tokens", "cached_input_tokens", "output_tokens",
    "reasoning_tokens", "total_tokens", "cost_usd", "stop_reason", "model",
    "served_model", "ttft_ms", "latency_ms", "sources", "web_searches",
}

# Columns added after the first release. CREATE TABLE IF NOT EXISTS does not alter
# a table that already exists, so older database files get these via ALTER TABLE.
_ADDED_COLUMNS = {
    "messages": {
        "cached_input_tokens": "INTEGER",
        "reasoning_tokens": "INTEGER",
        "served_model": "TEXT",
        "sources": "TEXT",
        "web_searches": "INTEGER",
    },
    "conversations": {
        "summary": "TEXT",
        "summary_through_id": "TEXT",
    },
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _new_id() -> str:
    return uuid.uuid4().hex


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)  # e.g. a fresh clone has no data/ yet

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        # Foreign-key enforcement is OFF by default in SQLite and is a
        # per-connection setting, so it must be switched on every time.
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init_schema(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(SCHEMA)
            self._migrate(conn)

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Add columns introduced after a database file was first created."""
        for table, columns in _ADDED_COLUMNS.items():
            existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            for name, sql_type in columns.items():
                if name not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")

    # ── conversations ────────────────────────────────────────────────────
    def create_conversation(self, title: str = "New chat") -> dict[str, Any]:
        now = _now()
        conv = {"id": _new_id(), "title": title, "created_at": now, "updated_at": now}
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO conversations (id, title, created_at, updated_at) "
                "VALUES (:id, :title, :created_at, :updated_at)",
                conv,
            )
        return conv

    def list_conversations(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id, title, created_at, updated_at FROM conversations "
                "ORDER BY updated_at DESC, rowid DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_conversation(self, conversation_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT id, title, created_at, updated_at FROM conversations WHERE id = ?",
                (conversation_id,),
            ).fetchone()
        return dict(row) if row else None

    def delete_conversation(self, conversation_id: str) -> bool:
        """Delete a conversation; its messages go with it (ON DELETE CASCADE).
        Returns False if there was no such conversation."""
        with self.connect() as conn:
            cur = conn.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
        return cur.rowcount > 0

    def get_summary(self, conversation_id: str) -> tuple[str | None, str | None]:
        """(summary text, id of the last message it covers); (None, None) if there is none."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT summary, summary_through_id FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone()
        return (row["summary"], row["summary_through_id"]) if row and row["summary"] else (None, None)

    def set_summary(self, conversation_id: str, summary: str, through_message_id: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE conversations SET summary = ?, summary_through_id = ? WHERE id = ?",
                (summary, through_message_id, conversation_id),
            )

    def add_llm_call(self, conversation_id: str | None, purpose: str, model: str, **fields: Any) -> str:
        """Record a non-reply model call. `fields` are the usage columns and cost_usd."""
        allowed = {"served_model", "input_tokens", "cached_input_tokens", "output_tokens",
                   "reasoning_tokens", "total_tokens", "cost_usd"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"cannot record columns: {sorted(unknown)}")
        call_id = _new_id()
        columns = {"id": call_id, "conversation_id": conversation_id, "purpose": purpose,
                   "model": model, "created_at": _now(), **fields}
        with self.connect() as conn:
            conn.execute(
                f"INSERT INTO llm_calls ({', '.join(columns)}) VALUES ({', '.join(':' + c for c in columns)})",
                columns,  # column names come from the whitelist above, values are bound
            )
        return call_id

    def list_llm_calls(self, conversation_id: str | None = None) -> list[dict[str, Any]]:
        with self.connect() as conn:
            if conversation_id is None:
                rows = conn.execute("SELECT * FROM llm_calls ORDER BY created_at, rowid").fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM llm_calls WHERE conversation_id = ? ORDER BY created_at, rowid",
                    (conversation_id,),
                ).fetchall()
        return [dict(r) for r in rows]

    def set_title(self, conversation_id: str, title: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE conversations SET title = ? WHERE id = ?", (title, conversation_id)
            )

    def touch_conversation(self, conversation_id: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (_now(), conversation_id),
            )

    # ── messages ─────────────────────────────────────────────────────────
    def add_message(
        self,
        conversation_id: str,
        role: str,
        content: str = "",
        status: str = "complete",
        model: str | None = None,
    ) -> dict[str, Any]:
        """Insert a message. Raises sqlite3.IntegrityError for a bad conversation_id,
        role or status (foreign key / CHECK constraints)."""
        msg_id = _new_id()
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO messages (id, conversation_id, role, content, status, model, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (msg_id, conversation_id, role, content, status, model, _now()),
            )
        return self.get_message(msg_id)  # type: ignore[return-value]

    def get_message(self, message_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
        return dict(row) if row else None

    def list_messages(self, conversation_id: str) -> list[dict[str, Any]]:
        # rowid breaks ties: a user message and its assistant placeholder are
        # created in the same millisecond, and insertion order must win.
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM messages WHERE conversation_id = ? ORDER BY created_at, rowid",
                (conversation_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def update_message(self, message_id: str, **fields: Any) -> None:
        unknown = set(fields) - _UPDATABLE
        if unknown:
            raise ValueError(f"cannot update columns: {sorted(unknown)}")
        if not fields:
            return
        if "status" in fields and fields["status"] not in STATUSES:
            raise ValueError(f"invalid status: {fields['status']!r}")
        assignments = ", ".join(f"{col} = :{col}" for col in fields)  # whitelisted names only
        with self.connect() as conn:
            conn.execute(
                f"UPDATE messages SET {assignments} WHERE id = :_id",
                {**fields, "_id": message_id},
            )

    def has_streaming_message(self, conversation_id: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM messages WHERE conversation_id = ? AND status = 'streaming' LIMIT 1",
                (conversation_id,),
            ).fetchone()
        return row is not None

    def recover_interrupted(self) -> int:
        """Crash recovery. Run once at startup.

        If the process was killed mid-stream (kill -9, power loss, OOM), nothing
        got to run its error handler, so rows are stuck at status='streaming'
        forever. At startup no stream can possibly be live, so any such row is
        an orphan: mark it 'error'. The partial text flushed to `content`
        before the crash is kept.
        """
        with self.connect() as conn:
            cur = conn.execute(
                "UPDATE messages SET status = 'error', stop_reason = 'interrupted' "
                "WHERE status = 'streaming'"
            )
            return cur.rowcount

    def conversation_totals(self, conversation_id: str) -> dict[str, Any]:
        """Per-conversation accounting: SUMs of what was actually recorded on the rows.

        Rows with NULL usage/cost (cancelled, errored, unpriced) simply don't
        contribute; nothing is recomputed or estimated here.
        """
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(cost_usd), 0.0)            AS total_cost_usd, "
                "       COALESCE(SUM(total_tokens), 0)          AS total_tokens, "
                "       COALESCE(SUM(input_tokens), 0)          AS total_input_tokens, "
                "       COALESCE(SUM(cached_input_tokens), 0)   AS total_cached_input_tokens, "
                "       COALESCE(SUM(output_tokens), 0)         AS total_output_tokens, "
                # assistant replies that finished with usage but have no price
                "       COALESCE(SUM(CASE WHEN role = 'assistant' AND total_tokens IS NOT NULL "
                "                         AND cost_usd IS NULL THEN 1 ELSE 0 END), 0) AS unpriced_messages "
                "FROM messages WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
        return dict(row)

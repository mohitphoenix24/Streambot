"""PostgreSQL persistence: conversations and messages.

The schema lives in migrations/*.sql (see app/migrations.py); this module is every SQL
statement the app runs. Design notes (the "why" behind the schema):

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
  answer from one that died halfway.

* NULL means "unknown", never 0. cost_usd NULL = "no price configured", which is
  honest; 0.0 would be a lie.

Connections come from a POOL (opening a Postgres connection costs milliseconds, so
connections are reused). Every method borrows one, does one small thing, and returns it;
the borrow commits on success and rolls back on any error. The calls are SYNCHRONOUS on
purpose: they take well under a millisecond on a local database, and keeping them
synchronous preserves a property the streaming code relies on, namely that the final
`finally` block that saves a reply contains no `await`, so a cancelled task cannot be
interrupted halfway through saving. (The cost is that a slow database stalls the event
loop; the upgrade path is psycopg's async pool plus a shielded save.)
"""
from __future__ import annotations

import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

STATUSES = ("streaming", "complete", "cancelled", "error")
ROLES = ("user", "assistant")

# Everything a message row carries, in one place (so `seq`, the ordering column, never leaks out).
_MESSAGE_COLUMNS = (
    "id, conversation_id, role, content, status, input_tokens, cached_input_tokens, output_tokens, "
    "reasoning_tokens, total_tokens, cost_usd, stop_reason, model, served_model, ttft_ms, latency_ms, "
    "sources, web_searches, created_at"
)

# Columns update_message() is allowed to touch. Column names cannot be bound as
# SQL parameters, so we whitelist them rather than interpolating caller input.
_UPDATABLE = {
    "content", "status", "input_tokens", "cached_input_tokens", "output_tokens",
    "reasoning_tokens", "total_tokens", "cost_usd", "stop_reason", "model",
    "served_model", "ttft_ms", "latency_ms", "sources", "web_searches",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def _new_id() -> str:
    return uuid.uuid4().hex


def vector_literal(vector: list[float]) -> str:
    """A Python list as pgvector's text form, '[0.1,0.2,...]' (cast with ::vector in SQL)."""
    return "[" + ",".join(f"{x:.7g}" for x in vector) + "]"


def _clean(row: dict[str, Any] | None) -> dict[str, Any] | None:
    """A row as the rest of the app sees it: timestamps as ISO-8601 strings (the API's format)."""
    if row is None:
        return None
    return {k: (_iso(v) if isinstance(v, datetime) else v) for k, v in row.items()}


class Database:
    def __init__(self, url: str, pool_max: int = 10) -> None:
        self.url = url
        self._pool = ConnectionPool(
            url, min_size=1, max_size=pool_max, open=False, name="streambot",
            kwargs={"row_factory": dict_row},
        )
        self._open_lock = threading.Lock()
        self._opened = False

    def _ensure_open(self) -> None:
        if self._opened:
            return
        with self._open_lock:
            if not self._opened:
                self._pool.open(wait=True, timeout=15)  # fails with a clear error if Postgres is not reachable
                self._opened = True

    def close(self) -> None:
        if self._opened:
            self._pool.close()
            self._opened = False

    @contextmanager
    def connect(self) -> Iterator[psycopg.Connection]:
        """Borrow a pooled connection. Commits on success, rolls back on any error."""
        self._ensure_open()
        with self._pool.connection() as conn:
            yield conn

    def init_schema(self) -> None:
        """Bring the database up to the latest schema (applies pending migrations)."""
        from app.migrations import migrate

        migrate(self)

    # ── users ────────────────────────────────────────────────────────────
    def create_user(self, email: str, password_hash: str) -> dict[str, Any]:
        """Create an account. Raises psycopg.errors.UniqueViolation if the email is taken."""
        user = {"id": _new_id(), "email": email.strip().lower(), "password_hash": password_hash, "created_at": _now()}
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO users (id, email, password_hash, created_at) "
                "VALUES (%(id)s, %(email)s, %(password_hash)s, %(created_at)s)",
                user,
            )
        return _clean(user)  # type: ignore[return-value]

    def get_user(self, user_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT id, email, password_hash, created_at FROM users WHERE id = %s", (user_id,)
            ).fetchone()
        return _clean(row)

    def set_password_hash(self, user_id: str, password_hash: str) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE users SET password_hash = %s WHERE id = %s", (password_hash, user_id))

    def get_user_by_email(self, email: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT id, email, password_hash, created_at FROM users WHERE email = %s", (email.strip().lower(),)
            ).fetchone()
        return _clean(row)

    # ── conversations ────────────────────────────────────────────────────
    # Every conversation belongs to one user, and every query that starts from a request takes
    # that user's id. Asking for someone else's conversation finds NOTHING (the same answer as
    # a conversation that does not exist), so ids can never be used to probe other accounts.
    def create_conversation(self, user_id: str, title: str = "New chat") -> dict[str, Any]:
        now = _now()
        conv = {"id": _new_id(), "user_id": user_id, "title": title, "created_at": now, "updated_at": now}
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO conversations (id, user_id, title, created_at, updated_at) "
                "VALUES (%(id)s, %(user_id)s, %(title)s, %(created_at)s, %(updated_at)s)",
                conv,
            )
        conv.pop("user_id")
        return _clean(conv)  # type: ignore[return-value]

    def list_conversations(self, user_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id, title, created_at, updated_at FROM conversations WHERE user_id = %s "
                "ORDER BY updated_at DESC, seq DESC",
                (user_id,),
            ).fetchall()
        return [_clean(r) for r in rows]  # type: ignore[misc]

    def get_conversation(self, conversation_id: str, user_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT id, title, created_at, updated_at FROM conversations WHERE id = %s AND user_id = %s",
                (conversation_id, user_id),
            ).fetchone()
        return _clean(row)

    def conversation_owner(self, conversation_id: str) -> str | None:
        """Who owns this conversation, for server-side work that starts from a conversation
        id the server already trusts (background summaries, title calls). Never for requests."""
        with self.connect() as conn:
            row = conn.execute("SELECT user_id FROM conversations WHERE id = %s", (conversation_id,)).fetchone()
        return row["user_id"] if row else None

    def delete_conversation(self, conversation_id: str, user_id: str) -> bool:
        """Delete a conversation; its messages go with it (ON DELETE CASCADE).
        Returns False if there was no such conversation (or it is not this user's).

        The content is gone, but what it COST is not: each reply's usage numbers are copied
        to `archived_replies` in the same transaction first, so spend history stays true.
        """
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO archived_replies "
                "(id, user_id, conversation_id, model, served_model, input_tokens, cached_input_tokens, "
                " output_tokens, reasoning_tokens, total_tokens, cost_usd, web_searches, created_at, archived_at) "
                "SELECT m.id, c.user_id, m.conversation_id, m.model, m.served_model, m.input_tokens, "
                "       m.cached_input_tokens, m.output_tokens, m.reasoning_tokens, m.total_tokens, m.cost_usd, "
                "       m.web_searches, m.created_at, %s "
                "FROM messages m JOIN conversations c ON c.id = m.conversation_id "
                "WHERE m.conversation_id = %s AND c.user_id = %s AND m.role = 'assistant' "
                "  AND (m.total_tokens IS NOT NULL OR m.cost_usd IS NOT NULL) "
                "ON CONFLICT (id) DO NOTHING",
                (_now(), conversation_id, user_id),
            )
            cur = conn.execute("DELETE FROM conversations WHERE id = %s AND user_id = %s", (conversation_id, user_id))
        return cur.rowcount > 0

    def get_summary(self, conversation_id: str) -> tuple[str | None, str | None]:
        """(summary text, id of the last message it covers); (None, None) if there is none."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT summary, summary_through_id FROM conversations WHERE id = %s", (conversation_id,)
            ).fetchone()
        return (row["summary"], row["summary_through_id"]) if row and row["summary"] else (None, None)

    def set_summary(self, conversation_id: str, summary: str, through_message_id: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE conversations SET summary = %s, summary_through_id = %s WHERE id = %s",
                (summary, through_message_id, conversation_id),
            )

    def add_llm_call(self, user_id: str, conversation_id: str | None, purpose: str, model: str, **fields: Any) -> str:
        """Record a non-reply model call. `fields` are the usage columns and cost_usd."""
        allowed = {"served_model", "input_tokens", "cached_input_tokens", "output_tokens",
                   "reasoning_tokens", "total_tokens", "cost_usd"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"cannot record columns: {sorted(unknown)}")
        call_id = _new_id()
        columns = {"id": call_id, "user_id": user_id, "conversation_id": conversation_id, "purpose": purpose,
                   "model": model, "created_at": _now(), **fields}
        with self.connect() as conn:
            conn.execute(
                f"INSERT INTO llm_calls ({', '.join(columns)}) VALUES ({', '.join('%(' + c + ')s' for c in columns)})",
                columns,  # column names come from the whitelist above, values are bound
            )
        return call_id

    def list_llm_calls(self, conversation_id: str | None = None) -> list[dict[str, Any]]:
        with self.connect() as conn:
            if conversation_id is None:
                rows = conn.execute("SELECT * FROM llm_calls ORDER BY created_at, id").fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM llm_calls WHERE conversation_id = %s ORDER BY created_at, id",
                    (conversation_id,),
                ).fetchall()
        return [_clean(r) for r in rows]  # type: ignore[misc]

    def usage_rows(self, user_id: str, since: datetime) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Everything this user spent (or may have spent) since `since` (an aware datetime):
        assistant replies that reported usage, and title/summary calls.
        Replies carry the conversation title (None if the chat was deleted since)."""
        with self.connect() as conn:
            replies = conn.execute(
                "SELECT m.id, m.conversation_id, c.title AS conversation_title, m.model, m.served_model, "
                "       m.input_tokens, m.cached_input_tokens, m.output_tokens, m.reasoning_tokens, "
                "       m.total_tokens, m.cost_usd, m.web_searches, m.created_at "
                "FROM messages m JOIN conversations c ON c.id = m.conversation_id "
                "WHERE c.user_id = %s AND m.role = 'assistant' AND m.created_at >= %s "
                "  AND (m.total_tokens IS NOT NULL OR m.cost_usd IS NOT NULL) "
                "ORDER BY m.created_at",
                (user_id, since),
            ).fetchall()
            calls = conn.execute(
                "SELECT id, conversation_id, purpose, model, served_model, input_tokens, cached_input_tokens, "
                "       output_tokens, reasoning_tokens, total_tokens, cost_usd, created_at "
                "FROM llm_calls WHERE user_id = %s AND created_at >= %s ORDER BY created_at",
                (user_id, since),
            ).fetchall()
            archived = conn.execute(
                "SELECT id, conversation_id, NULL AS conversation_title, model, served_model, input_tokens, "
                "       cached_input_tokens, output_tokens, reasoning_tokens, total_tokens, cost_usd, "
                "       web_searches, created_at "
                "FROM archived_replies WHERE user_id = %s AND created_at >= %s ORDER BY created_at",
                (user_id, since),
            ).fetchall()
        # Replies of deleted chats count like any other reply; they just have no chat to link to.
        return [_clean(r) for r in replies] + [_clean(r) for r in archived], [_clean(r) for r in calls]  # type: ignore[misc]

    def set_title(self, conversation_id: str, title: str) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE conversations SET title = %s WHERE id = %s", (title, conversation_id))

    def touch_conversation(self, conversation_id: str) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE conversations SET updated_at = %s WHERE id = %s", (_now(), conversation_id))

    # ── documents and chunks (attachments / RAG) ─────────────────────────
    def add_document(self, user_id: str, conversation_id: str, filename: str, kind: str, size_bytes: int) -> dict[str, Any]:
        """Register an upload. It starts as 'processing'; a background task makes it 'ready' or 'failed'."""
        doc = {"id": _new_id(), "user_id": user_id, "conversation_id": conversation_id, "filename": filename,
               "kind": kind, "size_bytes": size_bytes, "created_at": _now()}
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO documents (id, user_id, conversation_id, filename, kind, size_bytes, status, created_at) "
                "VALUES (%(id)s, %(user_id)s, %(conversation_id)s, %(filename)s, %(kind)s, %(size_bytes)s, 'processing', %(created_at)s)",
                doc,
            )
        return self.get_document(doc["id"], user_id)  # type: ignore[return-value]

    _DOCUMENT_COLUMNS = "id, conversation_id, filename, kind, size_bytes, status, error, page_count, chunk_count, created_at"

    def get_document(self, document_id: str, user_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                f"SELECT {self._DOCUMENT_COLUMNS} FROM documents WHERE id = %s AND user_id = %s", (document_id, user_id)
            ).fetchone()
        return _clean(row)

    def list_documents(self, conversation_id: str, user_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT {self._DOCUMENT_COLUMNS} FROM documents WHERE conversation_id = %s AND user_id = %s ORDER BY created_at, id",
                (conversation_id, user_id),
            ).fetchall()
        return [_clean(r) for r in rows]  # type: ignore[misc]

    def delete_document(self, document_id: str, user_id: str) -> bool:
        with self.connect() as conn:  # its chunks go with it (ON DELETE CASCADE)
            cur = conn.execute("DELETE FROM documents WHERE id = %s AND user_id = %s", (document_id, user_id))
        return cur.rowcount > 0

    def has_ready_documents(self, conversation_id: str, user_id: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 AS found FROM documents WHERE conversation_id = %s AND user_id = %s AND status = 'ready' LIMIT 1",
                (conversation_id, user_id),
            ).fetchone()
        return row is not None

    def mark_document_failed(self, document_id: str, error: str) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE documents SET status = 'failed', error = %s WHERE id = %s", (error, document_id))

    def complete_document(self, document_id: str, page_count: int | None, chunks: list[dict[str, Any]]) -> None:
        """Store a document's chunks (with their vectors) and mark it ready, in ONE transaction:
        a document is never 'ready' with only some of its chunks."""
        with self.connect() as conn:
            conn.execute("DELETE FROM chunks WHERE document_id = %s", (document_id,))
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO chunks (document_id, user_id, conversation_id, ordinal, page, heading, content, tokens, embedding) "
                    "VALUES (%(document_id)s, %(user_id)s, %(conversation_id)s, %(ordinal)s, %(page)s, %(heading)s, "
                    "        %(content)s, %(tokens)s, %(embedding)s::vector)",
                    [{**c, "document_id": document_id, "embedding": vector_literal(c["embedding"])} for c in chunks],
                )
            conn.execute(
                "UPDATE documents SET status = 'ready', error = NULL, page_count = %s, chunk_count = %s WHERE id = %s",
                (page_count, len(chunks), document_id),
            )

    def recover_interrupted_documents(self) -> int:
        """A server killed mid-upload leaves documents 'processing' for ever. At startup none can be live."""
        with self.connect() as conn:
            cur = conn.execute(
                "UPDATE documents SET status = 'failed', error = 'Processing was interrupted. Please upload it again.' "
                "WHERE status = 'processing'"
            )
            return cur.rowcount

    def search_chunks(
        self, user_id: str, conversation_id: str, query_vector: list[float], query_text: str,
        candidates: int = 20, limit: int = 6,
    ) -> list[dict[str, Any]]:
        """HYBRID search over one user's one chat: meaning (vector) + keywords (full text), merged.

        Each method ranks its own top `candidates`; the two rankings are combined with
        Reciprocal Rank Fusion (RRF): a chunk scores 1/(60 + rank) from each list it appears in, so
        a passage that BOTH methods like beats one that only one likes. It needs no tuning, because
        it uses ranks, not the (incomparable) raw scores.

        The keyword leg matches a passage that has ANY of the question's words (the `&` that
        websearch_to_tsquery puts between terms becomes `|`) and ranks by how many it has and how
        often. With AND semantics a natural-language question ("Can I get a refund?") would match
        nothing, because no passage contains every word of it, including "get".
        """
        params = {"u": user_id, "c": conversation_id, "q": vector_literal(query_vector), "t": query_text,
                  "k": candidates, "n": limit}
        with self.connect() as conn:
            # The filter (one chat) is very selective, so let the HNSW index keep scanning until it
            # has enough matching rows instead of giving up early with too few results.
            conn.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")
            rows = conn.execute(
                """
                WITH vec AS (
                    SELECT id, ROW_NUMBER() OVER (ORDER BY embedding <=> %(q)s::vector) AS rank
                    FROM chunks WHERE user_id = %(u)s AND conversation_id = %(c)s
                    ORDER BY embedding <=> %(q)s::vector LIMIT %(k)s
                ), parsed AS (
                    SELECT replace(websearch_to_tsquery('english', %(t)s)::text, '&', '|')::tsquery AS query
                ), kw AS (
                    SELECT id, ROW_NUMBER() OVER (ORDER BY ts_rank_cd(tsv, parsed.query) DESC) AS rank
                    FROM chunks, parsed
                    WHERE user_id = %(u)s AND conversation_id = %(c)s AND tsv @@ parsed.query
                    ORDER BY ts_rank_cd(tsv, parsed.query) DESC LIMIT %(k)s
                )
                SELECT c.id, c.document_id, d.filename, c.page, c.heading, c.content,
                       vec.rank AS vector_rank, kw.rank AS keyword_rank,
                       (COALESCE(1.0 / (60 + vec.rank), 0) + COALESCE(1.0 / (60 + kw.rank), 0))::float8 AS score
                FROM (SELECT id FROM vec UNION SELECT id FROM kw) hits
                JOIN chunks c ON c.id = hits.id
                JOIN documents d ON d.id = c.document_id
                LEFT JOIN vec ON vec.id = c.id
                LEFT JOIN kw ON kw.id = c.id
                ORDER BY score DESC, c.id
                LIMIT %(n)s
                """,
                params,
            ).fetchall()
        return [dict(r) for r in rows]

    # ── messages ─────────────────────────────────────────────────────────
    def add_message(
        self,
        conversation_id: str,
        role: str,
        content: str = "",
        status: str = "complete",
        model: str | None = None,
    ) -> dict[str, Any]:
        """Insert a message. Raises psycopg.errors.IntegrityError for a bad conversation_id,
        role or status (foreign key / CHECK constraints)."""
        with self.connect() as conn:
            row = conn.execute(
                f"INSERT INTO messages (id, conversation_id, role, content, status, model, created_at) "
                f"VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING {_MESSAGE_COLUMNS}",
                (_new_id(), conversation_id, role, content, status, model, _now()),
            ).fetchone()
        return _clean(row)  # type: ignore[return-value]

    def get_message(self, message_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                f"SELECT {_MESSAGE_COLUMNS} FROM messages WHERE id = %s", (message_id,)
            ).fetchone()
        return _clean(row)

    def list_messages(self, conversation_id: str) -> list[dict[str, Any]]:
        # seq is insertion order: a user message and its assistant placeholder are created in
        # the same millisecond, so the timestamp alone cannot order them.
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT {_MESSAGE_COLUMNS} FROM messages WHERE conversation_id = %s ORDER BY seq",
                (conversation_id,),
            ).fetchall()
        return [_clean(r) for r in rows]  # type: ignore[misc]

    def update_message(self, message_id: str, **fields: Any) -> None:
        unknown = set(fields) - _UPDATABLE
        if unknown:
            raise ValueError(f"cannot update columns: {sorted(unknown)}")
        if not fields:
            return
        if "status" in fields and fields["status"] not in STATUSES:
            raise ValueError(f"invalid status: {fields['status']!r}")
        assignments = ", ".join(f"{col} = %({col})s" for col in fields)  # whitelisted names only
        with self.connect() as conn:
            conn.execute(
                f"UPDATE messages SET {assignments} WHERE id = %(_id)s",
                {**fields, "_id": message_id},
            )

    def has_streaming_message(self, conversation_id: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 AS found FROM messages WHERE conversation_id = %s AND status = 'streaming' LIMIT 1",
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
                "FROM messages WHERE conversation_id = %s",
                (conversation_id,),
            ).fetchone()
        return {k: (int(v) if k != "total_cost_usd" else float(v)) for k, v in row.items()}  # type: ignore[union-attr]

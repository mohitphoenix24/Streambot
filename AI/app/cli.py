"""Admin commands. Run from the AI/ directory:

    python -m app.cli create-user you@example.com          # asks for a password (use this when SIGNUP_ENABLED=false)
    python -m app.cli claim-legacy you@example.com         # give pre-login data to a real account
    python -m app.cli import-sqlite data/chat.db you@example.com   # copy chats from the old SQLite database
"""
from __future__ import annotations

import argparse
import getpass
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.auth.passwords import hash_password
from app.config import Settings
from app.db import Database

# Table -> the columns that exist in the PostgreSQL schema (and so can be imported).
_IMPORT_TABLES = {
    "conversations": ("id", "title", "created_at", "updated_at", "summary", "summary_through_id"),
    "messages": (
        "id", "conversation_id", "role", "content", "status", "input_tokens", "cached_input_tokens",
        "output_tokens", "reasoning_tokens", "total_tokens", "cost_usd", "stop_reason", "model",
        "served_model", "ttft_ms", "latency_ms", "sources", "web_searches", "created_at",
    ),
    "llm_calls": (
        "id", "conversation_id", "purpose", "model", "served_model", "input_tokens", "cached_input_tokens",
        "output_tokens", "reasoning_tokens", "total_tokens", "cost_usd", "created_at",
    ),
    "archived_replies": (
        "id", "conversation_id", "model", "served_model", "input_tokens", "cached_input_tokens",
        "output_tokens", "reasoning_tokens", "total_tokens", "cost_usd", "web_searches", "created_at", "archived_at",
    ),
}
_TIMESTAMPS = {"created_at", "updated_at", "archived_at"}
_OWNED = {"conversations", "llm_calls", "archived_replies"}  # rows that carry a user_id


def _timestamp(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def import_sqlite(db: Database, sqlite_path: Path, user_id: str) -> dict[str, int]:
    """Copy an old Streambot SQLite database into PostgreSQL, owned by `user_id`.

    Ids and timestamps are kept, so the chats look exactly as they did, and a second run
    skips everything that is already there (ON CONFLICT DO NOTHING). Returns rows read per table.
    """
    source = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    counts: dict[str, int] = {}
    try:
        with db.connect() as conn:
            for table, wanted in _IMPORT_TABLES.items():
                present = {r["name"] for r in source.execute(f"PRAGMA table_info({table})")}
                if not present:
                    counts[table] = 0
                    continue
                columns = [c for c in wanted if c in present]  # older files lack the newer columns
                rows = source.execute(f"SELECT {', '.join(columns)} FROM {table} ORDER BY rowid").fetchall()
                target = list(columns) + (["user_id"] if table in _OWNED else [])
                sql = (
                    f"INSERT INTO {table} ({', '.join(target)}) "
                    f"VALUES ({', '.join('%s' for _ in target)}) ON CONFLICT (id) DO NOTHING"
                )
                for row in rows:
                    values = [_timestamp(row[c]) if c in _TIMESTAMPS and row[c] is not None else row[c] for c in columns]
                    conn.execute(sql, values + ([user_id] if table in _OWNED else []))
                counts[table] = len(rows)
    finally:
        source.close()
    return counts


def claim_legacy(db: Database, user_id: str) -> dict[str, int]:
    """Move everything the migration parked under the 'legacy' placeholder to a real account."""
    moved: dict[str, int] = {}
    with db.connect() as conn:
        for table in sorted(_OWNED):
            moved[table] = conn.execute(f"UPDATE {table} SET user_id = %s WHERE user_id = 'legacy'", (user_id,)).rowcount
        conn.execute("DELETE FROM users WHERE id = 'legacy'")
    return moved


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("create-user", help="create an account")
    p.add_argument("email")
    p = sub.add_parser("claim-legacy", help="give pre-login data to an account")
    p.add_argument("email")
    p = sub.add_parser("import-sqlite", help="import chats from the old SQLite database")
    p.add_argument("path", type=Path)
    p.add_argument("email")
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    db = Database(settings.database_url)
    db.init_schema()
    try:
        if args.command == "create-user":
            password = getpass.getpass("Password: ")
            if len(password) < settings.password_min_length:
                print(f"Password must be at least {settings.password_min_length} characters.", file=sys.stderr)
                return 1
            user = db.create_user(args.email, hash_password(password))
            print(f"created {user['email']}")
            return 0

        user = db.get_user_by_email(args.email)
        if user is None:
            print(f"No account for {args.email}. Create it first: python -m app.cli create-user {args.email}", file=sys.stderr)
            return 1
        if args.command == "claim-legacy":
            print("moved:", claim_legacy(db, user["id"]))
        else:
            print("imported:", import_sqlite(db, args.path, user["id"]))
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())

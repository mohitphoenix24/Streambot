"""A small, explicit schema-migration runner.

A database schema changes over time (new columns, new tables), and every copy of the
database (your laptop, a teammate's, production) must end up identical. The standard
answer is MIGRATIONS: numbered SQL files, applied in order, each exactly once.

    AI/migrations/001_initial.sql      applied first
    AI/migrations/002_users.sql        applied second ...

The table `schema_migrations` records which versions a database already has, so
running this on every startup is safe: it applies only what is missing. Rules:

  * a migration is NEVER edited after it has shipped; a change is a new file;
  * all pending migrations run in ONE transaction (Postgres can roll back DDL), so a
    failure leaves the database exactly as it was;
  * an advisory lock serialises concurrent startups (two server processes booting at
    the same moment must not both apply the same migration).

This is deliberately ~40 lines instead of a framework like Alembic: the idea is the same,
and nothing is hidden.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.db import Database

log = logging.getLogger("chat.migrations")

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"
_NAME = re.compile(r"^(\d{3,})_[a-z0-9_]+\.sql$")
_LOCK_ID = 7_240_001  # any constant; identifies "the Streambot migration lock"


def migration_files(directory: Path = MIGRATIONS_DIR) -> list[tuple[str, Path]]:
    """(version, path) for every migration, in the order they must be applied."""
    found = []
    for path in sorted(directory.glob("*.sql")):
        if not _NAME.match(path.name):
            raise ValueError(f"migration file names look like 001_name.sql, got {path.name!r}")
        found.append((path.stem, path))
    versions = [v for v, _ in found]
    if len(set(v.split("_")[0] for v in versions)) != len(versions):
        raise ValueError("two migrations share a number")
    return found


def migrate(db: "Database", directory: Path = MIGRATIONS_DIR) -> list[str]:
    """Apply every migration the database does not have yet. Returns the versions applied."""
    applied_now: list[str] = []
    with db.connect() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK_ID,))  # released at commit
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        done = {r["version"] for r in conn.execute("SELECT version FROM schema_migrations").fetchall()}
        for version, path in migration_files(directory):
            if version in done:
                continue
            log.info("applying_migration version=%s", version)
            conn.execute(path.read_text(encoding="utf-8"))
            conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (version,))
            applied_now.append(version)
    return applied_now

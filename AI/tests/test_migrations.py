"""The migration runner: numbered SQL files, applied in order, each exactly once."""
import psycopg
import pytest

from app.db import Database
from app.migrations import MIGRATIONS_DIR, migrate, migration_files


def table_names(db):
    with db.connect() as conn:
        rows = conn.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'").fetchall()
    return {r["tablename"] for r in rows}


def test_the_shipped_migrations_are_numbered_and_ordered():
    versions = [v for v, _ in migration_files()]
    assert versions == sorted(versions) and versions[0] == "001_initial"
    assert len({v.split("_")[0] for v in versions}) == len(versions)  # no two share a number


def test_a_migrated_database_has_the_tables_and_records_every_version(db):
    assert {"conversations", "messages", "llm_calls", "archived_replies", "schema_migrations"} <= table_names(db)
    with db.connect() as conn:
        recorded = [r["version"] for r in conn.execute("SELECT version FROM schema_migrations ORDER BY version")]
    assert recorded == [v for v, _ in migration_files()]


def test_running_migrations_again_applies_nothing(db):
    assert migrate(db) == []  # safe to run on every startup


def test_only_missing_migrations_are_applied(db, tmp_path):
    (tmp_path / "001_a.sql").write_text("CREATE TABLE a (id INT);")
    (tmp_path / "002_b.sql").write_text("CREATE TABLE b (id INT);")
    assert migrate(db, tmp_path) == ["001_a", "002_b"]
    (tmp_path / "003_c.sql").write_text("CREATE TABLE c (id INT);")
    assert migrate(db, tmp_path) == ["003_c"]  # the first two were already recorded
    assert {"a", "b", "c"} <= table_names(db)


def test_a_failing_migration_leaves_the_database_untouched(db, tmp_path):
    (tmp_path / "001_ok.sql").write_text("CREATE TABLE fine (id INT);")
    (tmp_path / "002_broken.sql").write_text("CREATE TABLE half (id INT); THIS IS NOT SQL;")
    with pytest.raises(psycopg.errors.SyntaxError):
        migrate(db, tmp_path)
    # one transaction: not even the good migration before it was kept, and nothing is recorded
    assert "fine" not in table_names(db) and "half" not in table_names(db)
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM schema_migrations WHERE version LIKE '00%_ok'").fetchone()["n"] == 0


def test_bad_file_names_are_rejected(tmp_path):
    (tmp_path / "setup.sql").write_text("SELECT 1;")
    with pytest.raises(ValueError):
        migration_files(tmp_path)


def test_two_migrations_with_the_same_number_are_rejected(tmp_path):
    (tmp_path / "001_a.sql").write_text("SELECT 1;")
    (tmp_path / "001_b.sql").write_text("SELECT 1;")
    with pytest.raises(ValueError):
        migration_files(tmp_path)


def test_a_fresh_empty_database_can_be_migrated_from_scratch(database_url):
    import uuid

    from tests.conftest import ADMIN_URL, _url_for

    name = f"t_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')  # empty, no template
        try:
            fresh = Database(_url_for(name))
            assert migrate(fresh) == [v for v, _ in migration_files()]
            fresh.close()
        finally:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')

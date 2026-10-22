"""The admin commands: importing the old SQLite database, and claiming pre-login data."""
import sqlite3

from app.cli import claim_legacy, import_sqlite


def old_database(path):
    """A small file shaped like the SQLite database Streambot used before PostgreSQL."""
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE conversations(id TEXT PRIMARY KEY, title TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                                   summary TEXT, summary_through_id TEXT);
        CREATE TABLE messages(id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL DEFAULT '',
                              status TEXT NOT NULL, input_tokens INTEGER, output_tokens INTEGER, total_tokens INTEGER, cost_usd REAL,
                              stop_reason TEXT, model TEXT, ttft_ms REAL, latency_ms REAL, history_truncated INTEGER, created_at TEXT NOT NULL);
        CREATE TABLE llm_calls(id TEXT PRIMARY KEY, conversation_id TEXT, purpose TEXT NOT NULL, model TEXT NOT NULL, served_model TEXT,
                               input_tokens INTEGER, cached_input_tokens INTEGER, output_tokens INTEGER, reasoning_tokens INTEGER,
                               total_tokens INTEGER, cost_usd REAL, created_at TEXT NOT NULL);
        INSERT INTO conversations VALUES ('c1', 'Old chat', '2026-10-01T10:00:00.000+00:00', '2026-10-01T10:05:00.000+00:00', NULL, NULL);
        INSERT INTO messages(id, conversation_id, role, content, status, input_tokens, output_tokens, total_tokens, cost_usd, history_truncated, created_at)
            VALUES ('m1', 'c1', 'user', 'hello', 'complete', NULL, NULL, NULL, NULL, 0, '2026-10-01T10:00:00.000+00:00');
        INSERT INTO messages(id, conversation_id, role, content, status, input_tokens, output_tokens, total_tokens, cost_usd, history_truncated, created_at)
            VALUES ('m2', 'c1', 'assistant', 'hi!', 'complete', 10, 5, 15, 0.001, 1, '2026-10-01T10:00:00.000+00:00');
        INSERT INTO llm_calls VALUES ('l1', 'c1', 'title', 'm', NULL, 5, 0, 2, 0, 7, 0.0001, '2026-10-01T10:00:05.000+00:00');
    """)
    conn.commit()
    conn.close()


def test_the_old_sqlite_database_is_imported_under_the_chosen_user(db, uid, tmp_path):
    old_database(tmp_path / "chat.db")

    counts = import_sqlite(db, tmp_path / "chat.db", uid)

    assert counts == {"conversations": 1, "messages": 2, "llm_calls": 1, "archived_replies": 0}
    conv = db.list_conversations(uid)
    assert [(c["id"], c["title"]) for c in conv] == [("c1", "Old chat")]
    assert conv[0]["created_at"] == "2026-10-01T10:00:00.000+00:00"  # timestamps are kept
    messages = db.list_messages("c1")
    assert [(m["id"], m["content"]) for m in messages] == [("m1", "hello"), ("m2", "hi!")]  # order kept
    assert (messages[1]["total_tokens"], messages[1]["cost_usd"]) == (15, 0.001)
    assert db.list_llm_calls("c1")[0]["user_id"] == uid


def test_importing_twice_does_not_duplicate_anything(db, uid, tmp_path):
    old_database(tmp_path / "chat.db")
    import_sqlite(db, tmp_path / "chat.db", uid)
    import_sqlite(db, tmp_path / "chat.db", uid)
    assert len(db.list_conversations(uid)) == 1 and len(db.list_messages("c1")) == 2


def test_the_import_never_touches_the_source_file(db, uid, tmp_path):
    path = tmp_path / "chat.db"
    old_database(path)
    before = path.read_bytes()
    import_sqlite(db, path, uid)
    assert path.read_bytes() == before  # opened read-only


def test_claiming_legacy_data_moves_it_to_a_real_account(db, uid):
    with db.connect() as conn:
        conn.execute("INSERT INTO users (id, email, password_hash, created_at) VALUES ('legacy', 'legacy@streambot.invalid', '!', now())")
        conn.execute("INSERT INTO conversations (id, user_id, title, created_at, updated_at) VALUES ('old', 'legacy', 'parked', now(), now())")

    moved = claim_legacy(db, uid)

    assert moved["conversations"] == 1
    assert [c["id"] for c in db.list_conversations(uid)] == ["old"]
    assert db.get_user("legacy") is None  # the placeholder is gone

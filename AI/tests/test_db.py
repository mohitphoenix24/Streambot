"""Database tests. Real SQLite, real file (pytest's tmp_path) - no mocks needed,
because SQLite is fast and deterministic."""
import sqlite3

import pytest

from app.db import Database


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "test.db")
    database.init_schema()
    return database


def test_conversation_creation(db):
    conv = db.create_conversation("Hello")
    assert conv["title"] == "Hello"
    assert db.get_conversation(conv["id"])["id"] == conv["id"]
    assert [c["id"] for c in db.list_conversations()] == [conv["id"]]


def test_delete_conversation_removes_its_messages(db):
    cid = db.create_conversation()["id"]
    other = db.create_conversation()["id"]
    mid = db.add_message(cid, "user", "hi")["id"]
    db.add_message(other, "user", "keep me")

    assert db.delete_conversation(cid) is True
    assert db.get_conversation(cid) is None
    assert db.get_message(mid) is None
    assert len(db.list_messages(other)) == 1
    assert db.delete_conversation(cid) is False


def test_get_missing_conversation_returns_none(db):
    assert db.get_conversation("nope") is None


def test_conversations_listed_most_recently_active_first(db):
    first = db.create_conversation("first")
    second = db.create_conversation("second")
    db.touch_conversation(first["id"])  # activity on the older one moves it to the top
    assert [c["id"] for c in db.list_conversations()] == [first["id"], second["id"]]


def test_message_creation_and_defaults(db):
    conv = db.create_conversation()
    msg = db.add_message(conv["id"], "assistant", status="streaming")
    assert msg["content"] == ""
    assert msg["status"] == "streaming"
    # unknown is NULL, never 0
    assert msg["cost_usd"] is None and msg["total_tokens"] is None


def test_message_update(db):
    conv = db.create_conversation()
    msg = db.add_message(conv["id"], "assistant", status="streaming")
    db.update_message(
        msg["id"], content="partial", status="error",
        input_tokens=10, output_tokens=5, total_tokens=15, cost_usd=0.001,
    )
    row = db.get_message(msg["id"])
    assert (row["content"], row["status"], row["total_tokens"], row["cost_usd"]) == (
        "partial", "error", 15, 0.001,
    )


def test_messages_come_back_in_insertion_order(db):
    conv = db.create_conversation()
    # Created in the same millisecond on purpose: rowid must break the tie.
    ids = [db.add_message(conv["id"], r, f"m{i}")["id"]
           for i, r in enumerate(["user", "assistant", "user", "assistant"])]
    assert [m["id"] for m in db.list_messages(conv["id"])] == ids


def test_update_rejects_unknown_column_and_bad_status(db):
    conv = db.create_conversation()
    msg = db.add_message(conv["id"], "user", "hi")
    with pytest.raises(ValueError):
        db.update_message(msg["id"], id="hijack")  # not whitelisted
    with pytest.raises(ValueError):
        db.update_message(msg["id"], status="finished")  # not a valid status


def test_foreign_key_is_enforced(db):
    with pytest.raises(sqlite3.IntegrityError):
        db.add_message("no-such-conversation", "user", "orphan")


def test_check_constraints_reject_bad_role(db):
    conv = db.create_conversation()
    with pytest.raises(sqlite3.IntegrityError):
        db.add_message(conv["id"], "system", "nope")


def test_recover_interrupted_marks_orphaned_streams_as_error_but_keeps_text(db):
    conv = db.create_conversation()
    streaming = db.add_message(conv["id"], "assistant", status="streaming")
    db.update_message(streaming["id"], content="half an ans")
    done = db.add_message(conv["id"], "assistant", "fine", status="complete")

    assert db.recover_interrupted() == 1

    row = db.get_message(streaming["id"])
    assert (row["status"], row["content"], row["stop_reason"]) == ("error", "half an ans", "interrupted")
    assert db.get_message(done["id"])["status"] == "complete"
    assert not db.has_streaming_message(conv["id"])


def test_conversation_totals_sum_cost_and_flag_unpriced(db):
    conv = db.create_conversation()
    a = db.add_message(conv["id"], "assistant", "a")
    b = db.add_message(conv["id"], "assistant", "b")
    db.update_message(a["id"], input_tokens=70, output_tokens=30, total_tokens=100, cost_usd=0.25)
    db.update_message(b["id"], input_tokens=40, output_tokens=10, total_tokens=50)  # usage known, price unknown
    totals = db.conversation_totals(conv["id"])
    assert totals == {
        "total_cost_usd": 0.25, "total_tokens": 150, "total_input_tokens": 110,
        "total_cached_input_tokens": 0, "total_output_tokens": 40, "unpriced_messages": 1,
    }


# ── rolling summary + side-call accounting ───────────────────────────────
def test_summary_roundtrip_and_default(db):
    cid = db.create_conversation()["id"]
    assert db.get_summary(cid) == (None, None)
    db.set_summary(cid, "- likes FastAPI", "m-17")
    assert db.get_summary(cid) == ("- likes FastAPI", "m-17")
    assert "summary" not in db.get_conversation(cid)  # the API shape of a conversation is unchanged


def test_llm_calls_are_recorded_and_survive_deleting_the_conversation(db):
    cid = db.create_conversation()["id"]
    db.add_llm_call(cid, "title", "m", served_model="snap", input_tokens=10, output_tokens=2, total_tokens=12, cost_usd=0.0001)
    db.add_llm_call(cid, "summary", "m", cost_usd=None)
    assert [c["purpose"] for c in db.list_llm_calls(cid)] == ["title", "summary"]

    db.delete_conversation(cid)
    assert len(db.list_llm_calls()) == 2  # spend history outlives the chat


def test_llm_calls_reject_unknown_purpose_and_columns(db):
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        db.add_llm_call(None, "gossip", "m")
    with pytest.raises(ValueError):
        db.add_llm_call(None, "title", "m", evil="1; DROP TABLE messages")


def test_old_databases_gain_the_summary_columns(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE conversations(id TEXT PRIMARY KEY, title TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
    conn.execute("INSERT INTO conversations VALUES ('c', 't', 'x', 'x')")
    conn.commit(); conn.close()

    migrated = Database(path)
    migrated.init_schema()
    assert migrated.get_summary("c") == (None, None)
    migrated.set_summary("c", "s", "m1")
    assert migrated.get_summary("c") == ("s", "m1")

"""Database tests: a REAL PostgreSQL database per test (see conftest.py), no mocks.
The constraints, cascades and ordering rules are the database's job, so they are tested
against the database itself."""
import psycopg
import pytest


def test_conversation_creation(db, uid):
    conv = db.create_conversation(uid, "Hello")
    assert conv["title"] == "Hello"
    assert db.get_conversation(conv["id"], uid)["id"] == conv["id"]
    assert [c["id"] for c in db.list_conversations(uid)] == [conv["id"]]


def test_delete_conversation_removes_its_messages(db, uid):
    cid = db.create_conversation(uid)["id"]
    other = db.create_conversation(uid)["id"]
    mid = db.add_message(cid, "user", "hi")["id"]
    db.add_message(other, "user", "keep me")

    assert db.delete_conversation(cid, uid) is True
    assert db.get_conversation(cid, uid) is None
    assert db.get_message(mid) is None
    assert len(db.list_messages(other)) == 1
    assert db.delete_conversation(cid, uid) is False


def test_get_missing_conversation_returns_none(db, uid):
    assert db.get_conversation("nope", uid) is None


def test_conversations_listed_most_recently_active_first(db, uid):
    first = db.create_conversation(uid, "first")
    second = db.create_conversation(uid, "second")
    db.touch_conversation(first["id"])  # activity on the older one moves it to the top
    assert [c["id"] for c in db.list_conversations(uid)] == [first["id"], second["id"]]


def test_message_creation_and_defaults(db, uid):
    conv = db.create_conversation(uid)
    msg = db.add_message(conv["id"], "assistant", status="streaming")
    assert msg["content"] == ""
    assert msg["status"] == "streaming"
    # unknown is NULL, never 0
    assert msg["cost_usd"] is None and msg["total_tokens"] is None


def test_message_update(db, uid):
    conv = db.create_conversation(uid)
    msg = db.add_message(conv["id"], "assistant", status="streaming")
    db.update_message(
        msg["id"], content="partial", status="error",
        input_tokens=10, output_tokens=5, total_tokens=15, cost_usd=0.001,
    )
    row = db.get_message(msg["id"])
    assert (row["content"], row["status"], row["total_tokens"], row["cost_usd"]) == (
        "partial", "error", 15, 0.001,
    )


def test_messages_come_back_in_insertion_order(db, uid):
    conv = db.create_conversation(uid)
    # Created in the same millisecond on purpose: seq (insertion order) must break the tie.
    ids = [db.add_message(conv["id"], r, f"m{i}")["id"]
           for i, r in enumerate(["user", "assistant", "user", "assistant"])]
    assert [m["id"] for m in db.list_messages(conv["id"])] == ids


def test_update_rejects_unknown_column_and_bad_status(db, uid):
    conv = db.create_conversation(uid)
    msg = db.add_message(conv["id"], "user", "hi")
    with pytest.raises(ValueError):
        db.update_message(msg["id"], id="hijack")  # not whitelisted
    with pytest.raises(ValueError):
        db.update_message(msg["id"], status="finished")  # not a valid status


def test_foreign_key_is_enforced(db):
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        db.add_message("no-such-conversation", "user", "orphan")


def test_check_constraints_reject_bad_role(db, uid):
    conv = db.create_conversation(uid)
    with pytest.raises(psycopg.errors.CheckViolation):
        db.add_message(conv["id"], "system", "nope")


def test_recover_interrupted_marks_orphaned_streams_as_error_but_keeps_text(db, uid):
    conv = db.create_conversation(uid)
    streaming = db.add_message(conv["id"], "assistant", status="streaming")
    db.update_message(streaming["id"], content="half an ans")
    done = db.add_message(conv["id"], "assistant", "fine", status="complete")

    assert db.recover_interrupted() == 1

    row = db.get_message(streaming["id"])
    assert (row["status"], row["content"], row["stop_reason"]) == ("error", "half an ans", "interrupted")
    assert db.get_message(done["id"])["status"] == "complete"
    assert not db.has_streaming_message(conv["id"])


def test_conversation_totals_sum_cost_and_flag_unpriced(db, uid):
    conv = db.create_conversation(uid)
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
def test_summary_roundtrip_and_default(db, uid):
    cid = db.create_conversation(uid)["id"]
    assert db.get_summary(cid) == (None, None)
    db.set_summary(cid, "- likes FastAPI", "m-17")
    assert db.get_summary(cid) == ("- likes FastAPI", "m-17")
    assert "summary" not in db.get_conversation(cid, uid)  # the API shape of a conversation is unchanged


def test_llm_calls_are_recorded_and_survive_deleting_the_conversation(db, uid):
    cid = db.create_conversation(uid)["id"]
    db.add_llm_call(uid, cid, "title", "m", served_model="snap", input_tokens=10, output_tokens=2, total_tokens=12, cost_usd=0.0001)
    db.add_llm_call(uid, cid, "summary", "m", cost_usd=None)
    assert [c["purpose"] for c in db.list_llm_calls(cid)] == ["title", "summary"]

    db.delete_conversation(cid, uid)
    assert len(db.list_llm_calls()) == 2  # spend history outlives the chat


def test_llm_calls_reject_unknown_purpose_and_columns(db, uid):
    with pytest.raises(psycopg.errors.CheckViolation):
        db.add_llm_call(uid, None, "gossip", "m")
    with pytest.raises(ValueError):
        db.add_llm_call(uid, None, "title", "m", evil="1; DROP TABLE messages")

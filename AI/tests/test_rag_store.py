"""Chunks in PostgreSQL: hybrid search, isolation between users and chats, cascades, atomic writes."""
import psycopg
import pytest

from tests.conftest import TEST_PASSWORD_HASH
from tests.helpers import fake_vector


def chunk(ordinal, content, page=None, heading=None):
    return {"ordinal": ordinal, "page": page, "heading": heading, "content": content,
            "tokens": max(1, len(content) // 4), "embedding": fake_vector(content)}


def add_ready(db, uid, cid, filename, texts, kind="text"):
    doc = db.add_document(uid, cid, filename, kind, 100)
    db.complete_document(doc["id"], None, [{"user_id": uid, "conversation_id": cid, **chunk(i, t)} for i, t in enumerate(texts)])
    return doc["id"]


def search(db, uid, cid, question, **kw):
    return db.search_chunks(uid, cid, fake_vector(question), question, **kw)


@pytest.fixture
def cid(db, uid):
    return db.create_conversation(uid)["id"]


# ── lifecycle ────────────────────────────────────────────────────────────
def test_a_new_document_is_processing_until_completed(db, uid, cid):
    doc = db.add_document(uid, cid, "a.txt", "text", 10)
    assert (doc["status"], doc["chunk_count"], doc["error"]) == ("processing", 0, None)
    assert db.has_ready_documents(cid, uid) is False

    db.complete_document(doc["id"], 3, [{"user_id": uid, "conversation_id": cid, **chunk(0, "hello there")}])

    ready = db.get_document(doc["id"], uid)
    assert (ready["status"], ready["chunk_count"], ready["page_count"]) == ("ready", 1, 3)
    assert db.has_ready_documents(cid, uid) is True


def test_a_failed_document_keeps_a_short_reason(db, uid, cid):
    doc = db.add_document(uid, cid, "a.txt", "text", 10)
    db.mark_document_failed(doc["id"], "No readable text was found in this file.")
    row = db.get_document(doc["id"], uid)
    assert (row["status"], row["error"]) == ("failed", "No readable text was found in this file.")
    assert db.has_ready_documents(cid, uid) is False


def test_documents_list_in_upload_order(db, uid, cid):
    ids = [db.add_document(uid, cid, f"{n}.txt", "text", 1)["id"] for n in "abc"]
    assert [d["id"] for d in db.list_documents(cid, uid)] == ids


def test_startup_recovery_fails_documents_left_processing(db, uid, cid):
    stuck = db.add_document(uid, cid, "stuck.txt", "text", 1)
    done = add_ready(db, uid, cid, "done.txt", ["fine"])
    assert db.recover_interrupted_documents() == 1
    assert db.get_document(stuck["id"], uid)["status"] == "failed"
    assert "interrupted" in db.get_document(stuck["id"], uid)["error"]
    assert db.get_document(done, uid)["status"] == "ready"


def test_unknown_kind_is_rejected_by_the_database(db, uid, cid):
    with pytest.raises(psycopg.errors.CheckViolation):
        db.add_document(uid, cid, "x.exe", "exe", 1)


# ── atomicity ────────────────────────────────────────────────────────────
def test_a_failed_chunk_insert_leaves_no_partial_document(db, uid, cid):
    doc = db.add_document(uid, cid, "a.txt", "text", 10)
    good = {"user_id": uid, "conversation_id": cid, **chunk(0, "first passage")}
    bad = {"user_id": uid, "conversation_id": cid, **chunk(1, "second passage"), "embedding": [0.1, 0.2]}  # wrong size
    with pytest.raises(psycopg.Error):
        db.complete_document(doc["id"], None, [good, bad])

    assert db.get_document(doc["id"], uid)["status"] == "processing"  # NOT half-ready
    assert search(db, uid, cid, "first passage") == []  # and the good chunk was rolled back too


def test_completing_twice_replaces_the_chunks(db, uid, cid):
    doc = db.add_document(uid, cid, "a.txt", "text", 10)
    for text in ("old words here", "new words here"):
        db.complete_document(doc["id"], None, [{"user_id": uid, "conversation_id": cid, **chunk(0, text)}])
    assert db.get_document(doc["id"], uid)["chunk_count"] == 1
    assert [r["content"] for r in search(db, uid, cid, "words here")] == ["new words here"]


# ── search ───────────────────────────────────────────────────────────────
def test_search_finds_the_best_passage_first(db, uid, cid):
    add_ready(db, uid, cid, "policy.txt", [
        "Refunds are issued within five business days of the return.",
        "Our office is closed on public holidays and weekends.",
        "Shipping to remote islands may take three extra weeks.",
    ])
    hits = search(db, uid, cid, "how long do refunds take")
    assert hits[0]["content"].startswith("Refunds are issued")
    assert hits[0]["filename"] == "policy.txt"


def test_a_passage_found_by_both_methods_outranks_one_found_by_one(db, uid, cid):
    add_ready(db, uid, cid, "a.txt", ["the quarterly revenue grew strongly", "gardening tips for spring tomatoes"])
    top, *rest = search(db, uid, cid, "quarterly revenue")
    assert top["vector_rank"] == 1 and top["keyword_rank"] == 1
    assert top["score"] == pytest.approx(2 / 61)


def test_keyword_search_finds_a_rare_code_the_vector_may_miss(db, uid, cid):
    add_ready(db, uid, cid, "a.txt", [f"filler passage number {i} about nothing in particular" for i in range(8)] + ["Part code ZX9-QWERTY is the replacement valve."])
    hits = search(db, uid, cid, "ZX9", candidates=3)  # tiny candidate pool: only the keyword leg can surface it
    assert any("ZX9-QWERTY" in h["content"] for h in hits)


def test_search_respects_the_limit(db, uid, cid):
    add_ready(db, uid, cid, "a.txt", [f"common words appear in passage {i}" for i in range(10)])
    assert len(search(db, uid, cid, "common words", limit=3)) == 3


def test_search_with_nothing_indexed_returns_nothing(db, uid, cid):
    assert search(db, uid, cid, "anything at all") == []


def test_a_query_of_only_punctuation_does_not_crash(db, uid, cid):
    add_ready(db, uid, cid, "a.txt", ["some text"])
    assert isinstance(search(db, uid, cid, "!!! ??? ((("), list)


def test_the_page_and_heading_come_back_for_citations(db, uid, cid):
    doc = db.add_document(uid, cid, "r.pdf", "pdf", 1)
    db.complete_document(doc["id"], 9, [{"user_id": uid, "conversation_id": cid, **chunk(0, "warranty lasts two years", page=7, heading="Warranty")}])
    (hit,) = search(db, uid, cid, "warranty")
    assert (hit["page"], hit["heading"], hit["document_id"]) == (7, "Warranty", doc["id"])


# ── isolation: the security-critical part ────────────────────────────────
def test_one_chats_documents_are_invisible_to_another_chat(db, uid, cid):
    other = db.create_conversation(uid)["id"]
    add_ready(db, uid, cid, "a.txt", ["the secret launch date is in March"])
    assert search(db, uid, other, "secret launch date") == []
    assert len(search(db, uid, cid, "secret launch date")) == 1


def test_another_users_chunks_are_never_returned(db, uid, cid):
    mallory = db.create_user("mallory@example.com", TEST_PASSWORD_HASH)["id"]
    mallory_chat = db.create_conversation(mallory)["id"]
    add_ready(db, uid, cid, "private.txt", ["the secret launch date is in March"])
    add_ready(db, mallory, mallory_chat, "mine.txt", ["totally unrelated gardening notes"])

    assert search(db, mallory, mallory_chat, "secret launch date")[0]["filename"] == "mine.txt"
    # even naming the victim's chat id, the user filter stops it
    assert search(db, mallory, cid, "secret launch date") == []


def test_documents_are_owned_by_their_user(db, uid, cid):
    mallory = db.create_user("mallory@example.com", TEST_PASSWORD_HASH)["id"]
    doc_id = add_ready(db, uid, cid, "a.txt", ["hello"])
    assert db.get_document(doc_id, mallory) is None
    assert db.delete_document(doc_id, mallory) is False
    assert db.get_document(doc_id, uid) is not None


# ── cascades ─────────────────────────────────────────────────────────────
def _chunk_count(db):
    with db.connect() as conn:
        return conn.execute("SELECT count(*) AS n FROM chunks").fetchone()["n"]


def test_deleting_a_document_deletes_its_chunks(db, uid, cid):
    keep = add_ready(db, uid, cid, "keep.txt", ["keep me"])
    drop = add_ready(db, uid, cid, "drop.txt", ["drop me", "drop me too"])
    assert db.delete_document(drop, uid) is True
    assert _chunk_count(db) == 1 and db.get_document(keep, uid) is not None


def test_deleting_a_chat_deletes_its_documents_and_chunks(db, uid, cid):
    add_ready(db, uid, cid, "a.txt", ["one", "two"])
    db.delete_conversation(cid, uid)
    assert _chunk_count(db) == 0 and db.list_documents(cid, uid) == []


def test_deleting_a_user_deletes_everything_they_uploaded(db, uid, cid):
    add_ready(db, uid, cid, "a.txt", ["one"])
    with db.connect() as conn:
        conn.execute("DELETE FROM users WHERE id = %s", (uid,))
    assert _chunk_count(db) == 0

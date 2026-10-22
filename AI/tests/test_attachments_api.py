"""Per-chat attachments through the real HTTP API: upload, status, delete, isolation, limits,
and a full question-answering round trip where the model searches the uploaded file."""
import time

import pytest

from app.llm.client import Completed, TextDelta, ToolCallFinished, ToolCallStarted
from tests.conftest import client_for
from tests.helpers import FakeEmbedder, ScriptedLLM, make_docx, make_pdf, ok_script, parse_sse

FAQ = b"# Refunds\n\nRefunds are issued within five business days of the return.\n\n# Shipping\n\nShipping is free over fifty dollars."


def new_chat(client):
    return client.post("/conversations").json()["id"]


def upload(client, cid, name="faq.md", data=FAQ, content_type="application/octet-stream"):
    return client.post(f"/conversations/{cid}/documents", files={"file": (name, data, content_type)})


def wait_until(client, cid, status="ready", timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        docs = client.get(f"/conversations/{cid}/documents").json()
        if docs and all(d["status"] == status for d in docs):
            return docs
        time.sleep(0.02)
    raise AssertionError(f"documents never became {status}: {docs}")


# ── upload and status ────────────────────────────────────────────────────
def test_upload_returns_at_once_then_becomes_ready(make_client):
    client = make_client()
    cid = new_chat(client)

    r = upload(client, cid)

    assert r.status_code == 202
    body = r.json()
    assert body["filename"] == "faq.md" and body["kind"] == "markdown" and body["conversation_id"] == cid
    assert body["size_bytes"] == len(FAQ) and body["status"] in ("processing", "ready")
    (doc,) = wait_until(client, cid)
    assert doc["chunk_count"] == 1 and doc["error"] is None


@pytest.mark.parametrize("name, data, kind", [
    ("report.pdf", make_pdf(["quarterly numbers look good"]), "pdf"),
    ("brief.docx", make_docx([("h1", "Brief"), ("p", "Launch is in March.")]), "docx"),
    ("notes.txt", b"plain notes about the launch", "text"),
])
def test_every_supported_format_can_be_uploaded(make_client, name, data, kind):
    client = make_client()
    cid = new_chat(client)
    assert upload(client, cid, name, data).json()["kind"] == kind
    assert wait_until(client, cid)[0]["status"] == "ready"


def test_the_list_is_in_upload_order(make_client):
    client = make_client()
    cid = new_chat(client)
    for name in ("a.txt", "b.txt", "c.txt"):
        upload(client, cid, name, b"some text in " + name.encode())
    wait_until(client, cid)
    assert [d["filename"] for d in client.get(f"/conversations/{cid}/documents").json()] == ["a.txt", "b.txt", "c.txt"]


def test_a_file_that_cannot_be_read_ends_up_failed_with_a_reason(make_client):
    client = make_client()
    cid = new_chat(client)
    assert upload(client, cid, "scan.pdf", b"%PDF-1.4 broken").status_code == 202  # looks like a PDF, so it is accepted...
    (doc,) = wait_until(client, cid, "failed")  # ...and indexing explains why it failed
    assert "could not be read" in doc["error"]


def test_an_embedding_outage_marks_the_document_failed(make_client):
    from app.llm.client import LLMError

    client = make_client(embedder=FakeEmbedder(fail_with=LLMError("The model is unavailable right now.")))
    cid = new_chat(client)
    upload(client, cid)
    assert wait_until(client, cid, "failed")[0]["error"] == "The model is unavailable right now."


# ── refusals ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("name, data", [
    ("program.exe", b"MZ\x90\x00"),
    ("photo.png", b"\x89PNG\r\n"),
    ("fake.pdf", b"MZ\x90\x00 not a pdf"),
    ("fake.docx", b"not a zip at all"),
    ("empty.txt", b""),
    ("binary.txt", b"ab\x00\x01\x02cd"),
])
def test_unsupported_or_disguised_files_are_rejected_with_415(make_client, name, data):
    client = make_client()
    cid = new_chat(client)
    r = upload(client, cid, name, data)
    assert r.status_code == 415 and r.json()["detail"]
    assert client.get(f"/conversations/{cid}/documents").json() == []  # nothing was stored


def test_an_oversized_file_is_rejected_with_413(make_client):
    client = make_client(max_upload_mb=1)
    cid = new_chat(client)
    r = upload(client, cid, "big.txt", b"a" * (1024 * 1024 + 10))
    assert r.status_code == 413
    assert client.get(f"/conversations/{cid}/documents").json() == []


def test_the_per_chat_document_limit_is_enforced(make_client):
    client = make_client(max_documents_per_chat=2)
    cid = new_chat(client)
    assert [upload(client, cid, f"{i}.txt", b"some text").status_code for i in range(3)] == [202, 202, 409]


def test_uploads_are_rate_limited_per_user(make_client):
    client = make_client(upload_rate_limit_per_minute=2)
    cid = new_chat(client)
    codes = [upload(client, cid, f"{i}.txt", b"some text").status_code for i in range(3)]
    assert codes == [202, 202, 429]


@pytest.mark.parametrize("sent, stored", [
    ("../../etc/passwd.txt", "passwd.txt"),
    ("C:\\Users\\me\\notes.txt", "notes.txt"),
    ("/abs/path/report.md", "report.md"),
])
def test_a_hostile_filename_is_reduced_to_its_base_name(make_client, sent, stored):
    client = make_client()
    cid = new_chat(client)
    assert upload(client, cid, sent, b"some text here").json()["filename"] == stored


def test_uploading_to_a_missing_chat_is_404(make_client):
    assert upload(make_client(), "no-such-chat").status_code == 404


# ── delete ───────────────────────────────────────────────────────────────
def test_deleting_a_document_removes_it_and_its_chunks(make_client):
    client = make_client()
    cid = new_chat(client)
    doc_id = upload(client, cid).json()["id"]
    wait_until(client, cid)

    assert client.delete(f"/conversations/{cid}/documents/{doc_id}").status_code == 204

    assert client.get(f"/conversations/{cid}/documents").json() == []
    with client.app.state.db.connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM chunks").fetchone()["n"] == 0
    assert client.delete(f"/conversations/{cid}/documents/{doc_id}").status_code == 404


def test_deleting_the_chat_removes_its_documents(make_client):
    client = make_client()
    cid = new_chat(client)
    upload(client, cid)
    wait_until(client, cid)
    client.delete(f"/conversations/{cid}")
    with client.app.state.db.connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM documents").fetchone()["n"] == 0


def test_a_document_cannot_be_deleted_through_another_chat(make_client):
    client = make_client()
    a, b = new_chat(client), new_chat(client)
    doc_id = upload(client, a).json()["id"]
    assert client.delete(f"/conversations/{b}/documents/{doc_id}").status_code == 404
    assert len(client.get(f"/conversations/{a}/documents").json()) == 1


# ── another user ─────────────────────────────────────────────────────────
def test_other_users_cannot_see_upload_or_delete_in_my_chat(make_client):
    mine = make_client()
    cid = new_chat(mine)
    doc_id = upload(mine, cid).json()["id"]
    wait_until(mine, cid)
    intruder = client_for(mine)

    assert intruder.get(f"/conversations/{cid}/documents").status_code == 404
    assert upload(intruder, cid).status_code == 404
    assert intruder.delete(f"/conversations/{cid}/documents/{doc_id}").status_code == 404
    assert len(mine.get(f"/conversations/{cid}/documents").json()) == 1  # untouched


def test_the_model_never_sees_another_users_documents(make_client):
    llm = ScriptedLLM(ok_script("ok"))
    mine = make_client(llm)
    cid = new_chat(mine)
    upload(mine, cid)
    wait_until(mine, cid)
    intruder = client_for(mine)
    their_chat = new_chat(intruder)

    intruder.post(f"/conversations/{their_chat}/messages", json={"text": "what is the refund policy?"})
    mine.post(f"/conversations/{cid}/messages", json={"text": "what is the refund policy?"})

    assert llm.tools_seen[0] is None  # the intruder's chat has no documents, so no document tool
    assert llm.tools_seen[1] is not None


# ── tool offering and /info ──────────────────────────────────────────────
def test_the_document_tool_is_offered_only_once_a_document_is_ready(make_client):
    llm = ScriptedLLM(ok_script("x"))
    client = make_client(llm)
    cid = new_chat(client)

    client.post(f"/conversations/{cid}/messages", json={"text": "before upload"})
    upload(client, cid)
    wait_until(client, cid)
    client.post(f"/conversations/{cid}/messages", json={"text": "after upload"})

    assert llm.tools_seen[0] is None
    assert [d["name"] for d in llm.tools_seen[1].definitions] == ["search_documents"]
    assert "search_documents" in llm.tools_seen[1].instructions or "documents" in llm.tools_seen[1].instructions


def test_a_failed_document_does_not_turn_the_tool_on(make_client):
    llm = ScriptedLLM(ok_script("x"))
    client = make_client(llm)
    cid = new_chat(client)
    upload(client, cid, "scan.pdf", b"%PDF-1.4 broken")
    wait_until(client, cid, "failed")
    client.post(f"/conversations/{cid}/messages", json={"text": "hello"})
    assert llm.tools_seen == [None]


def test_info_advertises_attachments(make_client):
    assert make_client().get("/info").json()["attachments_available"] is True


# ── the whole loop ───────────────────────────────────────────────────────
class DocumentUsingLLM(ScriptedLLM):
    """Behaves like a model that decides to search the attached files, then answers from them."""

    async def stream(self, history, tools=None, cache_key=None):
        self.histories.append(history)
        self.tools_seen.append(tools)
        outcome = await tools.run("search_documents", '{"query": "how long do refunds take"}')
        yield ToolCallStarted("c1", "search_documents", outcome.query)
        yield ToolCallFinished("c1", "search_documents", outcome.sources, outcome.error)
        self.tool_text = outcome.model_text
        yield TextDelta("Five business days [1].")
        yield Completed("completed", None)


def test_asking_about_an_uploaded_file_retrieves_and_cites_it(make_client):
    llm = DocumentUsingLLM([])
    client = make_client(llm)
    cid = new_chat(client)
    upload(client, cid, "policy.md", FAQ)
    wait_until(client, cid)

    events = dict(parse_sse(client.post(f"/conversations/{cid}/messages", json={"text": "What is the refund policy?"}).text))

    (source, *_) = events["tool_result"]["sources"]
    assert source["n"] == 1 and source["kind"] == "document"
    assert source["title"].startswith("policy.md") and "five business days" in source["snippet"]
    assert source["document_id"] and source["url"] == ""
    assert "five business days" in llm.tool_text and "(from the user's document)" in llm.tool_text

    assert events["done"]["web_searches"] == 0  # searching your own files is not a web search

    reply = client.get(f"/conversations/{cid}").json()["messages"][-1]
    assert reply["content"] == "Five business days [1]." and reply["status"] == "complete"
    assert reply["sources"][0]["kind"] == "document"  # the citation survives a reload
    assert reply["web_searches"] is None


def test_usage_shows_embedding_spend_separately(make_client, settings):
    settings.prices_path.write_text('[models."fake-snapshot"]\ninput = 0.02\noutput = 0.0\n')
    llm = DocumentUsingLLM([])
    client = make_client(llm)
    cid = new_chat(client)
    upload(client, cid)
    wait_until(client, cid)
    client.post(f"/conversations/{cid}/messages", json={"text": "What is the refund policy?"})

    calls = client.app.state.db.list_llm_calls(cid)
    assert [c["purpose"] for c in calls].count("embedding") == 2  # one to index the file, one to embed the question
    report = client.get("/usage?days=30")
    assert report.status_code == 200
    embedding = {p["purpose"]: p for p in report.json()["by_purpose"]}["embedding"]
    assert embedding["calls"] == 2 and embedding["total_tokens"] > 0 and embedding["cost_usd"] > 0

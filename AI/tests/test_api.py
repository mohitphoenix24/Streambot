"""HTTP-level tests through FastAPI's in-process TestClient (ScriptedLLM, no network)."""
import pytest

from app.db import Database
from tests.helpers import ScriptedLLM, ok_script, parse_sse


def new_conversation(client):
    return client.post("/conversations").json()["id"]


def send(client, cid, text="hello"):
    return client.post(f"/conversations/{cid}/messages", json={"text": text})


# ── conversations ────────────────────────────────────────────────────────
def test_create_list_and_get_conversation(make_client):
    client = make_client()
    created = client.post("/conversations")
    assert created.status_code == 201
    cid = created.json()["id"]

    assert [c["id"] for c in client.get("/conversations").json()] == [cid]
    detail = client.get(f"/conversations/{cid}").json()
    assert detail["id"] == cid and detail["messages"] == [] and detail["total_cost_usd"] == 0


def test_delete_conversation(make_client):
    client = make_client()
    cid = new_conversation(client)
    send(client, cid)

    assert client.delete(f"/conversations/{cid}").status_code == 204
    assert client.get("/conversations").json() == []
    assert client.get(f"/conversations/{cid}").status_code == 404
    assert client.delete(f"/conversations/{cid}").status_code == 404


def test_cannot_delete_a_conversation_while_a_reply_is_streaming(make_client, settings):
    client = make_client()
    cid = new_conversation(client)
    Database(settings.database_path).add_message(cid, "assistant", "", status="streaming")

    assert client.delete(f"/conversations/{cid}").status_code == 409
    assert client.get(f"/conversations/{cid}").status_code == 200


def test_unknown_conversation_is_404(make_client):
    client = make_client()
    assert client.get("/conversations/nope").status_code == 404
    assert send(client, "nope").status_code == 404


def test_info_reports_the_backend_and_model_but_no_secrets(make_client):
    body = make_client().get("/info").json()
    assert body == {"model": "test-model", "usd_to_inr": 88.0,
                    "usd_to_inr_source": "fixed", "usd_to_inr_as_of": None,
                    "web_search_available": False}
    hidden = make_client(usd_to_inr=0).get("/info").json()
    assert (hidden["usd_to_inr"], hidden["usd_to_inr_source"]) == (None, "off")  # 0 hides INR


def test_health(make_client):
    assert make_client().get("/health").json() == {"status": "ok"}


def test_conversation_survives_a_server_restart(make_client, settings):
    client = make_client(ScriptedLLM(ok_script("persisted answer")))
    cid = new_conversation(client)
    send(client, cid, "remember me")

    restarted = make_client(ScriptedLLM(ok_script("x")))  # new app instance, same SQLite file
    messages = restarted.get(f"/conversations/{cid}").json()["messages"]
    assert [m["content"] for m in messages] == ["remember me", "persisted answer"]


# ── history reaches the model ────────────────────────────────────────────
def test_previous_turns_are_sent_to_the_model(make_client):
    llm = ScriptedLLM(ok_script("Nice to meet you."))
    client = make_client(llm)
    cid = new_conversation(client)
    send(client, cid, "My name is Mohit.")
    llm.script = ok_script("Your name is Mohit.")
    send(client, cid, "What is my name?")

    assert llm.histories[1] == [
        {"role": "user", "content": "My name is Mohit."},
        {"role": "assistant", "content": "Nice to meet you."},
        {"role": "user", "content": "What is my name?"},
    ]


def test_low_history_budget_makes_the_model_forget_and_says_so(make_client):
    llm = ScriptedLLM(ok_script("a" * 100))
    # Memory off: this exercises the plain-truncation safety net (see test_memory.py for summaries).
    client = make_client(llm, history_budget_tokens=40, memory_summary_enabled=False)
    cid = new_conversation(client)
    send(client, cid, "x" * 100)  # first turn
    start = [d for n, d in parse_sse(send(client, cid, "second").text) if n == "start"][0]

    assert start["history_truncated"] is True
    assert {"role": "user", "content": "x" * 100} not in llm.histories[1]  # forgotten
    assert llm.histories[1][-1] == {"role": "user", "content": "second"}  # newest kept
    assert llm.histories[1][0]["role"] == "user"


# ── titles ───────────────────────────────────────────────────────────────
def test_title_generated_after_first_message_only(make_client):
    llm = ScriptedLLM(ok_script("answer"), title="Understanding RAG")
    client = make_client(llm)
    cid = new_conversation(client)

    events = parse_sse(send(client, cid, "How does RAG work?").text)
    assert events[-1] == ("title", {"title": "Understanding RAG"})  # arrives AFTER done
    assert [n for n, _ in events].index("done") < [n for n, _ in events].index("title")
    assert client.get(f"/conversations/{cid}").json()["title"] == "Understanding RAG"

    second = parse_sse(send(client, cid, "and more?").text)
    assert "title" not in [n for n, _ in second]
    assert llm.title_requests == ["How does RAG work?"]


def test_title_waits_for_the_first_completed_reply_and_names_the_first_question(make_client):
    llm = ScriptedLLM([ok_script("x")[0], RuntimeError("boom")], title="Real Title")
    client = make_client(llm)
    cid = new_conversation(client)

    first = parse_sse(send(client, cid, "first question").text)  # reply fails
    assert "title" not in [n for n, _ in first]
    assert client.get(f"/conversations/{cid}").json()["title"] == "first question"  # provisional

    llm.script = ok_script("fine now")
    second = parse_sse(send(client, cid, "second question").text)  # first COMPLETED reply
    assert second[-1] == ("title", {"title": "Real Title"})
    assert llm.title_requests == ["first question"]  # named after the conversation's opening question
    assert client.get(f"/conversations/{cid}").json()["title"] == "Real Title"


def test_failed_title_call_keeps_the_provisional_title(make_client):
    client = make_client(ScriptedLLM(ok_script("answer"), title=None))
    cid = new_conversation(client)
    send(client, cid, "How does RAG work?")
    assert client.get(f"/conversations/{cid}").json()["title"] == "How does RAG work?"


# ── input protection ─────────────────────────────────────────────────────
def test_empty_message_is_rejected(make_client):
    client = make_client()
    cid = new_conversation(client)
    assert send(client, cid, "   ").status_code == 422


def test_overlong_message_is_rejected_before_the_model_is_called(make_client):
    llm = ScriptedLLM(ok_script("x"))
    client = make_client(llm)  # fixture limit: 200 chars
    cid = new_conversation(client)
    assert send(client, cid, "x" * 201).status_code == 413
    assert llm.histories == []  # no model call, no cost
    assert client.get(f"/conversations/{cid}").json()["messages"] == []  # nothing saved


def test_oversized_request_body_is_rejected_by_content_length(make_client):
    client = make_client()  # limit = 200*12+1024 bytes
    cid = new_conversation(client)
    assert client.post(f"/conversations/{cid}/messages", content=b"x" * 5000,
                       headers={"content-type": "application/json"}).status_code == 413


def test_second_message_while_a_reply_is_streaming_is_409(make_client, settings):
    client = make_client()
    cid = new_conversation(client)
    db = Database(settings.database_path)
    db.add_message(cid, "user", "q")
    db.add_message(cid, "assistant", status="streaming")  # a reply is in flight
    assert send(client, cid).status_code == 409


def test_rate_limit_returns_429_with_retry_after(make_client):
    client = make_client(rate_limit_per_minute=2)
    cid = new_conversation(client)
    assert [send(client, cid).status_code for _ in range(2)] == [200, 200]
    limited = send(client, cid)
    assert limited.status_code == 429
    assert int(limited.headers["retry-after"]) >= 1


def test_streaming_response_headers_disable_buffering_and_caching(make_client):
    client = make_client()
    cid = new_conversation(client)
    r = send(client, cid)
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["cache-control"] == "no-cache"
    assert r.headers["x-accel-buffering"] == "no"


# ── crash recovery ───────────────────────────────────────────────────────
def test_replies_orphaned_by_a_crash_become_errors_on_startup_with_text_kept(make_client, settings):
    db = Database(settings.database_path)
    db.init_schema()
    cid = db.create_conversation()["id"]
    db.add_message(cid, "user", "q")
    orphan = db.add_message(cid, "assistant", status="streaming")
    db.update_message(orphan["id"], content="half an answer")  # last checkpoint before kill -9

    client = make_client()  # startup runs recovery
    row = client.get(f"/conversations/{cid}").json()["messages"][1]
    assert (row["status"], row["content"]) == ("error", "half an answer")
    assert send(client, cid).status_code == 200  # and the conversation is usable again


# ── non-streaming endpoint + cors ──────────────────────────────
def test_sync_endpoint_returns_the_full_message(make_client):
    client = make_client()
    cid = new_conversation(client)
    r = client.post(f"/conversations/{cid}/messages/sync", json={"text": "hi"})
    assert r.status_code == 200
    body = r.json()
    assert (body["content"], body["status"], body["total_tokens"]) == ("complete-text", "complete", 15)


def test_cors_allows_the_frontend_origin_only(make_client):
    client = make_client()
    ok = client.options("/conversations", headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "POST"})
    bad = client.options("/conversations", headers={"Origin": "http://evil.example", "Access-Control-Request-Method": "POST"})
    assert ok.headers.get("access-control-allow-origin") == "http://localhost:5173"
    assert "access-control-allow-origin" not in bad.headers


# ── web search tool ──────────────────────────────────────────────────────
from app.llm.client import Completed, TextDelta, ToolCallFinished, ToolCallStarted  # noqa: E402
from app.tools.web_search import TavilySearch  # noqa: E402

SRC = {"n": 1, "title": "Python 3.14", "url": "https://python.org/news", "snippet": "released"}


def search_tool():
    async def fetch(query, n):
        return {"results": []}
    return TavilySearch("k", fetch=fetch)


def tool_script(*after, error=None):
    return [ToolCallStarted("c1", "web_search", "python release"),
            ToolCallFinished("c1", "web_search", () if error else (SRC,), error), *after]


def test_tool_events_stream_in_order_and_sources_are_persisted(make_client):
    llm = ScriptedLLM(tool_script(TextDelta("Out now [1]"), Completed("completed", None)))
    client = make_client(llm, search=search_tool())
    cid = new_conversation(client)

    events = parse_sse(send(client, cid).text)
    assert [name for name, _ in events] == ["start", "tool_call", "tool_result", "delta", "done", "title"]
    data = dict(events)
    assert data["tool_call"] == {"id": "c1", "name": "web_search", "query": "python release"}
    assert data["tool_result"]["sources"] == [SRC] and data["tool_result"]["error"] is None
    assert data["done"]["web_searches"] == 1

    reply = client.get(f"/conversations/{cid}").json()["messages"][-1]
    assert reply["sources"] == [SRC] and reply["web_searches"] == 1 and reply["content"] == "Out now [1]"


def test_sources_are_kept_when_the_reply_fails_afterwards(make_client):
    llm = ScriptedLLM(tool_script(TextDelta("partial"), RuntimeError("boom")))
    client = make_client(llm, search=search_tool())
    cid = new_conversation(client)
    send(client, cid)
    reply = client.get(f"/conversations/{cid}").json()["messages"][-1]
    assert reply["status"] == "error" and reply["sources"] == [SRC] and reply["web_searches"] == 1


def test_a_failed_search_is_reported_but_not_counted(make_client):
    llm = ScriptedLLM(tool_script(TextDelta("answer"), Completed("completed", None), error="Web search is unavailable right now."))
    client = make_client(llm, search=search_tool())
    cid = new_conversation(client)
    events = dict(parse_sse(send(client, cid).text))
    assert events["tool_result"]["error"] == "Web search is unavailable right now."
    assert events["done"]["web_searches"] == 0
    reply = client.get(f"/conversations/{cid}").json()["messages"][-1]
    assert reply["sources"] == [] and reply["web_searches"] is None and reply["status"] == "complete"


def test_the_toggle_and_the_api_key_control_whether_tools_are_offered(make_client):
    llm = ScriptedLLM(ok_script("x"))
    with_key = make_client(llm, search=search_tool())
    cid = new_conversation(with_key)
    with_key.post(f"/conversations/{cid}/messages", json={"text": "a"})  # default: on
    with_key.post(f"/conversations/{cid}/messages", json={"text": "b", "web_search": False})
    assert [t is not None for t in llm.tools_seen] == [True, False]

    no_key = ScriptedLLM(ok_script("x"))
    client = make_client(no_key)  # no search configured
    cid = new_conversation(client)
    client.post(f"/conversations/{cid}/messages", json={"text": "a"})
    assert no_key.tools_seen == [None]


def test_info_says_whether_web_search_is_available(make_client):
    assert make_client(search=search_tool()).get("/info").json()["web_search_available"] is True
    assert make_client().get("/info").json()["web_search_available"] is False


# ── rolling memory through the API ───────────────────────────────────────
import time  # noqa: E402


def wait_for(predicate, timeout=3.0):
    """The summary is written by a background task; poll instead of sleeping blindly."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_a_long_chat_gets_summarized_and_the_next_turn_uses_the_summary(make_client, settings):
    llm = ScriptedLLM(ok_script("a" * 100))
    client = make_client(llm, history_budget_tokens=200, max_message_chars=500)
    cid = new_conversation(client)
    db = Database(settings.database_path)

    for i in range(6):
        send(client, cid, f"question {i} " + "q" * 80)
    assert wait_for(lambda: db.get_summary(cid)[0] is not None), "summary was never stored"
    assert llm.summary_requests, "the summary call was never made"

    events = parse_sse(send(client, cid, "and one more").text)
    start = dict(events)["start"]
    assert start["history_summarized"] is True
    sent = llm.histories[-1]
    assert sent[0]["role"] == "developer" and "SCRIPTED SUMMARY" in sent[0]["content"]
    assert sent[1]["role"] == "user"  # the verbatim part still opens with a user turn
    assert sent[-1] == {"role": "user", "content": "and one more"}
    assert not any("question 0" in m["content"] for m in sent[1:])  # folded away, not repeated


def test_short_chats_are_never_summarized(make_client, settings):
    llm = ScriptedLLM(ok_script("hi"))
    client = make_client(llm)
    cid = new_conversation(client)
    for i in range(3):
        start = dict(parse_sse(send(client, cid, f"q{i}").text))["start"]
        assert start["history_summarized"] is False
    time.sleep(0.1)
    assert llm.summary_requests == []


def test_memory_can_be_switched_off(make_client, settings):
    llm = ScriptedLLM(ok_script("a" * 100))
    client = make_client(llm, history_budget_tokens=200, max_message_chars=500, memory_summary_enabled=False)
    cid = new_conversation(client)
    for i in range(6):
        send(client, cid, f"question {i} " + "q" * 80)
    time.sleep(0.2)
    assert llm.summary_requests == [] and Database(settings.database_path).get_summary(cid) == (None, None)


def test_the_title_call_is_recorded_in_llm_calls(make_client, settings):
    client = make_client(ScriptedLLM(ok_script("ok"), title="A Title"))
    cid = new_conversation(client)
    send(client, cid, "hello")
    (call,) = Database(settings.database_path).list_llm_calls(cid)
    assert call["purpose"] == "title" and call["total_tokens"] == 36 and call["served_model"] == "title-snapshot"


def test_a_failing_summary_never_breaks_the_chat(make_client, settings):
    llm = ScriptedLLM(ok_script("a" * 100))
    llm.summary_text = None  # every summary call fails
    client = make_client(llm, history_budget_tokens=200, max_message_chars=500)
    cid = new_conversation(client)
    for i in range(8):
        assert send(client, cid, f"question {i} " + "q" * 80).status_code == 200
    reply = client.get(f"/conversations/{cid}").json()["messages"][-1]
    assert reply["status"] == "complete"  # truncation kept the prompt bounded


# ── caching + usage endpoint ─────────────────────────────────────────────
def test_the_conversation_id_is_the_prompt_cache_key(make_client):
    llm = ScriptedLLM(ok_script("x"))
    client = make_client(llm)
    cid = new_conversation(client)
    send(client, cid, "a")
    send(client, cid, "b")
    assert llm.cache_keys == [cid, cid]  # every turn of a chat shares one key


def test_cached_tokens_show_up_as_savings_on_the_reply(make_client, settings):
    from app.llm.usage import Usage

    prices = settings.prices_path
    prices.write_text('[models."test-model"]\ninput = 1.0\ncached_input = 0.1\noutput = 4.0\n')
    llm = ScriptedLLM(ok_script("x", usage=Usage(1_000_000, 0, 1_000_000, cached_input_tokens=500_000)))
    client = make_client(llm)
    cid = new_conversation(client)

    done = dict(parse_sse(send(client, cid).text))["done"]
    assert done["cost"]["cache_savings"] == pytest.approx(0.45)  # 500k x ($1.00 - $0.10)
    reply = client.get(f"/conversations/{cid}").json()["messages"][-1]
    assert reply["cache_savings_usd"] == pytest.approx(0.45)  # still there after a reload


def test_usage_endpoint_reports_replies_and_titles(make_client):
    client = make_client(ScriptedLLM(ok_script("x"), title="T"))
    cid = new_conversation(client)
    send(client, cid, "hello")

    body = client.get("/usage?days=7").json()
    assert body["days"] == 7 and len(body["daily"]) == 7
    assert body["totals"]["replies"] == 1 and body["totals"]["calls"] == 2  # the reply + its title call
    assert [p["purpose"] for p in body["by_purpose"]] == ["reply", "title", "summary"]


@pytest.mark.parametrize("query", ["days=0", "days=400", "utc_offset_minutes=9999", "days=abc"])
def test_usage_endpoint_validates_its_parameters(make_client, query):
    assert make_client().get(f"/usage?{query}").status_code == 422


def test_deleting_a_chat_does_not_change_the_usage_totals(make_client):
    client = make_client(ScriptedLLM(ok_script("x"), title="T"))
    keep, gone = new_conversation(client), new_conversation(client)
    send(client, keep, "stay")
    send(client, gone, "go")
    before = client.get("/usage?days=7").json()

    assert client.delete(f"/conversations/{gone}").status_code == 204
    after = client.get("/usage?days=7").json()

    assert after["totals"] == before["totals"]  # the deleted chat's tokens were still spent
    assert after["totals"]["replies"] == 2
    assert [c["id"] for c in after["top_conversations"]] in ([keep], [])  # but it can't be linked to any more

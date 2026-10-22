"""The most important tests: what happens when a stream goes wrong halfway.

No real model is involved. ScriptedLLM plays back a fixed script, so these tests
are fast and deterministic (a real provider would be slow, flaky, and cost money).
"""
import asyncio

import pytest

from app.llm.client import Completed, LLMError, TextDelta, Usage
from app.llm.pricing import PriceTable
from app.chat.streaming import begin_turn, stream_reply
from tests.helpers import ScriptedLLM, ok_script, parse_sse


def new_conversation(client):
    return client.post("/conversations").json()["id"]


def assistant_row(client, cid):
    return client.get(f"/conversations/{cid}").json()["messages"][1]


# ── the headline test ────────────────────────────────────────────────────
def test_stream_failure_keeps_partial_text_marks_error_and_emits_error_event(make_client):
    llm = ScriptedLLM([TextDelta("Hello "), TextDelta("world"), LLMError("The model is unavailable right now.")])
    client = make_client(llm)
    cid = new_conversation(client)

    response = client.post(f"/conversations/{cid}/messages", json={"text": "hi"})
    events = parse_sse(response.text)

    # SSE: start, the two deltas that made it through, then an error event
    assert [name for name, _ in events] == ["start", "delta", "delta", "error"]
    assert events[-1][1] == {"message": "The model is unavailable right now.", "partial": True}

    # database: the partial reply is NOT lost, and is marked as an error
    row = assistant_row(client, cid)
    assert row["content"] == "Hello world"
    assert row["status"] == "error"
    assert row["stop_reason"] == "error"


def test_internal_exception_details_never_reach_the_browser(make_client):
    secret = "ConnectionError at https://internal.example/v1 key=sk-SECRET-123"
    client = make_client(ScriptedLLM([TextDelta("part"), RuntimeError(secret)]))
    cid = new_conversation(client)

    body = client.post(f"/conversations/{cid}/messages", json={"text": "hi"}).text

    assert "sk-SECRET-123" not in body and "internal.example" not in body
    error = [d for n, d in parse_sse(body) if n == "error"][0]
    assert error["message"] == "The model is unavailable right now."  # generic and safe
    assert assistant_row(client, cid)["content"] == "part"


def test_failure_before_any_token_leaves_an_empty_error_row_not_a_missing_one(make_client):
    client = make_client(ScriptedLLM([LLMError("The model is unavailable right now.")]))
    cid = new_conversation(client)

    events = parse_sse(client.post(f"/conversations/{cid}/messages", json={"text": "hi"}).text)

    assert events[-1][0] == "error" and events[-1][1]["partial"] is False
    messages = client.get(f"/conversations/{cid}").json()["messages"]
    assert [(m["role"], m["status"]) for m in messages] == [("user", "complete"), ("assistant", "error")]
    assert messages[1]["content"] == ""


def test_stream_that_ends_without_a_completed_event_is_an_error_not_a_success(make_client):
    # connection dropped silently: deltas, then the iterator just stops
    client = make_client(ScriptedLLM([TextDelta("cut "), TextDelta("off")]))
    cid = new_conversation(client)

    events = parse_sse(client.post(f"/conversations/{cid}/messages", json={"text": "hi"}).text)

    assert events[-1][0] == "error"
    row = assistant_row(client, cid)
    assert (row["status"], row["content"]) == ("error", "cut off")


def test_failed_reply_is_not_fed_back_into_the_next_prompt(make_client):
    llm = ScriptedLLM([TextDelta("broken"), RuntimeError("x")])
    client = make_client(llm)
    cid = new_conversation(client)
    client.post(f"/conversations/{cid}/messages", json={"text": "first"})

    llm.script = ok_script("fine")
    client.post(f"/conversations/{cid}/messages", json={"text": "second"})

    assert llm.histories[1] == [{"role": "user", "content": "first"}, {"role": "user", "content": "second"}]


# ── success path ─────────────────────────────────────────────────────────
def test_successful_stream_emits_done_with_usage_and_persists_everything(make_client, settings):
    settings.prices_path.write_text('[models."test-model"]\ninput = 1.0\noutput = 2.0\n')
    client = make_client(ScriptedLLM(ok_script("Hi ", "there", usage=Usage(100, 50, 150))))
    cid = new_conversation(client)

    events = parse_sse(client.post(f"/conversations/{cid}/messages", json={"text": "hello"}).text)

    names = [n for n, _ in events]
    assert names[:4] == ["start", "delta", "delta", "done"]
    done = events[3][1]
    assert done["stop_reason"] == "completed"
    assert done["usage"] == {
        "input_tokens": 100, "cached_input_tokens": None, "output_tokens": 50,
        "reasoning_tokens": None, "total_tokens": 150,
    }
    assert done["cost"]["total_cost"] == pytest.approx(100 * 1 / 1e6 + 50 * 2 / 1e6)
    assert done["ttft_ms"] is not None and done["conversation_total_tokens"] == 150

    detail = client.get(f"/conversations/{cid}").json()
    user, assistant = detail["messages"]
    assert (user["role"], user["content"], user["status"]) == ("user", "hello", "complete")
    assert (assistant["content"], assistant["status"], assistant["total_tokens"]) == ("Hi there", "complete", 150)
    assert assistant["cost_usd"] == pytest.approx(0.0002)
    assert detail["total_cost_usd"] == pytest.approx(0.0002)


def test_unpriced_model_stores_null_cost_not_zero(make_client):
    client = make_client(ScriptedLLM(ok_script("x")))  # no prices file in tmp_path
    cid = new_conversation(client)
    done = [d for n, d in parse_sse(client.post(f"/conversations/{cid}/messages", json={"text": "q"}).text) if n == "done"][0]

    assert done["cost"] is None
    assert assistant_row(client, cid)["cost_usd"] is None
    assert client.get(f"/conversations/{cid}").json()["unpriced_messages"] == 1


def test_output_limit_is_reported_not_hidden(make_client):
    client = make_client(ScriptedLLM(ok_script("trunc", stop="max_output_tokens")))
    cid = new_conversation(client)
    done = [d for n, d in parse_sse(client.post(f"/conversations/{cid}/messages", json={"text": "q"}).text) if n == "done"][0]

    assert done["stop_reason"] == "max_output_tokens"
    assert assistant_row(client, cid)["stop_reason"] == "max_output_tokens"


# ── cancellation (driving the generator directly) ────────────────────────
class FakeRequest:
    """Stands in for starlette's Request: reports 'disconnected' from the Nth poll on."""

    def __init__(self, disconnect_on_poll: int | None = None) -> None:
        self.polls = 0
        self.disconnect_on_poll = disconnect_on_poll

    async def is_disconnected(self) -> bool:
        self.polls += 1
        return self.disconnect_on_poll is not None and self.polls >= self.disconnect_on_poll


def start_turn(db, settings, llm):
    cid = db.create_conversation()["id"]
    return cid, begin_turn(db, llm, settings, cid, "question")


async def drain(gen):
    return [chunk async for chunk in gen]


async def test_client_disconnect_stops_generation_and_saves_partial_as_cancelled(db, settings):
    llm = ScriptedLLM(ok_script("one ", "two ", "three ", "four "))
    cid, turn = start_turn(db, settings, llm)

    # the 3rd is_disconnected() poll says "the browser is gone"
    chunks = await drain(stream_reply(
        request=FakeRequest(disconnect_on_poll=3), db=db, llm=llm, settings=settings,
        prices=PriceTable(), turn=turn, started_at=0.0,
    ))

    names = [n for n, _ in parse_sse("".join(chunks))]
    assert names == ["start", "delta", "delta"]  # no done/error: nobody is listening
    row = db.get_message(turn.assistant_message["id"])
    assert (row["status"], row["stop_reason"], row["content"]) == ("cancelled", "cancelled", "one two ")
    assert llm.upstream_closed is True  # the provider stream was closed -> generation can stop


async def test_task_cancellation_while_waiting_for_the_model_saves_partial(db, settings):
    """If the browser disconnects while we are BLOCKED waiting for the next
    token, is_disconnected() can't run; the server cancels our task instead."""
    gate = asyncio.Event()

    class StallingLLM(ScriptedLLM):
        async def stream(self, history, tools=None, cache_key=None):
            try:
                yield TextDelta("partial ")
                await asyncio.sleep(3600)  # model stalls
            finally:
                self.upstream_closed = True

    llm = StallingLLM([])
    cid, turn = start_turn(db, settings, llm)

    async def consume():
        async for _ in stream_reply(
            request=FakeRequest(), db=db, llm=llm, settings=settings,
            prices=PriceTable(), turn=turn, started_at=0.0,
        ):
            gate.set()

    task = asyncio.create_task(consume())
    await asyncio.wait_for(gate.wait(), 2)
    await asyncio.sleep(0.05)  # now parked inside the stalled stream
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    row = db.get_message(turn.assistant_message["id"])
    assert (row["status"], row["content"]) == ("cancelled", "partial ")
    assert llm.upstream_closed is True


async def test_partial_text_is_checkpointed_to_the_database_while_streaming(db, settings):
    """Crash safety: text must reach SQLite DURING the stream, not only at the end."""
    snapshots = []

    class SnapshotLLM(ScriptedLLM):
        async def stream(self, history, tools=None, cache_key=None):
            yield TextDelta("alpha ")
            yield TextDelta("beta ")
            # by now the first delta must already have been flushed (interval=0)
            snapshots.append(db.get_message(turn.assistant_message["id"])["content"])
            yield Completed("completed", None)

    llm = SnapshotLLM([])
    cid, turn = start_turn(db, settings, llm)
    await drain(stream_reply(
        request=FakeRequest(), db=db, llm=llm, settings=settings,
        prices=PriceTable(), turn=turn, started_at=0.0,
    ))
    assert snapshots and snapshots[0].startswith("alpha")


def test_assistant_row_exists_in_streaming_state_before_the_model_is_called(db, settings):
    llm = ScriptedLLM([])
    cid = db.create_conversation()["id"]
    begin_turn(db, llm, settings, cid, "question")  # note: stream NOT started yet

    rows = db.list_messages(cid)
    assert [(r["role"], r["status"], r["content"]) for r in rows] == [
        ("user", "complete", "question"),
        ("assistant", "streaming", ""),
    ]

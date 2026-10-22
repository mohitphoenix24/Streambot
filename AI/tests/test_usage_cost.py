"""Usage and cost come from what OpenAI REPORTS, never from estimates.

    OpenAI usage -> usage.parse_usage -> pricing -> database -> SSE/UI

Real model: gpt-5.6-luna at $0.20 input / $0.02 cached input / $1.20 output per 1M.
No test here touches the network (the SDK client is a stub).
"""
import logging
import re
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from app.config import BASE_DIR, DATA_DIR, Settings
from app.llm.client import Completed, LLMError, OpenAILLM, TextDelta
from app.llm.pricing import PriceTable
from app.chat.streaming import begin_turn, stream_reply
from tests.helpers import ScriptedLLM, ok_script, parse_sse
from tests.test_streaming_failure import FakeRequest
from app.llm.usage import Usage, parse_usage

MODEL = "gpt-5.6-luna"
SNAPSHOT = "gpt-5.6-luna-2026-03-17"
EXPECTED_1000_200_300 = 800 * 0.20 / 1e6 + 200 * 0.02 / 1e6 + 300 * 1.20 / 1e6  # = 0.000524


# ── a stub of the OpenAI SDK client, shaped like the real objects we read ──
class StubStream:
    def __init__(self, items):
        self.items, self.closed = items, False

    def __aiter__(self):
        async def gen():
            for item in self.items:
                if isinstance(item, Exception):
                    raise item
                yield item
        return gen()

    async def close(self):
        self.closed = True


class StubClient:
    def __init__(self, *streams):
        self.streams = list(streams)
        self.responses = self

    async def create(self, **kwargs):
        if not kwargs.get("stream"):  # the non-streaming title call
            return NS(output_text="A Title")
        return self.streams.pop(0)


def raw_usage(inp=1000, out=300, total=1300, cached=200, reasoning=None, details=True):
    """What response.usage looks like; `details=False` omits the optional sub-objects."""
    u = NS(input_tokens=inp, output_tokens=out, total_tokens=total)
    if details:
        u.input_tokens_details = NS(cached_tokens=cached)
        u.output_tokens_details = NS(reasoning_tokens=reasoning)
    return u


def provider_stream(text_parts, usage, model=SNAPSHOT):
    done = NS(type="response.completed", response=NS(
        status="completed", incomplete_details=None, usage=usage, model=model))
    return StubStream([*(NS(type="response.output_text.delta", delta=t) for t in text_parts), done])


@pytest.fixture
def luna_client(make_client):
    """App wired to the REAL OpenAILLM adapter (stub SDK) and the SHIPPED prices.toml."""
    def build(*streams):
        s = Settings(openai_api_key="k", openai_model=MODEL,
                     llm_max_attempts=1, retry_base_delay_s=0.0)
        llm = OpenAILLM(s, client=StubClient(*streams))
        return make_client(llm, prices_path=DATA_DIR / "prices.toml")
    return build


def chat(client, text="hi"):
    cid = client.post("/conversations").json()["id"]
    body = client.post(f"/conversations/{cid}/messages", json={"text": text}).text
    return cid, parse_sse(body)


def assistant(client, cid):
    return [m for m in client.get(f"/conversations/{cid}").json()["messages"] if m["role"] == "assistant"][-1]


# ── Test 1: exact usage is stored ───────────────────────────────────────
def test_exact_usage_is_stored_exactly(luna_client):
    client = luna_client(provider_stream(["Hello"], raw_usage(1000, 300, 1300, cached=200, reasoning=50)))
    cid, events = chat(client)

    row = assistant(client, cid)
    assert (row["input_tokens"], row["cached_input_tokens"], row["output_tokens"],
            row["reasoning_tokens"], row["total_tokens"]) == (1000, 200, 300, 50, 1300)
    done = dict(events)["done"]
    assert done["usage"] == {"input_tokens": 1000, "cached_input_tokens": 200, "output_tokens": 300,
                             "reasoning_tokens": 50, "total_tokens": 1300}


# ── Test 2: cost ────────────────────────────────────────────────────────
def test_cost_for_1000_input_200_cached_300_output(luna_client):
    client = luna_client(provider_stream(["Hello"], raw_usage(1000, 300, 1300, cached=200)))
    cid, events = chat(client)

    cost = dict(events)["done"]["cost"]
    assert cost["input_cost"] == pytest.approx(800 * 0.20 / 1e6)  # NON-cached input only
    assert cost["cached_input_cost"] == pytest.approx(200 * 0.02 / 1e6)
    assert cost["output_cost"] == pytest.approx(300 * 1.20 / 1e6)
    assert cost["total_cost"] == pytest.approx(EXPECTED_1000_200_300)
    assert cost["total_cost"] == pytest.approx(0.000524)
    assert assistant(client, cid)["cost_usd"] == pytest.approx(0.000524)  # stored cost matches


def test_reasoning_tokens_are_not_double_counted(luna_client):
    # 300 output tokens, 250 of them reasoning: cost is still 300 x output rate, not 550
    with_reasoning = luna_client(provider_stream(["x"], raw_usage(1000, 300, 1300, cached=200, reasoning=250)))
    _, events = chat(with_reasoning)
    assert dict(events)["done"]["cost"]["total_cost"] == pytest.approx(EXPECTED_1000_200_300)


# ── Test 3: no estimation ───────────────────────────────────────────────
@pytest.mark.parametrize("module", ["llm/pricing.py", "llm/usage.py", "llm/client.py", "chat/streaming.py"])
def test_billing_code_contains_no_text_length_estimation(module):
    source = (BASE_DIR / "app" / module).read_text()
    assert "// 4" not in source and "//4" not in source
    assert "approx_tokens" not in source and "approx_usage" not in source


def test_usage_is_independent_of_how_long_the_streamed_text_is(luna_client):
    huge_text = "word " * 5000  # 25,000 chars: any length-based estimate would be ~6,000 tokens
    client = luna_client(provider_stream([huge_text], raw_usage(7, 3, 10, cached=0)))
    cid, _ = chat(client)
    row = assistant(client, cid)
    assert (row["input_tokens"], row["output_tokens"], row["total_tokens"]) == (7, 3, 10)
    assert row["content"] == huge_text


# ── Tests 4 & 5: optional fields may be missing ─────────────────────────
@pytest.mark.parametrize("usage", [
    raw_usage(details=False),  # no *_details objects at all
    NS(input_tokens=1000, output_tokens=300, total_tokens=1300,
       input_tokens_details=None, output_tokens_details=None),  # explicitly null
    NS(input_tokens=1000, output_tokens=300, total_tokens=1300,
       input_tokens_details=NS(cached_tokens=None), output_tokens_details=NS(reasoning_tokens=None)),
    {"input_tokens": 1000, "output_tokens": 300, "total_tokens": 1300},  # a plain dict
])
def test_missing_cached_and_reasoning_tokens_do_not_crash(luna_client, usage):
    client = luna_client(provider_stream(["ok"], usage))
    cid, events = chat(client)

    row = assistant(client, cid)
    assert row["status"] == "complete"
    assert (row["input_tokens"], row["output_tokens"], row["total_tokens"]) == (1000, 300, 1300)
    assert row["cached_input_tokens"] is None and row["reasoning_tokens"] is None  # unknown, not 0
    # nothing reported as cached -> all input is charged at the normal input rate
    assert row["cost_usd"] == pytest.approx(1000 * 0.20 / 1e6 + 300 * 1.20 / 1e6)


def test_parse_usage_edge_cases():
    assert parse_usage(None) is None
    assert parse_usage(NS()) is None  # no counts at all
    assert parse_usage(NS(input_tokens=5, output_tokens="x")) is None  # garbage
    assert parse_usage(NS(input_tokens=True, output_tokens=1)) is None  # bool is not a token count
    # total missing: sum of the two REPORTED numbers (arithmetic, not estimation)
    assert parse_usage(NS(input_tokens=5, output_tokens=7)).total_tokens == 12
    assert parse_usage({"input_tokens": 1, "output_tokens": 2, "total_tokens": 3}) == Usage(1, 2, 3)


# ── Test 6: unknown model ───────────────────────────────────────────────
def test_unknown_served_model_gives_null_cost_a_warning_and_a_successful_reply(luna_client, caplog):
    client = luna_client(provider_stream(["fine"], raw_usage(), model="brand-new-model-x"))
    with caplog.at_level(logging.WARNING, logger="chat.pricing"):
        cid, events = chat(client)

    done = dict(events)["done"]
    assert done["cost"] is None  # no fake price
    row = assistant(client, cid)
    assert row["status"] == "complete" and row["cost_usd"] is None
    assert row["total_tokens"] == 1300  # usage still recorded
    assert row["served_model"] == "brand-new-model-x" and row["model"] == MODEL
    assert any("Pricing not configured for model: brand-new-model-x" in r.getMessage() for r in caplog.records)


def test_dated_snapshot_of_the_priced_alias_uses_the_alias_price():
    table = PriceTable.load(DATA_DIR / "prices.toml")
    assert table.resolve(MODEL, SNAPSHOT) == MODEL
    assert table.resolve(MODEL, None) == MODEL
    assert table.resolve(MODEL, MODEL) == MODEL
    assert table.resolve(MODEL, "gpt-5.6-luna-pro") is None  # a different model, not a dated snapshot
    assert table.resolve(MODEL, "other-2026-03-17") is None
    assert table.cost(MODEL, "other-2026-03-17", Usage(10, 10, 20)) is None


def test_no_usage_means_no_cost():
    assert PriceTable.load(DATA_DIR / "prices.toml").cost(MODEL, MODEL, None) is None


# ── Test 7: provider error / cancellation never fabricate usage ─────────
def test_provider_error_before_usage_stores_null_usage(luna_client):
    failing = StubStream([NS(type="response.output_text.delta", delta="partial "), RuntimeError("boom")])
    client = luna_client(failing)
    cid, events = chat(client)

    assert events[-1][0] == "error"
    row = assistant(client, cid)
    assert row["status"] == "error" and row["content"] == "partial "
    assert all(row[c] is None for c in (
        "input_tokens", "cached_input_tokens", "output_tokens", "reasoning_tokens",
        "total_tokens", "cost_usd", "served_model"))


def test_error_before_any_token_also_has_null_usage(make_client):
    client = make_client(ScriptedLLM([LLMError("down")]))
    cid, _ = chat(client)
    row = assistant(client, cid)
    assert row["status"] == "error" and row["total_tokens"] is None and row["cost_usd"] is None


async def test_cancelled_reply_without_reported_usage_stores_null(settings, db):
    llm = ScriptedLLM(ok_script("a ", "b ", "c "))
    cid = db.create_conversation()["id"]
    turn = begin_turn(db, llm, settings, cid, "q")
    _ = [c async for c in stream_reply(request=FakeRequest(disconnect_on_poll=2), db=db, llm=llm,
                                       settings=settings, prices=PriceTable(), turn=turn, started_at=0.0)]
    row = db.get_message(turn.assistant_message["id"])
    assert row["status"] == "cancelled" and row["content"] == "a "
    assert row["total_tokens"] is None and row["cost_usd"] is None  # never invented


async def test_cancelled_reply_keeps_usage_if_the_provider_had_reported_it(settings, db, tmp_path):
    prices_file = tmp_path / "p.toml"
    prices_file.write_text('[models."gpt-5.6-luna"]\ninput = 0.20\ncached_input = 0.02\noutput = 1.20\n')
    usage = Usage(1000, 300, 1300, cached_input_tokens=200)
    # final event arrives, then the client disconnects before the stream fully closes
    llm = ScriptedLLM([TextDelta("hi"), Completed("completed", usage, MODEL), TextDelta("late")], model=MODEL)
    cid = db.create_conversation()["id"]
    turn = begin_turn(db, llm, settings, cid, "q")
    _ = [c async for c in stream_reply(request=FakeRequest(disconnect_on_poll=3), db=db, llm=llm,
                                       settings=settings, prices=PriceTable.load(prices_file),
                                       turn=turn, started_at=0.0)]
    row = db.get_message(turn.assistant_message["id"])
    assert row["status"] == "cancelled"
    assert (row["input_tokens"], row["cached_input_tokens"], row["output_tokens"]) == (1000, 200, 300)
    assert row["cost_usd"] == pytest.approx(EXPECTED_1000_200_300)


# ── Test 8: streaming text vs usage are separate ────────────────────────
def test_streamed_text_is_displayed_and_usage_is_captured_separately(luna_client):
    client = luna_client(provider_stream(["Str", "eam", "ed ", "text"], raw_usage(1000, 300, 1300, cached=200)))
    cid, events = chat(client)

    names = [n for n, _ in events]
    assert names[:6] == ["start", "delta", "delta", "delta", "delta", "done"]
    streamed = "".join(d["text"] for n, d in events if n == "delta")
    assert streamed == "Streamed text"  # display text comes from the deltas...
    assert assistant(client, cid)["content"] == streamed
    # ...and the numbers come only from the final provider usage, not from the 13 characters
    assert dict(events)["done"]["usage"]["input_tokens"] == 1000
    assert dict(events)["done"]["usage"]["output_tokens"] == 300
    assert "usage" not in [d for n, d in events if n == "delta"][0]


def test_done_event_reports_requested_and_served_model(luna_client):
    client = luna_client(provider_stream(["x"], raw_usage()))
    _, events = chat(client)
    done = dict(events)["done"]
    assert (done["requested_model"], done["served_model"]) == (MODEL, SNAPSHOT)


# ── Test 9: conversation totals are sums of recorded values ─────────────
def test_conversation_cost_is_the_sum_of_recorded_message_costs(luna_client):
    client = luna_client(
        provider_stream(["a"], raw_usage(1000, 300, 1300, cached=200), model=SNAPSHOT),
        provider_stream(["b"], raw_usage(2000, 500, 2500, cached=1500), model=SNAPSHOT),
        provider_stream(["c"], raw_usage(400, 100, 500, cached=0), model="unpriced-model"),  # unpriced
    )
    cid = client.post("/conversations").json()["id"]
    for text in ("one", "two", "three"):
        client.post(f"/conversations/{cid}/messages", json={"text": text})

    detail = client.get(f"/conversations/{cid}").json()
    replies = [m for m in detail["messages"] if m["role"] == "assistant"]
    assert [m["cost_usd"] is None for m in replies] == [False, False, True]

    expected_1 = 800 * 0.20 / 1e6 + 200 * 0.02 / 1e6 + 300 * 1.20 / 1e6
    expected_2 = 500 * 0.20 / 1e6 + 1500 * 0.02 / 1e6 + 500 * 1.20 / 1e6
    assert replies[0]["cost_usd"] == pytest.approx(expected_1)
    assert replies[1]["cost_usd"] == pytest.approx(expected_2)
    assert detail["total_cost_usd"] == pytest.approx(expected_1 + expected_2)  # null cost contributes nothing
    assert detail["total_cost_usd"] == pytest.approx(sum(m["cost_usd"] or 0 for m in replies))
    assert detail["unpriced_messages"] == 1
    assert detail["total_input_tokens"] == 1000 + 2000 + 400
    assert detail["total_cached_input_tokens"] == 200 + 1500
    assert detail["total_output_tokens"] == 300 + 500 + 100
    assert detail["total_tokens"] == 1300 + 2500 + 500

    # the live `done` event of the last reply carries the same running totals
    assert detail["total_tokens"] == detail["total_input_tokens"] + detail["total_output_tokens"]


def test_done_event_carries_running_conversation_totals(luna_client):
    client = luna_client(provider_stream(["a"], raw_usage(1000, 300, 1300)), provider_stream(["b"], raw_usage(100, 50, 150)))
    cid = client.post("/conversations").json()["id"]
    client.post(f"/conversations/{cid}/messages", json={"text": "one"})
    body = client.post(f"/conversations/{cid}/messages", json={"text": "two"}).text
    done = dict(parse_sse(body))["done"]
    assert (done["conversation_total_input_tokens"], done["conversation_total_output_tokens"],
            done["conversation_total_tokens"]) == (1100, 350, 1450)

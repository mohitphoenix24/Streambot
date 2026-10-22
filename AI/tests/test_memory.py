"""Rolling-summary memory: when to summarize, what history looks like afterwards, and the
background manager. No network: ScriptedLLM.summarize is scripted."""
import asyncio

import pytest

from app.chat.history import approx_tokens, build_history
from app.chat.memory import KEEP_RECENT, SUMMARIZE_ABOVE, MemoryManager, plan_summary
from app.config import Settings
from app.llm.pricing import PriceTable
from tests.helpers import ScriptedLLM, ok_script


def row(i, role, text, status="complete"):
    return {"id": f"m{i}", "role": role, "content": text, "status": status}


def convo(n_pairs, size=100):
    """u0 a0 u1 a1 ... each message `size` chars = size//4 + 4 approx tokens."""
    out = []
    for i in range(n_pairs):
        out += [row(2 * i, "user", f"u{i}".ljust(size, ".")), row(2 * i + 1, "assistant", f"a{i}".ljust(size, "."))]
    return out


PER_MSG = approx_tokens("x" * 100)  # 29


# ── plan_summary: when and what ──────────────────────────────────────────
def test_nothing_to_do_while_under_the_threshold():
    budget = 1000
    messages = convo(5)  # 10 x 29 = 290 tokens, well under 80% of 1000
    assert plan_summary(messages, None, budget) is None


def test_just_under_the_threshold_still_waits():
    msgs = convo(14)  # 28 x 29 = 812 tokens
    assert plan_summary(msgs, None, 1000) is not None
    assert plan_summary(convo(13), None, 1000) is None  # 26 x 29 = 754 <= 800


def test_summarizing_folds_the_oldest_and_keeps_about_the_recent_share_verbatim():
    messages = convo(20)  # 40 x 29 = 1160 > 800
    plan = plan_summary(messages, None, 1000)

    kept = messages[len(plan.messages):]
    assert plan.messages == messages[: len(plan.messages)]  # a prefix, oldest first
    assert plan.through_id == plan.messages[-1]["id"]
    assert sum(approx_tokens(m["content"]) for m in kept) <= KEEP_RECENT * 1000
    assert kept[0]["role"] == "user"  # the verbatim part never opens with an orphaned answer
    assert len(kept) >= 10  # roughly 40% of the budget stays verbatim, not just a couple of messages


def test_the_boundary_moves_in_big_steps_not_every_turn():
    messages = convo(20)
    first = plan_summary(messages, None, 1000)
    # Right after summarizing, adding a few messages must NOT trigger another summary.
    through = first.through_id
    grown = messages + [row(100 + i, "user" if i % 2 == 0 else "assistant", "y" * 100) for i in range(4)]
    assert plan_summary(grown, through, 1000) is None


def test_only_messages_after_the_existing_summary_count():
    messages = convo(20)
    plan = plan_summary(messages, "m29", 1000)  # a summary already covers m0..m29
    assert plan is None  # the 10 remaining messages (290 tokens) are under the threshold


def test_failed_and_empty_messages_are_never_summarized():
    messages = convo(20)
    messages[3] = row(3, "assistant", "boom", status="error")
    messages[5] = row(5, "assistant", "   ")
    plan = plan_summary(messages, None, 1000)
    assert all(m["status"] in ("complete", "cancelled") and m["content"].strip() for m in plan.messages)


def test_a_single_huge_message_is_folded_rather_than_kept_forever():
    messages = [row(0, "user", "q"), row(1, "assistant", "a"), row(2, "user", "x" * 8000)]
    plan = plan_summary(messages, None, 1000)
    assert plan is not None and plan.through_id == "m2"


# ── build_history with a summary ─────────────────────────────────────────
def test_history_opens_with_the_summary_then_only_newer_messages():
    messages = convo(6)
    history = build_history(messages, 10_000, summary="User is Mohit, building Streambot.", summary_through_id="m5")

    assert history.summarized is True
    first, *rest = history.messages
    assert first["role"] == "developer" and "User is Mohit" in first["content"]
    assert [m["content"][:2] for m in rest] == ["u3", "a3", "u4", "a4", "u5", "a5"]  # nothing at or before m5


def test_the_summary_counts_against_the_budget():
    messages = convo(6)
    plain = build_history(messages, 10_000, summary="s" * 400, summary_through_id="m5")
    assert plain.approx_tokens >= approx_tokens("s" * 400)
    tight = build_history(messages, approx_tokens("s" * 400) + 2 * PER_MSG, summary="s" * 400, summary_through_id="m5")
    assert tight.approx_tokens <= approx_tokens("s" * 400) + 2 * PER_MSG + 60
    assert tight.truncated is True and tight.messages[0]["role"] == "developer"


def test_without_a_summary_history_is_unchanged():
    history = build_history(convo(3), 10_000)
    assert history.summarized is False and history.messages[0]["role"] == "user"


def test_a_summary_pointing_at_an_unknown_message_is_ignored():
    history = build_history(convo(3), 10_000, summary="stale", summary_through_id="does-not-exist")
    assert history.summarized is False and len(history.messages) == 6


# ── MemoryManager ────────────────────────────────────────────────────────
@pytest.fixture
def setup(db, database_url):
    settings = Settings(openai_api_key="k", openai_model="m", database_url=database_url, history_budget_tokens=1000)
    llm = ScriptedLLM(ok_script("x"))
    manager = MemoryManager(db, llm, PriceTable(), settings)
    cid = db.create_conversation()["id"]
    return db, llm, manager, cid


def fill(db, cid, n_pairs, size=100):
    for i in range(n_pairs):
        db.add_message(cid, "user", f"u{i}".ljust(size, "."))
        db.add_message(cid, "assistant", f"a{i}".ljust(size, "."))


async def test_manager_stores_the_summary_and_records_its_cost(setup):
    db, llm, manager, cid = setup
    fill(db, cid, 20)

    assert await manager.run(cid) is True

    summary, through = db.get_summary(cid)
    assert summary == "SCRIPTED SUMMARY" and through
    (previous, folded), = llm.summary_requests
    assert previous is None and folded[0]["content"].startswith("u0")
    (call,) = db.list_llm_calls(cid)
    assert call["purpose"] == "summary" and call["input_tokens"] == 200 and call["served_model"] == "summary-snapshot"


async def test_second_pass_merges_into_the_existing_summary(setup):
    db, llm, manager, cid = setup
    fill(db, cid, 20)
    await manager.run(cid)
    fill(db, cid, 20)  # grows again past the threshold

    assert await manager.run(cid) is True
    assert llm.summary_requests[1][0] == "SCRIPTED SUMMARY"  # the old summary is passed in to be updated


async def test_nothing_happens_when_it_is_not_due(setup):
    db, llm, manager, cid = setup
    fill(db, cid, 2)
    assert await manager.run(cid) is False
    assert llm.summary_requests == [] and db.get_summary(cid) == (None, None)


async def test_a_failed_summary_keeps_the_old_one(setup):
    db, llm, manager, cid = setup
    fill(db, cid, 20)
    await manager.run(cid)
    old = db.get_summary(cid)
    fill(db, cid, 20)
    llm.summary_text = None  # the next call fails

    assert await manager.run(cid) is False
    assert db.get_summary(cid) == old


async def test_an_exception_is_swallowed_not_raised(setup):
    db, llm, manager, cid = setup
    fill(db, cid, 20)

    async def boom(*a):
        raise RuntimeError("provider exploded")

    llm.summarize = boom
    assert await manager.run(cid) is False  # memory must never break a chat


async def test_concurrent_runs_summarize_only_once(setup):
    db, llm, manager, cid = setup
    fill(db, cid, 20)
    results = await asyncio.gather(manager.run(cid), manager.run(cid))
    assert sorted(results) == [False, True] and len(llm.summary_requests) == 1


async def test_a_chat_deleted_mid_summary_is_handled(setup):
    db, llm, manager, cid = setup
    fill(db, cid, 20)
    real = llm.summarize

    async def delete_then_answer(prev, msgs):
        db.delete_conversation(cid)
        return await real(prev, msgs)

    llm.summarize = delete_then_answer
    assert await manager.run(cid) is False
    assert db.get_summary(cid) == (None, None)
    assert len(db.list_llm_calls()) == 1  # the money was spent, so the call is still recorded


async def test_disabled_memory_schedules_nothing(setup):
    db, llm, _, cid = setup
    off = MemoryManager(db, llm, PriceTable(), Settings(openai_api_key="k", openai_model="m", memory_summary_enabled=False))
    fill(db, cid, 20)
    off.schedule(cid)
    await asyncio.sleep(0.05)
    assert llm.summary_requests == []


def test_thresholds_are_what_the_docs_say():
    assert (SUMMARIZE_ABOVE, KEEP_RECENT) == (0.8, 0.4)

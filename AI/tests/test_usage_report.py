"""The Usage page's numbers. Rows are inserted with explicit timestamps so the day
bucketing, timezone handling and filtering are deterministic."""
from datetime import datetime, timezone

import pytest

from app.chat.usage_report import build_usage_report
from app.db import Database
from app.llm.pricing import ModelPrice, PriceTable

PRICES = PriceTable({"m": ModelPrice(1.0, 4.0, 0.1)})  # $/1M: input 1.0, cached 0.1, output 4.0
NOW = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    d.init_schema()
    return d


def reply(db, cid, when, *, inp=1000, cached=0, out=100, cost=0.002, searches=None, model="m", status="complete"):
    msg = db.add_message(cid, "assistant", "x", status=status, model=model)
    db.update_message(msg["id"], status=status, input_tokens=inp, cached_input_tokens=cached, output_tokens=out,
                      total_tokens=inp + out, cost_usd=cost, web_searches=searches)
    with db.connect() as conn:
        conn.execute("UPDATE messages SET created_at = ? WHERE id = ?", (when, msg["id"]))
    return msg["id"]


def side_call(db, cid, purpose, when, *, inp=200, cached=0, out=20, cost=0.0003, model="m"):
    call_id = db.add_llm_call(cid, purpose, model, input_tokens=inp, cached_input_tokens=cached,
                              output_tokens=out, total_tokens=inp + out, cost_usd=cost)
    with db.connect() as conn:
        conn.execute("UPDATE llm_calls SET created_at = ? WHERE id = ?", (when, call_id))


def report(db, **kw):
    return build_usage_report(db, PRICES, now=NOW, **{"days": 7, **kw})


def test_empty_database_gives_zeros_and_nulls_not_made_up_numbers(db):
    r = report(db)
    assert r["totals"]["cost_usd"] == 0 and r["totals"]["calls"] == 0
    assert r["cache"] == {"hit_rate": None, "cached_input_tokens": 0, "savings_usd": 0.0}
    assert r["top_conversations"] == [] and [p["calls"] for p in r["by_purpose"]] == [0, 0, 0]
    assert len(r["daily"]) == 7 and r["daily"][-1]["date"] == "2026-10-22" and r["daily"][0]["date"] == "2026-10-16"


def test_totals_combine_replies_titles_and_summaries(db):
    cid = db.create_conversation()["id"]
    reply(db, cid, "2026-10-22T09:00:00.000+00:00", cost=0.002, searches=2)
    side_call(db, cid, "title", "2026-10-22T09:00:05.000+00:00", cost=0.0003)
    side_call(db, cid, "summary", "2026-10-22T10:00:00.000+00:00", cost=0.0007)

    r = report(db)
    assert r["totals"]["cost_usd"] == pytest.approx(0.003)
    assert (r["totals"]["calls"], r["totals"]["replies"], r["totals"]["web_searches"]) == (3, 1, 2)
    by = {p["purpose"]: p for p in r["by_purpose"]}
    assert by["reply"]["cost_usd"] == pytest.approx(0.002) and by["title"]["calls"] == 1 and by["summary"]["cost_usd"] == pytest.approx(0.0007)
    assert r["totals"]["total_tokens"] == 1100 + 220 + 220


def test_cache_hit_rate_and_savings(db):
    cid = db.create_conversation()["id"]
    reply(db, cid, "2026-10-22T09:00:00.000+00:00", inp=1000, cached=0)
    reply(db, cid, "2026-10-22T09:05:00.000+00:00", inp=3000, cached=2000)

    r = report(db)
    assert r["cache"]["hit_rate"] == pytest.approx(2000 / 4000)
    # 2000 cached tokens x ($1.00 - $0.10) per 1M = $0.0018
    assert r["cache"]["savings_usd"] == pytest.approx(0.0018)


def test_savings_are_unknown_not_zero_when_a_model_has_no_price(db):
    cid = db.create_conversation()["id"]
    reply(db, cid, "2026-10-22T09:00:00.000+00:00", inp=1000, cached=500, model="unpriced-model", cost=None)
    r = report(db)
    assert r["cache"]["savings_usd"] is None and r["totals"]["unpriced_calls"] == 1
    assert r["totals"]["cost_usd"] == 0  # unknown cost is left out, not counted as $0 spent... and flagged


def test_days_outside_the_range_are_excluded(db):
    cid = db.create_conversation()["id"]
    reply(db, cid, "2026-10-22T09:00:00.000+00:00")
    reply(db, cid, "2026-10-01T09:00:00.000+00:00")  # 21 days ago
    assert report(db, days=7)["totals"]["replies"] == 1
    assert report(db, days=30)["totals"]["replies"] == 2


def test_days_follow_the_users_clock(db):
    cid = db.create_conversation()["id"]
    # 23:00 UTC on the 21st is 04:30 on the 22nd in India (UTC+5:30)
    reply(db, cid, "2026-10-21T23:00:00.000+00:00")
    utc = {d["date"]: d["replies"] for d in report(db)["daily"]}
    ist = {d["date"]: d["replies"] for d in report(db, utc_offset_minutes=330)["daily"]}
    assert utc["2026-10-21"] == 1 and utc["2026-10-22"] == 0
    assert ist["2026-10-22"] == 1 and ist["2026-10-21"] == 0


def test_daily_series_is_continuous_and_sums_to_the_total(db):
    cid = db.create_conversation()["id"]
    reply(db, cid, "2026-10-20T09:00:00.000+00:00", cost=0.001)
    reply(db, cid, "2026-10-22T09:00:00.000+00:00", cost=0.004)
    r = report(db)
    assert len(r["daily"]) == 7
    assert sum(d["cost_usd"] for d in r["daily"]) == pytest.approx(r["totals"]["cost_usd"])
    assert r["daily"][4]["cost_usd"] == pytest.approx(0.001) and r["daily"][5]["cost_usd"] == 0


def test_top_conversations_rank_by_cost_and_skip_deleted_chats(db):
    cheap, pricey, gone = (db.create_conversation(t)["id"] for t in ("Cheap", "Pricey", "Gone"))
    reply(db, cheap, "2026-10-22T09:00:00.000+00:00", cost=0.001)
    reply(db, pricey, "2026-10-22T09:01:00.000+00:00", cost=0.010)
    reply(db, pricey, "2026-10-22T09:02:00.000+00:00", cost=0.005)
    reply(db, gone, "2026-10-22T09:03:00.000+00:00", cost=0.100)
    db.delete_conversation(gone)

    r = report(db)
    assert [c["title"] for c in r["top_conversations"]] == ["Pricey", "Cheap"]
    assert r["top_conversations"][0]["replies"] == 2
    # Deleting a chat cascades to its reply rows, so its reply spend leaves the totals too
    # (only title/summary calls are kept, see test_spend_on_side_calls_survives_deleting_the_chat).
    assert r["totals"]["cost_usd"] == pytest.approx(0.016)


def test_spend_on_side_calls_survives_deleting_the_chat(db):
    cid = db.create_conversation()["id"]
    side_call(db, cid, "summary", "2026-10-22T09:00:00.000+00:00", cost=0.0007)
    db.delete_conversation(cid)
    assert report(db)["totals"]["cost_usd"] == pytest.approx(0.0007)


def test_by_model_is_sorted_by_cost(db):
    cid = db.create_conversation()["id"]
    reply(db, cid, "2026-10-22T09:00:00.000+00:00", model="m", cost=0.001)
    reply(db, cid, "2026-10-22T09:01:00.000+00:00", model="other", cost=0.009)
    assert [m["model"] for m in report(db)["by_model"]] == ["other", "m"]


def test_cancelled_replies_with_reported_usage_still_count(db):
    cid = db.create_conversation()["id"]
    reply(db, cid, "2026-10-22T09:00:00.000+00:00", status="cancelled", cost=0.0005)
    assert report(db)["totals"]["cost_usd"] == pytest.approx(0.0005)

"""The numbers behind the Usage page: spend, tokens, cache hit rate and what caching saved.

Everything is computed from rows the app already stored (reply rows carry their own
usage and cost; title and summary calls live in `llm_calls`), so nothing is estimated.
A cost of NULL means "no price configured": those calls are counted as `unpriced_calls`
and left out of the money totals instead of being counted as $0.

Days are the USER's days: the browser sends its UTC offset, so "today" and the daily bars
line up with the clock on the user's wall.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from app.db import Database
from app.llm.pricing import PriceTable

PURPOSES = ("reply", "title", "summary")


def _parse(created_at: str) -> datetime:
    return datetime.fromisoformat(created_at)


def build_usage_report(
    db: Database,
    prices: PriceTable,
    user_id: str,
    *,
    days: int = 30,
    utc_offset_minutes: int = 0,
    now: datetime | None = None,
) -> dict[str, Any]:
    offset = timedelta(minutes=utc_offset_minutes)
    now = now or datetime.now(timezone.utc)
    today = (now + offset).date()
    first_day = today - timedelta(days=days - 1)
    # The UTC instant at which `first_day` began on the user's clock.
    since = datetime.combine(first_day, time.min, tzinfo=timezone.utc) - offset

    reply_rows, call_rows = db.usage_rows(user_id, since)
    calls: list[dict[str, Any]] = []
    for r in reply_rows:
        calls.append({**r, "purpose": "reply"})
    for r in call_rows:
        calls.append({**r, "purpose": r["purpose"], "web_searches": None, "conversation_title": None})

    totals = defaultdict(float)
    by_purpose = {p: {"purpose": p, "calls": 0, "cost_usd": 0.0, "total_tokens": 0} for p in PURPOSES}
    by_model: dict[str, dict[str, Any]] = {}
    daily = {first_day + timedelta(days=i): {"cost_usd": 0.0, "total_tokens": 0, "replies": 0} for i in range(days)}
    conversations: dict[str, dict[str, Any]] = {}
    savings, savings_known, unpriced = 0.0, False, 0

    for c in calls:
        day = (_parse(c["created_at"]) + offset).date()
        if day not in daily:
            continue
        cost = c["cost_usd"]
        tokens = c["total_tokens"] or 0
        inp, cached = c["input_tokens"] or 0, c["cached_input_tokens"] or 0

        totals["calls"] += 1
        totals["input_tokens"] += inp
        totals["cached_input_tokens"] += cached
        totals["output_tokens"] += c["output_tokens"] or 0
        totals["total_tokens"] += tokens
        totals["web_searches"] += c.get("web_searches") or 0
        if cost is None:
            unpriced += 1
        else:
            totals["cost_usd"] += cost

        bp = by_purpose[c["purpose"]]
        bp["calls"] += 1
        bp["total_tokens"] += tokens
        bp["cost_usd"] += cost or 0.0

        model = c["model"] or c["served_model"] or "unknown"
        bm = by_model.setdefault(model, {"model": model, "calls": 0, "cost_usd": 0.0, "total_tokens": 0})
        bm["calls"] += 1
        bm["total_tokens"] += tokens
        bm["cost_usd"] += cost or 0.0

        d = daily[day]
        d["cost_usd"] += cost or 0.0
        d["total_tokens"] += tokens
        d["replies"] += 1 if c["purpose"] == "reply" else 0

        saved = prices.cache_savings_for(c["model"], c["served_model"], c["input_tokens"], c["cached_input_tokens"])
        if saved is not None:
            savings += saved
            savings_known = True

        if c["purpose"] == "reply" and c["conversation_title"] is not None:  # skip deleted chats
            top = conversations.setdefault(
                c["conversation_id"],
                {"id": c["conversation_id"], "title": c["conversation_title"], "cost_usd": 0.0, "replies": 0},
            )
            top["cost_usd"] += cost or 0.0
            top["replies"] += 1

    input_total, cached_total = int(totals["input_tokens"]), int(totals["cached_input_tokens"])
    return {
        "days": days,
        "from": first_day.isoformat(),
        "to": today.isoformat(),
        "totals": {
            "cost_usd": totals["cost_usd"],
            "unpriced_calls": unpriced,  # calls with usage but no price: the cost is a lower bound
            "calls": int(totals["calls"]),
            "replies": by_purpose["reply"]["calls"],
            "input_tokens": input_total,
            "cached_input_tokens": cached_total,
            "output_tokens": int(totals["output_tokens"]),
            "total_tokens": int(totals["total_tokens"]),
            "web_searches": int(totals["web_searches"]),
        },
        "cache": {
            "hit_rate": cached_total / input_total if input_total else None,
            "cached_input_tokens": cached_total,
            # 0 if nothing was cached; None only when cached tokens exist but no price says what they saved.
            "savings_usd": savings if savings_known else (0.0 if cached_total == 0 else None),
        },
        "by_purpose": list(by_purpose.values()),
        "by_model": sorted(by_model.values(), key=lambda m: (-m["cost_usd"], m["model"])),
        "daily": [{"date": d.isoformat(), **v} for d, v in sorted(daily.items())],
        "top_conversations": sorted(
            (c for c in conversations.values() if c["cost_usd"] > 0),
            key=lambda c: -c["cost_usd"],
        )[:5],
    }

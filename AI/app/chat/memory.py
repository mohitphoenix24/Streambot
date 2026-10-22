"""Rolling-summary memory: keep long chats useful without sending every message.

The model is stateless, so "memory" means re-sending the conversation. That does not
scale: a chat longer than HISTORY_BUDGET_TOKENS used to lose its oldest messages
outright. Now the oldest messages are folded into a short summary instead:

    old messages --summarize--> [summary]  +  recent messages (verbatim)  -->  LLM

When (hysteresis): summarizing on every turn would change the start of the prompt
every time and defeat prompt caching. So we wait until the not-yet-summarized messages
pass SUMMARIZE_ABOVE of the budget, then fold the oldest ones until only KEEP_RECENT
of it remains verbatim. The summary boundary moves in big, rare steps, and between
steps the start of the prompt is byte-for-byte identical.

How: after a reply finishes, a background task (never on the reply's critical path)
asks the model to merge the new old messages into the existing summary. If that call
fails, the old summary stays and the plain truncation in history.py still protects the
budget. This module has no HTTP and no OpenAI types: plan_summary() is a pure function.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from app.chat.history import approx_tokens, eligible_messages
from app.config import Settings
from app.db import Database
from app.llm.accounting import record_llm_call
from app.llm.client import LLM
from app.llm.pricing import PriceTable

log = logging.getLogger("chat.memory")

SUMMARIZE_ABOVE = 0.8  # start summarizing when unsummarized messages exceed this share of the budget
KEEP_RECENT = 0.4  # ...and fold the oldest ones until about this share remains verbatim


@dataclass(frozen=True)
class SummaryPlan:
    messages: list[dict[str, Any]]  # rows to fold into the summary, oldest first
    through_id: str  # id of the last of them


def plan_summary(
    messages: list[dict[str, Any]], through_id: str | None, budget_tokens: int
) -> SummaryPlan | None:
    """Decide what (if anything) to fold into the summary now. Pure: rows in, plan out."""
    eligible = eligible_messages(messages)
    ids = [m["id"] for m in eligible]
    pending = eligible[ids.index(through_id) + 1:] if through_id in ids else eligible

    if sum(approx_tokens(m["content"]) for m in pending) <= SUMMARIZE_ABOVE * budget_tokens:
        return None

    # Walk back from the newest message, keeping what fits in the "recent" share.
    keep_budget = KEEP_RECENT * budget_tokens
    recent_start, used = len(pending), 0
    for i in range(len(pending) - 1, -1, -1):
        cost = approx_tokens(pending[i]["content"])
        if used + cost > keep_budget:
            break
        used += cost
        recent_start = i
    # The verbatim part must open with a user turn, never an orphaned assistant answer.
    while recent_start < len(pending) and pending[recent_start]["role"] != "user":
        recent_start += 1

    to_fold = pending[:recent_start]
    if not to_fold:
        return None
    return SummaryPlan(to_fold, to_fold[-1]["id"])


class MemoryManager:
    """Runs the summarizing, one conversation at a time, off the request path."""

    def __init__(self, db: Database, llm: LLM, prices: PriceTable, settings: Settings) -> None:
        self._db, self._llm, self._prices, self._settings = db, llm, prices, settings
        self._locks: dict[str, asyncio.Lock] = {}
        self._tasks: set[asyncio.Task] = set()  # keep references so tasks are not garbage collected

    def schedule(self, conversation_id: str) -> None:
        """Fire and forget: check (and if needed update) this conversation's summary."""
        if not self._settings.memory_summary_enabled:
            return
        task = asyncio.create_task(self.run(conversation_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def run(self, conversation_id: str) -> bool:
        """Update the summary if it is due. Returns True if a new summary was stored.
        Never raises: memory is a best-effort extra, not something that may break a chat."""
        lock = self._locks.setdefault(conversation_id, asyncio.Lock())
        async with lock:  # two replies finishing close together must not summarize twice
            try:
                return await self._update(conversation_id)
            except Exception:
                log.warning("summary_update_failed conversation_id=%s", conversation_id, exc_info=True)
                return False

    async def _update(self, conversation_id: str) -> bool:
        db = self._db
        owner = db.conversation_owner(conversation_id)  # the server's own lookup, not a request's
        if owner is None:
            return False
        summary, through_id = db.get_summary(conversation_id)
        plan = plan_summary(db.list_messages(conversation_id), through_id, self._settings.history_budget_tokens)
        if plan is None:
            return False

        folded = [{"role": m["role"], "content": m["content"]} for m in plan.messages]
        result = await self._llm.summarize(summary, folded)
        if result is None:
            return False  # keep the old summary; plain truncation still protects the budget

        record_llm_call(db, self._prices, owner, conversation_id, "summary", result)
        if db.conversation_owner(conversation_id) is None:
            return False  # the chat was deleted while we were summarizing
        db.set_summary(conversation_id, result.text, plan.through_id)
        log.info(
            "summary_updated conversation_id=%s folded_messages=%d summary_chars=%d",
            conversation_id, len(folded), len(result.text),
        )
        return True

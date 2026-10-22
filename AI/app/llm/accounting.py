"""Record what a side call (title, summary) cost, next to the replies' own accounting.

Replies keep their usage and cost on their message row. Titles and summaries belong to
no message, so they go to the `llm_calls` table; without this their cost would be
invisible.
"""
from __future__ import annotations

from app.db import Database
from app.llm.client import Generated
from app.llm.pricing import PriceTable
from app.llm.usage import Usage


def usage_columns(usage: Usage | None) -> dict[str, int | None]:
    """DB columns for a usage record. No usage -> all NULL (never invented)."""
    return {
        "input_tokens": usage.input_tokens if usage else None,
        "cached_input_tokens": usage.cached_input_tokens if usage else None,
        "output_tokens": usage.output_tokens if usage else None,
        "reasoning_tokens": usage.reasoning_tokens if usage else None,
        "total_tokens": usage.total_tokens if usage else None,
    }


def record_llm_call(
    db: Database, prices: PriceTable, user_id: str, conversation_id: str, purpose: str, generated: Generated
) -> None:
    """Store one title/summary call. Usage is the provider's report; cost is None when the
    model has no price (never guessed), exactly as for replies."""
    cost = prices.cost(generated.model, generated.served_model, generated.usage)
    db.add_llm_call(
        user_id, conversation_id, purpose, generated.model,
        served_model=generated.served_model,
        cost_usd=cost.total_cost if cost else None,
        **usage_columns(generated.usage),
    )

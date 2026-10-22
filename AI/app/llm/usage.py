"""Turn the provider's `usage` object into our own Usage record.

    OpenAI API -> actual usage -> [this parser] -> pricing -> database -> UI

These numbers are REPORTED by OpenAI after it generated the answer. They are
never estimated from text length. If OpenAI sends no usage (a cancelled stream, an
error), the result is None, and downstream code stores NULL / shows "Usage
unavailable" instead of inventing numbers.

Fields (all token counts):
  input_tokens         everything we sent (instructions + history + new message),
                       INCLUDING the cached part
  cached_input_tokens  the part of input_tokens served from OpenAI's prompt cache
                       (billed at a lower rate). A SUBSET of input_tokens.
  output_tokens        everything the model generated, INCLUDING reasoning tokens
  reasoning_tokens     the part of output_tokens the model spent "thinking" before
                       the visible answer. A SUBSET of output_tokens, kept only for
                       observability; it must NOT be added to the cost again.
  total_tokens         input_tokens + output_tokens

cached_input_tokens / reasoning_tokens are None when the provider didn't report
them (older models, optional fields): "unknown", which is not the same as 0.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("chat.usage")


@dataclass(frozen=True)
class Usage:
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cached_input_tokens: int | None = None
    reasoning_tokens: int | None = None

    def as_dict(self) -> dict[str, int | None]:
        return {
            "input_tokens": self.input_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "total_tokens": self.total_tokens,
        }


def _get(obj: Any, name: str) -> Any:
    """Read a field from an SDK object OR a plain dict; missing -> None."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _count(value: Any) -> int | None:
    # bool is an int subclass; a real token count is a non-negative int.
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def parse_usage(raw: Any) -> Usage | None:
    """Parse the provider's usage object. Returns None if it has no usable counts."""
    if raw is None:
        return None
    input_tokens = _count(_get(raw, "input_tokens"))
    output_tokens = _count(_get(raw, "output_tokens"))
    if input_tokens is None or output_tokens is None:
        log.warning("usage_unparseable: provider usage lacks input_tokens/output_tokens")
        return None

    reported_total = _count(_get(raw, "total_tokens"))
    return Usage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        # Adding two REPORTED numbers is arithmetic, not estimation.
        total_tokens=reported_total if reported_total is not None else input_tokens + output_tokens,
        cached_input_tokens=_count(_get(_get(raw, "input_tokens_details"), "cached_tokens")),
        reasoning_tokens=_count(_get(_get(raw, "output_tokens_details"), "reasoning_tokens")),
    )

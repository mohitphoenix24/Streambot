"""Turn stored messages into the history we send to the model.

LLMs are STATELESS. The model does not remember your previous question; every
request must contain the whole conversation it should know about. "Memory" in a
chat product is just us re-sending earlier turns each time:

    request 1: [user: My name is Mohit.]
    request 2: [user: My name is Mohit., assistant: Nice to meet you.,
                user: What is my name?]          -> "Your name is Mohit."

But a conversation grows forever and a model's context is finite (and every
input token costs money and adds latency). So we send only as much recent history
as fits a BUDGET.

Context window vs history budget (they are different numbers!):

    context window       = the MODEL's hard limit on tokens in one request
                           (input + output together).
    HISTORY_BUDGET_TOKENS = OUR smaller limit for just the past-messages part.

We intentionally spend less than the window on history, because the window must
also hold everything else:

    instructions (system prompt)  +  history  +  new user input  +  model output
                                  ^^^^^^^^^^^
                          this is the only part the budget caps

so:  window  >=  instructions + HISTORY_BUDGET + MAX_OUTPUT_TOKENS (+ safety margin)

This module knows nothing about HTTP, SQLite or OpenAI: messages in, messages
out, which makes it trivial to unit test.

UPGRADE PATH (documented, deliberately NOT implemented): a running summary.
Instead of silently forgetting what falls outside the budget, keep a short
summary of the dropped messages and send it as the first item:

    old messages --summarize--> [summary]  +  recent messages  -->  LLM

That preserves long-range facts ("the user's name is Mohit, they're building a
FastAPI app") at a fraction of the tokens. It costs an extra model call to
produce and refresh the summary, and the summary itself can lose or distort
details, which is why it is a second step after plain truncation works.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

HistoryMessage = dict[str, str]


def approx_tokens(text: str) -> int:
    """CHEAP ESTIMATE of how many tokens `text` is. Not exact.

    Rule of thumb: ~4 characters per token for English; +4 for per-message
    overhead (role markers, separators). This is NOT the model's tokenizer:
    other languages, code, emoji and numbers can tokenize quite differently, so
    the real count can be noticeably higher or lower.

    That is acceptable here because the budget only needs to be roughly right,
    and it leaves a safety margin below the context window. The real token
    counts (from the provider's `usage`) are what we store and bill. Upgrade
    path: swap in the model's actual tokenizer (e.g. tiktoken) behind this same
    function signature.
    """
    return len(text) // 4 + 4


@dataclass(frozen=True)
class History:
    messages: list[HistoryMessage]
    truncated: bool  # True if older eligible messages were dropped to fit the budget
    approx_tokens: int  # estimated size of `messages`


def _eligible(message: dict[str, Any]) -> bool:
    # 'error'     : a failed reply. Feeding a broken/partial answer back to the
    #               model as if it said it would confuse it.
    # 'streaming' : still being written (or orphaned by a crash).
    # empty text  : carries no information.
    # 'complete' and 'cancelled' are kept: the user SAW that text (a stopped
    # answer is still part of what was said).
    return message["status"] in ("complete", "cancelled") and bool(message["content"].strip())


def build_history(messages: list[dict[str, Any]], budget_tokens: int) -> History:
    """`messages` are DB rows in chronological order; the newest is the user's
    current question. Returns the largest recent suffix that fits the budget."""
    eligible = [m for m in messages if _eligible(m)]

    selected: list[dict[str, Any]] = []
    used = 0
    truncated = False

    # Walk from the NEWEST message backwards: recent turns matter most, so when
    # something must be dropped it is the oldest.
    for message in reversed(eligible):
        cost = approx_tokens(message["content"])
        if used + cost > budget_tokens:
            truncated = True
            break  # stop at the first message that doesn't fit; never skip it and
            #        keep older ones, which would leave a hole in the middle.
        selected.append(message)
        used += cost

    # Edge case: the newest message alone exceeds the budget. Sending NOTHING would
    # make the model answer a question it never saw, so the current user message is
    # always kept (its size is capped separately by MAX_MESSAGE_CHARS).
    if not selected and eligible and eligible[-1]["role"] == "user":
        selected.append(eligible[-1])
        used = approx_tokens(eligible[-1]["content"])
        truncated = len(eligible) > 1

    selected.reverse()  # back to chronological order

    # The conversation sent to the model must open with a USER turn. If the budget
    # cut landed between a user message and its reply, we'd start with an orphaned
    # assistant answer to a question the model can't see; drop it.
    while selected and selected[0]["role"] != "user":
        used -= approx_tokens(selected[0]["content"])
        selected.pop(0)
        truncated = True

    return History(
        messages=[{"role": m["role"], "content": m["content"]} for m in selected],
        truncated=truncated,
        approx_tokens=used,
    )

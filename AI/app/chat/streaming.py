"""The streaming pipeline: model deltas -> database -> SSE events.

The whole request, end to end (this is the whiteboard diagram):

    Browser  --POST /conversations/{id}/messages-->  FastAPI
        1. save the user's message                         (SQLite)
        2. create the assistant row: content="", status=streaming  (SQLite)
        3. read history from the database                  (SQLite)
        4. ask the model, streaming                        (OpenAI)
        5. for every delta from OpenAI:
               append to a buffer, forward as an SSE `delta` event  --> Browser
        6. finish: write final content/status/usage/cost   (SQLite)
           and send `done` (or `error`)                    --> Browser
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import aclosing
from dataclasses import dataclass
from typing import Any, AsyncIterator

from fastapi import Request

from app.config import Settings
from app.db import Database
from app.chat.history import build_history
from app.llm.client import (
    LLM, UNAVAILABLE, Completed, HistoryMessage, LLMError, TextDelta, ToolCallFinished,
    ToolCallStarted, ToolExecutor, Usage,
)
from app.llm.accounting import record_llm_call, usage_columns
from app.llm.pricing import CostBreakdown, PriceTable
from app.chat.sse import sse

log = logging.getLogger("chat.stream")


@dataclass
class Turn:
    """Everything prepared BEFORE the stream starts."""

    user_message: dict[str, Any]
    assistant_message: dict[str, Any]
    history: list[HistoryMessage]
    history_truncated: bool
    is_first_message: bool
    history_summarized: bool = False  # history opens with a summary of older messages


def begin_turn(db: Database, llm: LLM, settings: Settings, conversation_id: str, text: str) -> Turn:
    is_first = not db.list_messages(conversation_id)

    # 1. Persist what the user said. If anything below fails, their message is safe.
    user_msg = db.add_message(conversation_id, "user", text)

    # 2. Create the assistant row BEFORE calling the model, empty and marked
    #    'streaming'. This is deliberate. If the process dies halfway through the
    #    stream:
    #      * WITHOUT this row: the reply simply never existed. The user sees their
    #        question followed by nothing, and we cannot tell what happened.
    #      * WITH this row: there is a record with status + whatever partial text
    #        was flushed. Startup recovery marks it 'error' and the UI shows the
    #        partial answer instead of losing it.
    assistant_msg = db.add_message(
        conversation_id, "assistant", content="", status="streaming", model=llm.model
    )

    if is_first:
        db.set_title(conversation_id, provisional_title(text))

    # 3. Build the history FROM THE DATABASE (not from anything the browser
    #    claims), so the server decides what the model sees:
    #        database -> messages -> history builder -> OpenAI
    #    The empty 'streaming' placeholder created in step 2 is skipped by the
    #    builder, and so are failed replies. The newest message is the question
    #    we just saved.
    summary, summary_through_id = db.get_summary(conversation_id)
    history = build_history(
        db.list_messages(conversation_id), settings.history_budget_tokens, summary, summary_through_id
    )

    return Turn(user_msg, assistant_msg, history.messages, history.truncated, is_first, history.summarized)


def web_columns(sources: list[dict[str, Any]] | None, web_searches: int) -> dict[str, Any]:
    """DB columns for what web search contributed. No sources / searches -> NULL."""
    return {
        "sources": json.dumps(sources) if sources else None,
        "web_searches": web_searches or None,
    }


def finalize_complete(
    db: Database,
    prices: PriceTable,
    *,
    assistant_id: str,
    model: str,
    served_model: str | None,
    content: str,
    stop_reason: str,
    usage: Usage | None,
    ttft_ms: float | None,
    latency_ms: float,
    sources: list[dict[str, Any]] | None = None,
    web_searches: int = 0,
) -> CostBreakdown | None:
    """Write the final state of a successful reply. Returns the cost breakdown.

    Three separate steps, each in its own module:
        usage     provider-REPORTED counts (already parsed by usage.parse_usage)
        pricing   usage x prices.toml -> CostBreakdown (None if unpriced/unknown)
        db        persist usage + total cost

    The streamed `content` is only the text to display. Token counts and cost never
    come from its length.
    """
    cost = prices.cost(model, served_model, usage)
    db.update_message(
        assistant_id,
        content=content,
        status="complete",
        stop_reason=stop_reason,
        served_model=served_model,
        cost_usd=cost.total_cost if cost else None,
        ttft_ms=ttft_ms,
        latency_ms=latency_ms,
        **web_columns(sources, web_searches),
        **usage_columns(usage),
    )
    return cost


def provisional_title(text: str, limit: int = 40) -> str:
    """Instant, free fallback title: the first words of the user's message.

    Used right away (so the sidebar never shows "New chat" for a used
    conversation) and kept if the real title call fails."""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def _elapsed_ms(since: float) -> float:
    return round((time.perf_counter() - since) * 1000, 1)


async def stream_reply(
    *,
    request: Request,
    db: Database,
    llm: LLM,
    settings: Settings,
    prices: PriceTable,
    turn: Turn,
    started_at: float,
    tools: ToolExecutor | None = None,
    memory: Any = None,  # a MemoryManager: updates the rolling summary after a good reply
) -> AsyncIterator[str]:
    """Async generator yielding SSE-formatted strings. FastAPI's StreamingResponse
    sends each yielded string to the browser as soon as it is produced.

    Every way this function can end is funnelled into ONE `finally` block that
    writes the terminal state, so no path can forget to update the database:

        complete   the model finished              -> `done` event
        error      the model/network failed         -> `error` event, partial text kept
        cancelled  the browser went away (Stop)     -> partial text kept, no event
    """
    assistant_id = turn.assistant_message["id"]
    conversation_id = turn.assistant_message["conversation_id"]
    parts: list[str] = []
    ttft_ms: float | None = None
    completed: Completed | None = None
    public_error: str | None = None
    sources: list[dict[str, Any]] = []  # web sources cited so far, numbered [1].. across searches
    web_searches = 0

    # Assume 'cancelled' unless we positively reach success or a handled error.
    # Anything else that unwinds this generator (client disconnect, task
    # cancellation, the response being closed) IS a cancellation.
    outcome = "cancelled"
    last_flush = time.monotonic()

    yield sse("start", {
        "user_message_id": turn.user_message["id"],
        "assistant_message_id": assistant_id,
        "history_messages": len(turn.history),
        "history_truncated": turn.history_truncated,
        "history_summarized": turn.history_summarized,
    })

    try:
        # aclosing() guarantees the upstream stream is closed when we leave this
        # block early, which closes the HTTP connection to OpenAI so it stops
        # generating (and billing) tokens nobody is reading.
        upstream = llm.stream(turn.history, tools, cache_key=conversation_id)
        async with aclosing(upstream) as events:
            async for event in events:
                # CANCELLATION, signal 1: ask the server whether the browser is
                # still there. When the user clicks Stop, AbortController.abort()
                # closes the TCP connection; the ASGI server turns that into an
                # `http.disconnect` message, which is_disconnected() reports.
                #
                # Closing the tab is not enough on its own: if we never check, the
                # model request keeps running, you keep paying for tokens, and
                # nobody reads them. Cancellation must reach the provider call.
                if await request.is_disconnected():
                    break  # outcome stays 'cancelled'; leaving the block closes the stream

                if isinstance(event, TextDelta):
                    if ttft_ms is None:
                        # Time To First Token: request arrival -> first delta. The
                        # number users actually FEEL as "how fast does it respond".
                        ttft_ms = _elapsed_ms(started_at)
                    parts.append(event.text)

                    # Partial persistence: checkpoint the text so far every
                    # PARTIAL_FLUSH_INTERVAL_S. We do NOT write on every delta (a
                    # DB write per token is wasteful), but we also never hold the
                    # whole answer only in memory: a hard crash (kill -9, power
                    # loss) loses at most one interval of text, and startup
                    # recovery turns the orphaned 'streaming' row into 'error'.
                    now = time.monotonic()
                    if now - last_flush >= settings.partial_flush_interval_s:
                        db.update_message(assistant_id, content="".join(parts))
                        last_flush = now

                    yield sse("delta", {"text": event.text})
                elif isinstance(event, ToolCallStarted):
                    yield sse("tool_call", {"id": event.call_id, "name": event.name, "query": event.query})
                elif isinstance(event, ToolCallFinished):
                    if not event.error:
                        web_searches += 1
                    sources.extend(event.sources)
                    # Checkpoint right away so a crash keeps what the answer will cite.
                    db.update_message(assistant_id, **web_columns(sources, web_searches))
                    yield sse("tool_result", {
                        "id": event.call_id, "name": event.name,
                        "sources": list(event.sources), "error": event.error,
                    })
                elif isinstance(event, Completed):
                    completed = event
            else:
                # The loop ran to the end (no break): a healthy stream MUST have
                # ended with a Completed event.
                if completed is None:
                    raise LLMError("The model stream ended unexpectedly.")
                outcome = "complete"
    except asyncio.CancelledError:
        # CANCELLATION, signal 2: the server cancelled this task because the
        # client disconnected while we were waiting for the next delta (a poll of
        # is_disconnected() can't run while we are blocked waiting on OpenAI).
        # outcome is already 'cancelled'; re-raise so cancellation proceeds.
        raise
    except Exception as exc:
        outcome = "error"
        # Technical detail goes to the SERVER log only. The browser gets a short
        # safe message: raw exception text can leak internals (URLs, request ids,
        # config) and means nothing to an end user.
        log.error(
            "reply_failed conversation_id=%s message_id=%s model=%s error_type=%s partial_chars=%d",
            conversation_id, assistant_id, llm.model, type(exc.__cause__ or exc).__name__,
            sum(map(len, parts)), exc_info=True,
        )
        public_error = exc.public_message if isinstance(exc, LLMError) else UNAVAILABLE
    finally:
        # Runs for success, handled error, disconnect AND cancellation. These are
        # synchronous SQLite writes with no `await`, so even a task that is being
        # cancelled cannot be interrupted halfway through saving.
        latency_ms = _elapsed_ms(started_at)
        content = "".join(parts)
        cost: CostBreakdown | None = None
        if outcome == "complete" and completed is not None:
            cost = finalize_complete(
                db, prices, assistant_id=assistant_id, model=llm.model,
                served_model=completed.served_model, content=content,
                stop_reason=completed.stop_reason, usage=completed.usage,
                ttft_ms=ttft_ms, latency_ms=latency_ms, sources=sources, web_searches=web_searches,
            )
        else:
            # error / cancelled: keep whatever text arrived. We never replace it with blank.
            # Usage is stored ONLY if the provider actually reported it (i.e. its
            # final event arrived); a stream that died earlier reported nothing, so
            # those columns stay NULL ("Usage unavailable"). Tokens may still have
            # been billed by OpenAI; we only store what it told us.
            reported = completed.usage if completed else None
            served = completed.served_model if completed else None
            partial_cost = prices.cost(llm.model, served, reported)
            db.update_message(
                assistant_id, content=content, status=outcome, stop_reason=outcome,
                served_model=served,
                cost_usd=partial_cost.total_cost if partial_cost else None,
                ttft_ms=ttft_ms, latency_ms=latency_ms, **web_columns(sources, web_searches),
                **usage_columns(reported),
            )
        db.touch_conversation(conversation_id)
        usage = completed.usage if completed else None
        log.info(
            "reply_finished conversation_id=%s message_id=%s model=%s served_model=%s status=%s "
            "stop_reason=%s web_searches=%d input_tokens=%s cached_input_tokens=%s output_tokens=%s "
            "reasoning_tokens=%s cost_usd=%s ttft_ms=%s latency_ms=%s output_chars=%d",
            conversation_id, assistant_id, llm.model, completed.served_model if completed else None,
            outcome, completed.stop_reason if completed else outcome, web_searches,
            usage.input_tokens if usage else None, usage.cached_input_tokens if usage else None,
            usage.output_tokens if usage else None, usage.reasoning_tokens if usage else None,
            cost.total_cost if cost else None, ttft_ms, latency_ms, len(content),
        )

    if outcome == "cancelled":
        return  # nobody is listening any more
    if outcome == "error":
        yield sse("error", {"message": public_error, "partial": bool(parts)})
        return

    assert completed is not None
    usage = completed.usage
    totals = db.conversation_totals(conversation_id)
    if memory is not None:
        # Before the first yield below: a client that hangs up right after `done` must not
        # stop the summary from being kept up to date.
        memory.schedule(conversation_id)
    yield sse("done", {
        "stop_reason": completed.stop_reason,
        # Provider-reported counts, or null when OpenAI sent none. Never estimated.
        "usage": usage.as_dict() if usage else None,
        # Cost components (USD) calculated from that usage, or null if unpriced.
        "cost": cost.as_dict() if cost else None,
        "requested_model": llm.model,
        "served_model": completed.served_model,
        "ttft_ms": ttft_ms,
        "latency_ms": latency_ms,
        "web_searches": web_searches,
        "conversation_total_cost_usd": totals["total_cost_usd"],
        "conversation_total_tokens": totals["total_tokens"],
        "conversation_total_input_tokens": totals["total_input_tokens"],
        "conversation_total_output_tokens": totals["total_output_tokens"],
        "unpriced_messages": totals["unpriced_messages"],
    })

    # Conversation title: a SEPARATE, small, non-streaming model call, made only
    # after the real answer has been delivered, so it can never delay or break it.
    #
    # Model routing: naming a chat is easy, answering is hard. Setting
    # OPENAI_TITLE_MODEL to a cheaper model for this call and keeping the stronger
    # model for answers is a common way to cut cost without hurting quality where
    # users can tell. (Its tokens belong to no message, so they are recorded in the
    # llm_calls table instead of the conversation cost shown above.)
    # Trigger: this is the conversation's FIRST COMPLETED reply (not merely the first
    # message). If the first answer was stopped or failed, the next successful one
    # still earns the conversation a proper title.
    rows = db.list_messages(conversation_id)
    if sum(1 for r in rows if r["role"] == "assistant" and r["status"] == "complete") == 1:
        first_user_text = next(r["content"] for r in rows if r["role"] == "user")
        try:
            generated = await llm.generate_title(first_user_text)
        except Exception:
            log.warning("title_generation_failed conversation_id=%s", conversation_id, exc_info=True)
            generated = None
        if generated:
            record_llm_call(db, prices, conversation_id, "title", generated)  # its cost is tracked too
            db.set_title(conversation_id, generated.text)
            yield sse("title", {"title": generated.text})

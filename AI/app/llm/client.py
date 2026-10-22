"""The LLM integration layer: the ONLY module that knows about OpenAI.

Everything above this file (history, database, SSE, routes) speaks our own tiny
vocabulary:

    history message : {"role": "user" | "assistant", "content": "..."}
    events          : TextDelta(text), Completed(stop_reason, usage)
    failures        : LLMError(public_message, retryable)

Everything below it is the OpenAI SDK. Keeping that wall means:
  * swapping providers (or SDK versions - this SDK just went through a major
    version bump) touches one file;
  * the rest of the app is testable against a scripted stub that needs no network;
  * no OpenAI exception type or response shape leaks into the HTTP layer.
"""
from __future__ import annotations

import asyncio
import logging
import random
from contextlib import aclosing
from dataclasses import dataclass
from typing import Any, AsyncIterator, Awaitable, Callable, Protocol

import anyio
import httpx2
import openai
from openai import AsyncOpenAI

from app.config import Settings
from app.llm.usage import Usage, parse_usage  # Usage is re-exported from here for the rest of the app

log = logging.getLogger("chat.llm")

# Our app-level message. Plain dicts on purpose: easy to store, print, test.
HistoryMessage = dict[str, str]


# ── Our own vocabulary ───────────────────────────────────────────────────
@dataclass(frozen=True)
class Completion:
    """Result of a normal (non-streaming) request."""

    text: str
    stop_reason: str
    usage: Usage | None
    # The model id OpenAI says actually served this response (may be a dated
    # snapshot of the alias we asked for). Provider-reported, unlike `model`
    # which is just what WE requested.
    served_model: str | None = None


@dataclass(frozen=True)
class TextDelta:
    """A small piece of generated text (usually a few characters / one token or so)."""

    text: str


@dataclass(frozen=True)
class Completed:
    """Terminal event of a healthy stream: how it ended, plus exact token usage."""

    stop_reason: str
    usage: Usage | None
    served_model: str | None = None  # provider-reported; see Completion.served_model


LLMEvent = TextDelta | Completed


class LLMError(Exception):
    """A provider failure, already translated into something safe to show.

    public_message : safe for the browser. Never contains keys, URLs, request
                     ids or raw provider text.
    retryable      : is this the kind of failure that might succeed on a retry
                     (network blip, 5xx, rate limit)? Whether a retry is actually
                     SAFE also depends on whether tokens were already sent to the
                     user - that decision lives in the retry loop, not here.
    The original exception is attached as __cause__ for server-side logs.
    """

    def __init__(
        self,
        public_message: str,
        *,
        retryable: bool = False,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(public_message)
        self.public_message = public_message
        self.retryable = retryable
        self.retry_after = retry_after


class LLM(Protocol):
    """What the rest of the app needs from a model provider."""

    model: str

    async def complete(self, history: list[HistoryMessage]) -> Completion: ...

    def stream(self, history: list[HistoryMessage]) -> AsyncIterator[LLMEvent]: ...

    async def generate_title(self, first_user_text: str) -> str | None: ...


# ── Error translation ────────────────────────────────────────────────────
UNAVAILABLE = "The model is unavailable right now."


def translate_openai_error(exc: Exception) -> LLMError:
    """Map an SDK / network exception to (safe message, retryable?).

    Retry only TRANSIENT failures - ones where the same request could plausibly
    succeed moments later:
      * connection errors and timeouts        -> yes
      * 5xx from the provider, 408, 409       -> yes
      * 429 rate limit                        -> yes (short, bounded wait)
      * 429 with code insufficient_quota      -> NO: out of money is not transient
      * 400/401/403/404 (our bug, bad key, bad model name) -> NO: retrying an
        identical bad request just fails again and wastes time.
    """
    if isinstance(exc, openai.APIConnectionError):  # includes APITimeoutError
        return LLMError("Could not reach the model service.", retryable=True)

    if isinstance(exc, httpx2.TransportError):
        # Network died mid-stream: the SDK doesn't always wrap these.
        return LLMError("The connection to the model was interrupted.", retryable=True)

    if isinstance(exc, openai.APIStatusError):
        status = exc.status_code
        if isinstance(exc, openai.RateLimitError):
            if exc.code == "insufficient_quota":
                return LLMError("The model service has run out of quota. Please contact the administrator.")
            return LLMError(
                "The model is rate-limited right now. Please try again shortly.",
                retryable=True,
                retry_after=_retry_after_seconds(exc),
            )
        if status >= 500 or status in (408, 409):
            return LLMError(UNAVAILABLE, retryable=True, retry_after=_retry_after_seconds(exc))
        if isinstance(exc, (openai.AuthenticationError, openai.PermissionDeniedError)):
            return LLMError("The server's credentials were rejected by the model service. Please contact the administrator.")
        if isinstance(exc, openai.NotFoundError):
            return LLMError("The configured model is not available. Please contact the administrator.")
        if exc.code == "context_length_exceeded":
            return LLMError("This conversation is too long for the model.")
        return LLMError("The model could not process this request.")

    return LLMError(UNAVAILABLE)


def _retry_after_seconds(exc: openai.APIStatusError) -> float | None:
    raw = exc.response.headers.get("retry-after")
    try:
        return float(raw) if raw is not None else None
    except ValueError:
        return None  # an HTTP-date form; ignore and use our own backoff


# ── Retry policy ─────────────────────────────────────────────────────────
async def stream_with_retry(
    open_attempt: Callable[[], AsyncIterator[LLMEvent]],
    *,
    max_attempts: int,
    base_delay: float,
    max_delay: float = 8.0,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    jitter: Callable[[], float] = random.random,
) -> AsyncIterator[LLMEvent]:
    """Run `open_attempt()` and retry it ONLY when that is safe.

    THE RULE:  never retry once a text delta has been yielded.

        before the first token  -> the user has seen nothing. Starting over is
                                   invisible to them: safe to retry.
        after the first token   -> the user (and the database) already holds
                                   "Hello my". A fresh attempt would generate its
                                   own, different answer from the start:
                                   "Hello my name is..." Appending it duplicates
                                   text ("Hello myHello my name is..."), and
                                   replacing it makes visible text vanish. Neither
                                   is correct, and the model is not guaranteed to
                                   reproduce the same words (generation is
                                   sampled). So we stop, keep the partial text,
                                   and report an error. A cleaner "continue from
                                   here" needs provider support we don't assume.

    Also: retry only TRANSIENT errors (exc.retryable), a small bounded number of
    times, with exponential backoff + jitter (so many clients that failed together
    don't all hammer the provider again at the same instant). Cost note: a failed
    attempt that did start generating can still be billed, one more reason to keep
    the retry count small.
    """
    attempt = 1
    while True:
        started = False  # has any text from THIS reply been handed to the user?
        try:
            # aclosing(): deterministically close the inner stream (and its HTTP
            # connection) even if we are cancelled or stop early.
            async with aclosing(open_attempt()) as events:
                async for event in events:
                    if isinstance(event, TextDelta):
                        started = True
                    yield event
            return
        except LLMError as exc:
            if started or not exc.retryable or attempt >= max_attempts:
                log.info(
                    "llm_no_retry attempt=%d max_attempts=%d started=%s retryable=%s",
                    attempt, max_attempts, started, exc.retryable,
                )
                raise
            delay = min(max_delay, base_delay * 2 ** (attempt - 1)) + jitter() * base_delay
            if exc.retry_after:  # the provider told us how long to wait: respect it (capped)
                delay = max(delay, min(exc.retry_after, max_delay))
            log.warning(
                "llm_retry attempt=%d max_attempts=%d delay_s=%.2f cause=%s",
                attempt, max_attempts, delay, type(exc.__cause__ or exc).__name__,
            )
            await sleep(delay)
            attempt += 1


# ── The real thing: OpenAI Responses API ─────────────────────────────────
def to_openai_input(history: list[HistoryMessage]) -> list[dict[str, str]]:
    """Convert OUR history format into the Responses API `input` format.

    Today they are the same shape, and that is the point of having the function:
    the day a provider wants a different shape (content parts, tool messages,
    a different role name), only this function changes.
    """
    return [{"role": m["role"], "content": m["content"]} for m in history]


def _stop_reason(response: Any) -> str:
    """Normalise how a response ended.

    'completed'         -> the model finished on its own
    'max_output_tokens' -> we cut it off with our output limit (UI must say so!)
    'content_filter'    -> provider stopped it
    """
    if response.status == "incomplete":
        details = response.incomplete_details
        return (details.reason if details and details.reason else "incomplete")
    return "completed"


class OpenAILLM:
    def __init__(self, settings: Settings, client: AsyncOpenAI | None = None) -> None:
        assert settings.openai_model, "validated at startup"
        self.model = settings.openai_model
        self._title_model = settings.title_model
        self._instructions = settings.system_prompt
        self._max_output_tokens = settings.max_output_tokens
        self._max_attempts = settings.llm_max_attempts
        self._retry_base_delay = settings.retry_base_delay_s

        # The OpenAI client is created on the BACKEND because the API key must
        # never be shipped to the browser. The frontend only ever talks to our
        # FastAPI server; FastAPI then talks to OpenAI:
        #
        #     Browser  ->  FastAPI (holds the key)  ->  OpenAI
        #
        # max_retries=0: the SDK would otherwise silently retry on its own
        # (default 2). We turn that off because WE own the retry policy - it
        # needs to know whether tokens already reached the user, which the SDK
        # cannot know. See stream_with_retry().
        self._client = client or AsyncOpenAI(
            api_key=settings.openai_api_key,
            max_retries=0,
            # connect fast-fails; the read timeout applies BETWEEN streamed
            # chunks, so a stalled stream is detected instead of hanging.
            timeout=httpx2.Timeout(settings.openai_timeout_s, connect=5.0),
        )

    async def complete(self, history: list[HistoryMessage]) -> Completion:
        """One normal request: send everything, wait, get the whole answer.

        Request -> (wait for the entire generation) -> complete response.
        """
        try:
            response = await self._client.responses.create(
                model=self.model,  # which model: configuration, not code
                # `instructions` = the system prompt: standing rules for the
                # model's behaviour, kept separate from the conversation turns.
                instructions=self._instructions,
                # `input` = the conversation so far (our history, converted).
                input=to_openai_input(history),
                # Hard ceiling on generated tokens: bounds cost AND latency.
                max_output_tokens=self._max_output_tokens,
                # We keep history ourselves in SQLite; don't also store it at OpenAI.
                store=False,
            )
        except (openai.OpenAIError, httpx2.TransportError) as exc:
            raise translate_openai_error(exc) from exc

        if response.status == "failed":
            raise _failed_response_error(response.error)

        return Completion(
            text=response.output_text,  # SDK helper: joins all text parts of the output
            stop_reason=_stop_reason(response),
            usage=parse_usage(response.usage),  # exact tokens as REPORTED by OpenAI
            served_model=getattr(response, "model", None),
        )

    def stream(self, history: list[HistoryMessage]) -> AsyncIterator[LLMEvent]:
        """Streaming with bounded, safe retries (see stream_with_retry)."""
        return stream_with_retry(
            lambda: self._stream_once(history),
            max_attempts=self._max_attempts,
            base_delay=self._retry_base_delay,
        )

    async def _stream_once(self, history: list[HistoryMessage]) -> AsyncIterator[LLMEvent]:
        """ONE attempt at a streaming request: same call, but `stream=True`.

        Normal:    request -> wait for the WHOLE generation -> one big response
        Streaming: request -> delta, delta, delta, ... -> final "completed" event

        The server pushes many small events over one open HTTP response while the
        model is still generating. We yield each text delta the moment it arrives;
        the caller (FastAPI) forwards it to the browser immediately. That is why
        the first words show up after a fraction of a second even if the full
        answer takes ten: it improves PERCEIVED latency (time to first token),
        not total generation time.
        """
        stream = None
        try:
            stream = await self._client.responses.create(
                model=self.model,
                instructions=self._instructions,
                input=to_openai_input(history),
                max_output_tokens=self._max_output_tokens,
                store=False,
                stream=True,
            )
            # The SDK gives us typed events. The ones we care about:
            #   response.output_text.delta -> a new piece of text
            #   response.completed         -> finished; carries final usage
            #   response.incomplete        -> stopped early (e.g. max_output_tokens)
            #   response.failed / error    -> the generation broke
            # Everything else (response.created, output_item.added, ...) is
            # bookkeeping we deliberately ignore.
            async for event in stream:
                kind = event.type
                if kind == "response.output_text.delta":
                    yield TextDelta(event.delta)
                elif kind in ("response.completed", "response.incomplete"):
                    yield Completed(
                        _stop_reason(event.response),
                        parse_usage(event.response.usage),
                        getattr(event.response, "model", None),
                    )
                    return
                elif kind == "response.failed":
                    raise _failed_response_error(event.response.error)
                elif kind == "error":
                    log.error("provider_stream_error code=%s", event.code)
                    raise LLMError(UNAVAILABLE, retryable=event.code in ("server_error", "rate_limit_exceeded"))
            # The iterator ended WITHOUT a terminal event: the connection closed
            # early. Treating that as success would store a silently truncated
            # answer as 'complete', so it is an error.
            raise LLMError("The model stream ended unexpectedly.", retryable=True)
        except LLMError:
            raise
        except (openai.OpenAIError, httpx2.TransportError) as exc:
            raise translate_openai_error(exc) from exc
        finally:
            if stream is not None:
                # Closing the HTTP response tells OpenAI we are done listening, so
                # it can stop generating (and billing) tokens nobody will read.
                # Shielded so it still runs when this task is being CANCELLED
                # because the browser disconnected.
                with anyio.CancelScope(shield=True):
                    await stream.close()

    async def generate_title(self, first_user_text: str) -> str | None:
        """A separate, tiny, non-streaming call whose only job is naming the chat.

        Kept apart from the main reply so a slow/failed title never delays or
        breaks the answer, and so it can use a cheaper model (model routing).
        """
        try:
            response = await self._client.responses.create(
                model=self._title_model,
                instructions=(
                    "Write a title of 2 to 5 words for a chat that starts with the user "
                    "message below. Reply with the title only: no quotes, no final punctuation."
                ),
                input=first_user_text[:500],
                # Small cap = cheap. Not tiny: some models spend output tokens
                # "thinking" before the visible text, and a cap that is too low
                # leaves an empty title (we then fall back to a truncated message).
                max_output_tokens=128,
                store=False,
            )
        except (openai.OpenAIError, httpx2.TransportError) as exc:
            log.warning("title_generation_failed error_type=%s", type(exc).__name__)
            return None
        return clean_title(response.output_text)


def _failed_response_error(error: Any) -> LLMError:
    """A response can 'fail' after the HTTP request succeeded (status 200)."""
    code = getattr(error, "code", None)
    log.error("provider_response_failed code=%s", code)
    transient = code in ("server_error", "rate_limit_exceeded")
    return LLMError(UNAVAILABLE, retryable=transient)


def clean_title(raw: str) -> str | None:
    """Models sometimes add quotes, a trailing period or a second line."""
    lines = raw.strip().splitlines()
    title = lines[0].strip().strip("\"'`“”‘’").rstrip(".!?:;,") if lines else ""
    return title[:60] or None


def build_llm(settings: Settings) -> LLM:
    return OpenAILLM(settings)

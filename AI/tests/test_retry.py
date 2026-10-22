"""Retry policy: bounded, transient-only, and NEVER after the first token."""
import pytest

from app.llm.client import Completed, LLMError, TextDelta, stream_with_retry


def plans(*attempts):
    """Each attempt is a list of events / exceptions. Returns (open_attempt, calls)."""
    calls = {"n": 0}

    def open_attempt():
        plan = attempts[calls["n"]]
        calls["n"] += 1

        async def gen():
            for item in plan:
                if isinstance(item, Exception):
                    raise item
                yield item

        return gen()

    return open_attempt, calls


async def run(open_attempt, *, max_attempts=3, base_delay=1.0, max_delay=8.0, jitter=lambda: 0.0):
    delays, events = [], []

    async def fake_sleep(d):
        delays.append(d)

    try:
        async for e in stream_with_retry(
            open_attempt, max_attempts=max_attempts, base_delay=base_delay,
            max_delay=max_delay, sleep=fake_sleep, jitter=jitter,
        ):
            events.append(e)
        error = None
    except LLMError as exc:
        error = exc
    return events, delays, error


TRANSIENT = LLMError("down", retryable=True)
DONE = Completed("completed", None)


async def test_retries_before_first_token_and_then_succeeds():
    open_attempt, calls = plans([LLMError("down", retryable=True)], [TextDelta("Hi"), DONE])
    events, delays, error = await run(open_attempt)
    assert error is None and events == [TextDelta("Hi"), DONE]
    assert calls["n"] == 2 and len(delays) == 1


async def test_never_retries_after_the_first_token_even_if_the_error_is_retryable():
    open_attempt, calls = plans(
        [TextDelta("Hello my"), LLMError("network", retryable=True)],
        [TextDelta("Hello my name is"), DONE],  # would be a duplicated/contradictory answer
    )
    events, delays, error = await run(open_attempt)
    assert error is not None  # surfaced to the caller
    assert events == [TextDelta("Hello my")]  # NOT "Hello myHello my name is"
    assert calls["n"] == 1 and delays == []


async def test_non_retryable_errors_fail_immediately():
    open_attempt, calls = plans([LLMError("bad key", retryable=False)], [TextDelta("never"), DONE])
    _, delays, error = await run(open_attempt)
    assert error is not None and calls["n"] == 1 and delays == []


async def test_retries_are_bounded():
    open_attempt, calls = plans(*[[LLMError("down", retryable=True)]] * 10)
    _, delays, error = await run(open_attempt, max_attempts=3)
    assert error is not None
    assert calls["n"] == 3  # 1 initial + 2 retries, never infinite
    assert len(delays) == 2


async def test_max_attempts_one_means_no_retry():
    open_attempt, calls = plans([LLMError("down", retryable=True)], [DONE])
    _, _, error = await run(open_attempt, max_attempts=1)
    assert error is not None and calls["n"] == 1


async def test_backoff_is_exponential_and_capped():
    open_attempt, _ = plans(*[[LLMError("down", retryable=True)]] * 6)
    _, delays, _ = await run(open_attempt, max_attempts=6, base_delay=1.0, max_delay=5.0)
    assert delays == [1.0, 2.0, 4.0, 5.0, 5.0]  # 1,2,4 then capped at max_delay


async def test_jitter_is_added_to_spread_out_clients():
    open_attempt, _ = plans([LLMError("down", retryable=True)], [DONE])
    _, delays, _ = await run(open_attempt, base_delay=2.0, jitter=lambda: 0.5)
    assert delays == [2.0 + 0.5 * 2.0]


async def test_provider_retry_after_is_respected_but_capped():
    open_attempt, _ = plans(
        [LLMError("429", retryable=True, retry_after=6.0)],
        [LLMError("429", retryable=True, retry_after=600.0)],
        [DONE],
    )
    _, delays, _ = await run(open_attempt, base_delay=0.5, max_delay=8.0)
    assert delays[0] == 6.0  # honoured (bigger than our backoff)
    assert delays[1] == 8.0  # a hostile/huge Retry-After can't stall us forever

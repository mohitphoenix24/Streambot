---
title: The model layer: one wall around OpenAI
level: easy
time: 35 min
summary: The only module that talks to OpenAI, the small vocabulary it speaks to the rest of the app, how errors become safe messages, and the rule for when a failed request may be retried.
tags: abstraction, Protocol, errors, retries, backoff, timeouts
---

`AI/app/llm/client.py` is the **only** file in the project that imports `openai`. Everything above it (routes, pipeline, history, database) speaks a tiny vocabulary of its own. Everything below it is the SDK. This lesson is about that wall and what it buys.

:::analogy A power adapter
You do not rewire your laptop for every country. You carry one adapter. `client.py` is the adapter between OpenAI's plugs and the app's socket. If you change the model provider, or the SDK changes shape, you change the adapter, not the laptop.
:::

## The vocabulary

The rest of the app sees only these small, frozen records. No OpenAI object ever leaks out.

```python src="AI/app/llm/client.py" fn="TextDelta,Completed,Generated,ToolCallStarted,ToolCallFinished"
```

| Event | Meaning | Who uses it |
|---|---|---|
| `TextDelta(text)` | a few characters of the answer | forwarded straight to the browser |
| `Completed(stop_reason, usage, served_model)` | the reply ended healthily, with exact usage | triggers saving, cost and the `done` event |
| `ToolCallStarted` / `ToolCallFinished` | the model asked for a tool; we ran it | shown as "Searching the web…" |
| `Generated(text, usage, served_model, model)` | the result of a small side call (a title or a summary) | cost is recorded in `llm_calls` |

A stream is therefore a sequence like `TextDelta, TextDelta, …, Completed`, or `ToolCallStarted, ToolCallFinished, TextDelta, …, Completed`.

## The contract, as a Protocol

The pipeline does not depend on `OpenAILLM`. It depends on a *shape*:

```python src="AI/app/llm/client.py" fn="LLM,ToolExecutor"
```

:::def Protocol (structural typing)
A Python `Protocol` describes the methods an object must have, without requiring a base class. Anything with a `stream(...)` method that yields our events *is* an `LLM`. That is how tests plug in a scripted stand-in instead of the real model (see the testing lesson).
:::

## Errors: one safe sentence, and a flag

Providers fail in many ways. The browser must never see a raw exception, because it can contain URLs, request ids or parts of your configuration. So every failure is translated into an `LLMError` with two fields:

```python src="AI/app/llm/client.py" fn="LLMError"
```

- `public_message` is safe for the browser: a fixed, short sentence.
- `retryable` says whether trying again might succeed.

The translation table is where the engineering judgement lives:

```python src="AI/app/llm/client.py" fn="translate_openai_error"
```

| Failure | Retry? | Why |
|---|---|---|
| connection error, timeout | yes | the network blinked |
| 5xx, 408, 409 | yes | the provider had a bad moment |
| 429 rate limit | yes (short wait) | try again shortly, respecting `Retry-After` |
| 429 `insufficient_quota` | **no** | you are out of money; that is not transient |
| 400, 401, 403, 404 | **no** | a bug, a bad key or a wrong model name: an identical request fails identically |
| `context_length_exceeded` | **no** | the conversation is too long; retrying changes nothing |

## The most important rule: never retry after the first token

Retrying a failed request sounds harmless. It is not, once the user has seen words.

:::analogy The half-told story
A friend starts telling you a story: "Once upon a time there was a…" and the line drops. If they call back and start the story again from the top, you hear "Once upon a time there was a… Once upon a time there was a king…". Appending the new attempt duplicates text. Replacing the old text makes words you already read vanish. And the model is *sampled*, so the second telling is not even guaranteed to match the first.
:::

So the rule has two halves, and both are in `stream_with_retry`:

1. **Before the first token**: the user has seen nothing. Starting over is invisible. Retry (if the error is retryable and attempts remain).
2. **After the first token**: stop. Keep the partial text, report an error.

```python src="AI/app/llm/client.py" fn="stream_with_retry"
```

Read the `started` flag. It flips to `True` the moment a `TextDelta` is yielded, and the `except` block refuses to retry once it is set. Retries are also **bounded** (`LLM_MAX_ATTEMPTS`, default 2) and use **exponential backoff with jitter**.

:::def Exponential backoff with jitter
Wait longer after each failure (0.5 s, 1 s, 2 s…) so a struggling server gets room to recover, and add a little randomness (jitter) so many clients that failed together do not all retry at the same instant. If the provider sends `Retry-After`, honour it (capped).
:::

Here is the delay schedule the function computes, written out so you can play with it:

```python run title="The backoff formula from stream_with_retry" level=easy
import random

def delay(attempt, base=0.5, max_delay=8.0, retry_after=None):
    d = min(max_delay, base * 2 ** (attempt - 1)) + random.random() * base   # backoff + jitter
    if retry_after:                       # the provider told us how long to wait: respect it (capped)
        d = max(d, min(retry_after, max_delay))
    return d

for attempt in range(1, 6):
    print(f"after failure {attempt}: wait about {delay(attempt):.2f}s")
print("with Retry-After: 5 ->", round(delay(1, retry_after=5), 2), "s")
```

:::note Why the SDK's own retries are switched off
The OpenAI client silently retries twice by default. Streambot sets `max_retries=0` because *we* own the policy, and the policy needs to know whether tokens already reached the user, which the SDK cannot know. Two layers of retries would also multiply (2 × 2 attempts) and quietly double your bill during an outage.
:::

## Creating the client

```python src="AI/app/llm/client.py" fn="OpenAILLM.__init__"
```

Three details matter here.

- **The key lives only on the server.** The browser talks to FastAPI; FastAPI talks to OpenAI. Anything in the browser bundle is public, so a key there would be leaked.
- **Two different timeouts.** `connect=5.0` fails fast if OpenAI is unreachable. The other number (`OPENAI_TIMEOUT_S`) is a *read* timeout that applies **between streamed chunks**, so a stalled stream is detected instead of hanging for ever.
- **Model names come from settings**, including a separate `title_model` and `summary_model` so cheap tasks can use a cheap model (model routing).

## Letting go of the stream cleanly

When the user presses Stop, the server must close the HTTP response to OpenAI so it stops generating (and billing) tokens nobody will read. This is the `finally` block at the end of `_stream_once`:

```python src="AI/app/llm/client.py" from="finally:" to="await stream.close()"
```

The `CancelScope(shield=True)` is subtle but important. When a task is being **cancelled**, any `await` inside it would normally be interrupted immediately. Shielding lets the `close()` finish even while the surrounding task is being cancelled.

## Side calls: titles and summaries

Two small, non-streaming calls round out the layer. Naming a chat is easy; answering is hard, so the title call can run on a cheaper model, and a slow or failed title can never delay or break the answer. Models often add quotes or a trailing full stop, so the result is tidied by a small pure function you can run:

```python run include="AI/app/llm/client.py:clean_title" title="Cleaning a model-written title (the app's own function)" level=easy
for raw in ['"Understanding RAG."', "Trailing period.", "First line\nSecond line", "   ", "‘Fancy quotes’!"]:
    print(repr(raw), "->", repr(clean_title(raw)))

# Titles are capped at 60 characters, and an empty result is None (the app then keeps
# a title made from the first words of the message).
print(len(clean_title("x" * 200)))
```

:::keypoints
- `client.py` is the **only** file that imports `openai`. The rest of the app speaks `TextDelta`, `Completed`, `LLMError` and friends.
- A `Protocol` describes the shape the app needs, so tests can substitute a scripted model.
- Every failure becomes a **safe sentence** plus a **retryable** flag. Raw exceptions never reach the browser.
- **Never retry after the first token**: it would duplicate or erase text the user already read. Before the first token, retry with bounded **exponential backoff and jitter**.
- The SDK's silent retries are switched off, so retries are counted once and the app owns the policy.
- Close the upstream stream on cancel, shielded from the cancellation, so you stop paying for tokens nobody reads.
:::

:::exercise Classify the error
For each failure, decide *retryable or not* and why: (a) the Wi-Fi drops mid-request, (b) you typo the model name, (c) OpenAI returns HTTP 503, (d) the account is out of credit, (e) the connection drops after the model has already streamed 40 words.
:::

:::solution Show the answer
(a) retryable (transient network). (b) not retryable (a 404: the same request fails again). (c) retryable (server error). (d) not retryable (`insufficient_quota` is not transient). (e) **never retried**, even though the error type is retryable, because text was already delivered; the partial text is kept and an error is shown.
:::

```quiz
? A stream fails with a retryable error AFTER 40 words were already shown. What does Streambot do?
- Retries from the beginning and appends the new answer.
- Retries and replaces the old text.
+ Stops, keeps the 40 words, and shows an error.
> A fresh attempt generates a different answer, so appending duplicates and replacing erases what the user read.

? Why does Streambot set the SDK's `max_retries=0`?
- Retries are not supported for streaming.
+ The app owns the retry policy, which must know whether tokens already reached the user; SDK retries on top would also multiply attempts.
- To make requests faster.
> The policy lives in `stream_with_retry`, once, where it can see the `started` flag.

? Where does the OpenAI API key live?
- In the React bundle so the browser can call OpenAI directly.
+ Only on the server, in `.env`; the browser talks only to FastAPI.
- In the database.
> Anything in a frontend bundle is public. The key never leaves the server.
```

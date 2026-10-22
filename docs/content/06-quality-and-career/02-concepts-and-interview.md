---
title: Concept map and interview answers
level: medium
time: 40 min
summary: Every generative-AI and engineering idea in Streambot in one table, then the questions an interviewer is likely to ask, with short answers that point at the real code.
tags: concepts, cheat sheet, interview, Q&A
---

This lesson is for revision. First a map from each idea to *where it lives in the app*, then questions and answers you can practise out loud. The answers are deliberately short: in an interview you want a clear first minute, then you go deeper when asked.

## The concept map

| Concept | In one line | Where in Streambot |
|---|---|---|
| Stateless model | every request must carry the conversation | `begin_turn`, `build_history` |
| Token | the unit of cost and limits | `usage.py`, `pricing.py` |
| Context window | the model's hard limit | `HISTORY_BUDGET_TOKENS` is a smaller, deliberate budget |
| System prompt | developer-set standing rules | `instructions` in `client.py` |
| Streaming / TTFT | words arrive live; first-word delay is what users feel | `stream_reply`, `ttft_ms` |
| SSE | one-way text stream over HTTP | `chat/sse.py`, `frontend/src/sse.ts` |
| Cancellation | Stop must reach the provider call | `is_disconnected`, `CancelledError`, `aclosing` |
| Partial persistence / crash recovery | the row exists before the model runs | `begin_turn`, `recover_interrupted` |
| Retry policy | retry before the first token only; backoff and jitter | `stream_with_retry` |
| Error translation | safe sentence plus a retryable flag | `translate_openai_error` |
| Structural typing | the app depends on a shape, not a class | `LLM`, `ToolExecutor` protocols |
| Token budgeting | newest-first, no holes, user turn first | `build_history` |
| Rolling summary / memory | fold old messages into a summary | `plan_summary`, `MemoryManager` |
| Hysteresis | two thresholds, so changes are rare | `SUMMARIZE_ABOVE`, `KEEP_RECENT` |
| Tool / function calling | the model asks, the app runs | `WEB_SEARCH`, `ToolRunner` |
| Agent loop | model → tool → model → … → answer | `OpenAILLM._stream_agent` |
| Encrypted reasoning | reasoning travels back when `store=False` | `include=["reasoning.encrypted_content"]` |
| Citations / grounding | answers point at real, numbered sources | `ToolRunner`, `linkCitations` |
| Prompt injection | tool output is untrusted text | read-only tools, inert rendering |
| Prompt caching | an exact repeated prefix is billed cheaply | stable prompt order, `prompt_cache_key` |
| Cost accounting | exact arithmetic, honest unknowns | `calculate_cost`, `Decimal`, `None` |
| Model routing | cheap model for easy side tasks | `title_model`, `summary_model` |
| Rate limiting, validation | protect cost and availability | `SlidingWindowLimiter`, `prepare_send` |
| Fail-fast config | crash at startup with a clear message | `Settings.validate` |
| Optimistic UI | show the result before the server confirms | `Chat.tsx` |
| Safe rendering | LLM output is untrusted | `SafeMarkdown` |
| Test doubles | scripted model, SDK stub, network blocker | `tests/helpers.py`, `conftest.py` |
| Evals | scored questions that measure quality | *not implemented (be honest about it)* |

## Questions and answers

### Fundamentals

**Why does a chat app resend the whole conversation on every request?**
Because the model is stateless: it keeps nothing between calls. "Memory" is the app's job: store the messages and send the relevant ones each time. Streambot rebuilds the history from SQLite for every request, never trusting the browser, and sets `store=False` so OpenAI keeps nothing.

**What is a token, and why do you care?**
A chunk of text, roughly four characters in English. Cost and limits are both counted in tokens, with output priced several times higher than input. I use an *estimate* (`len//4 + 4`) only for budgeting, and the provider's exact `usage` for billing, and I have a test ensuring the estimate never appears in billing code.

**What is the difference between the context window and your history budget?**
The window is the model's limit for input plus output. The budget is my smaller cap for just the past-messages part, because the window must also hold the instructions, the new message and the reply.

### Streaming and reliability

**Why stream, and how?**
Streaming improves time to first token, the delay users feel; it does not shorten total generation time. The server is an async generator behind `StreamingResponse`, emitting SSE events. The browser reads the body as a `ReadableStream` with `fetch`, because `EventSource` cannot send a POST body.

**SSE versus WebSockets?**
SSE is one-way, plain text over HTTP, and works through normal proxies and `curl`. WebSockets are two-way and need an upgraded connection. A chat reply is one request and one streamed answer, so SSE is simpler.

**How does Stop work?**
`AbortController.abort()` closes the connection. The server notices two ways, `request.is_disconnected()` between events or task cancellation while it waits for the next token, closes the upstream stream so the model stops (and billing stops), and a `finally` block saves the partial text with status `cancelled`.

**Why create the assistant row before calling the model?**
So a Stop, an error or a crash still leaves a record with a status and any partial text. On startup, orphaned `streaming` rows are marked `error`. Without the row, a reply that dies would simply vanish.

**What is your retry policy, and why?**
Retry only transient errors, a bounded number of times, with exponential backoff and jitter, and **never after the first token has reached the user**, because a second attempt would generate a different answer: appending duplicates text and replacing erases text the user read. I also turned off the SDK's own retries so attempts are counted once.

**How do you stop provider errors leaking to users?**
Every failure becomes an `LLMError` with a fixed, safe `public_message` and a `retryable` flag. Details go to the server log only, and conversation text is never logged.

### Memory and history

**How do you keep long conversations working?**
First, a newest-first token budget (stop at the first message that does not fit, to avoid holes; always keep the newest user message; start with a user turn). On top of that, a rolling summary: when unsummarised messages exceed 80% of the budget, a background call folds the oldest into a summary until about 40% remains verbatim. The history sent is `[summary] + recent messages`.

**Why 80% and 40% instead of summarising every turn?**
Hysteresis. It avoids an extra model call per message, and it moves the summary boundary in big, rare steps, so the start of the prompt stays identical between steps, which matters for prompt caching.

**What are the downsides of summarising?**
It is lossy (details can drop or distort), it costs an extra call each time, and it lags one turn behind. So truncation stays as the safety net, failures keep the old summary, and it can be switched off.

### Agents and tools

**Explain tool calling.**
I describe a function to the model with a name, description and a JSON Schema. If it wants it, the model returns a function call instead of text; my code runs the function and sends the output back with the same `call_id`; the model continues. The model never runs anything itself.

**Walk me through your agent loop.**
`_stream_agent` makes a streamed request (a round). If the round ends with function calls, it runs them, appends the model's output items (including encrypted reasoning) and a `function_call_output` for each call, and asks again. A round with no function call is the answer. Retries wrap one round, and tools run after a round completes, so a retry can never repeat a search.

**Why `include=["reasoning.encrypted_content"]`?**
With `store=False` nothing is kept at the provider, so a reasoning model's reasoning from one round has to travel back with the next request for it to continue properly.

**How do citations work?**
The server numbers sources across searches in one reply. The prompt tells the model to cite as `[n]` using those numbers. The browser rewrites `[n]` into chips only when source n exists and never inside code, and shows every source in a collapsible list, because models cite sparsely.

**What are the security risks of a search tool?**
Prompt injection (a page can contain hostile instructions), malicious links, tracking images and key leakage. I keep the tool read-only so a manipulated model has nothing dangerous to do, keep only `http(s)` URLs, remove images, open links with `noopener noreferrer`, and keep the keys server-side.

### Cost

**How do you compute cost exactly?**
From the provider's reported usage and a price table I maintain, in `Decimal`. Cached tokens are a subset of input and reasoning tokens a subset of output, so each part is charged once. An unknown price is `None` ("cost n/a"), never zero.

**How does prompt caching work and what did you do for it?**
The provider caches the processed prefix of long prompts and bills repeats at a lower rate, but only for an exact prefix. I put the stable parts first (instructions, tools, summary, append-only history), keep dynamic data out of the prefix, and send the conversation id as `prompt_cache_key`. Verified live: the second identical call had 2,439 of 2,442 tokens cached and cost about 86% less.

**Why is a deleted chat's spend still counted?**
Because the tokens were spent. Deleting a chat removes its content, but each reply's usage numbers (never its text) are archived in the same transaction, on their original date.

### Production

**What would you change to put this in production?**
Authentication and per-user data, PostgreSQL, a shared rate-limit store, spend alerts and an agent-loop ceiling, observability (traces, metrics), CI and deployment, moderation and a retention policy, and an evaluation harness for answer quality.

**How do you know the app works without calling OpenAI in tests?**
A scripted model and an SDK stub cover the pipeline and the adapter, an autouse fixture makes any real network call fail, and a live-server test proves disconnect handling. The real integration was checked once by hand with a strict call ceiling. The tests do not prove answer quality; evals would.

### Frontend

**Why are chunks not events?**
TCP guarantees order, not message boundaries. One chunk can hold several events or half of one, so the parser buffers text and only emits an event once its blank-line terminator has arrived, keeping the incomplete tail for the next chunk.

**Why render Markdown only after a reply completes?**
A half-received document is often invalid Markdown: an open code fence swallows the rest, `**bo` has no closing marker. Re-parsing on every token flickers. Plain text while streaming, rendered Markdown once complete.

**What was the trickiest bug on the client?**
A stale closure: React ran a state updater after I had reassigned the assistant's message id, so the placeholder was never renamed and later deltas matched nothing. The fix is to capture the old id first.

:::keypoints
- Learn the **map**: for every concept, know *what it is*, *why it exists* and *where it lives in the code*.
- Answer in layers: a clear one-sentence answer first, then the reason, then the code or the number that proves it.
- Prefer **specific** answers ("2,439 of 2,442 tokens cached, about 86% cheaper") to general ones.
- Be honest about limits: no evals, no auth, single user. Saying so is a strength.
:::

:::exercise Teach it back
Pick any three rows of the concept map and explain each to a friend who has never written code, using one analogy, without looking at the lesson. The ones you cannot explain are the ones to re-read.
:::

```quiz
? An interviewer asks "why not just retry when the stream drops?" What is the best short answer?
- Retries are always safe.
+ Before the first token a retry is invisible; after it, a new attempt would be a different answer, so appending duplicates text and replacing erases what the user read.
- The SDK forbids retries.
> That is the rule in `stream_with_retry`.

? Which number best shows prompt caching working?
- 262 tests.
+ The second identical request had 2,439 of 2,442 input tokens cached and cost about 86% less.
- 5 sources.
> Concrete, measured evidence is what makes a claim credible.
```

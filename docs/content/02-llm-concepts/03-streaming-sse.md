---
title: Streaming with SSE, Stop and crash recovery
level: easy
time: 45 min
summary: Why replies stream, how Server-Sent Events look on the wire, how the pipeline turns model events into SSE events, and how Stop, errors and crashes are all funnelled into one place.
tags: SSE, StreamingResponse, async generator, cancellation, TTFT, state machine
---

This is the heart of the app. In this lesson you will see exactly what travels over the wire when a reply streams, read the generator that produces it, and understand the awkward cases: the user pressing **Stop**, the model failing halfway, and the server being killed in the middle of a reply.

## Why stream at all?

A long answer can take ten seconds to generate. Without streaming, the screen is frozen for ten seconds and then the whole answer appears. With streaming, the **first words appear in a fraction of a second** and the rest follow as they are written.

:::def Time to first token (TTFT)
The time from sending a request until the first piece of the answer arrives. It is the number users actually *feel* as "how fast does it respond". Streaming does not make the full answer finish sooner; it makes the app feel fast by improving TTFT. Streambot measures it for every reply and shows it under the answer.
:::

## Server-Sent Events, on the wire

:::def Server-Sent Events (SSE)
A one-way streaming format over an ordinary HTTP response. The server sets `Content-Type: text/event-stream` and simply never finishes the response; it keeps writing small text blocks, each one an *event*. It is plain text, works through ordinary proxies and load balancers, and can be read with `curl`.
:::

One event is a few `field: value` lines ended by **one blank line**:

```text title="What the browser receives"
event: start
data: {"user_message_id": "a1", "assistant_message_id": "b2", "history_messages": 3, ...}

event: delta
data: {"text": "The latest "}

event: delta
data: {"text": "stable release is 3.15."}

event: done
data: {"stop_reason": "completed", "usage": {...}, "cost": {...}, ...}

```

| Line | Meaning |
|---|---|
| `event:` | the event's name, so the client can react differently to `delta`, `done`, `error` … |
| `data:` | the payload. Streambot always sends **one line of JSON** |
| a blank line | **ends the event**. Without it, the client waits for ever |

The function that produces one event is tiny. Run it, and try putting a newline inside the text:

```python run include="AI/app/chat/sse.py:sse" title="One SSE event (the app's own function)" level=easy
print(repr(sse("delta", {"text": "Hello, "})))
print(repr(sse("delta", {"text": "नमस्ते 👋"})))
print(repr(sse("done", {})))

# A newline INSIDE the text must not end the event early. json.dumps writes it as the two
# characters backslash-n, so `data:` always stays on ONE line:
wire = sse("delta", {"text": "line one\n\nline two"})
print(repr(wire))
assert wire.count("\n") == 3          # event line, data line, and the terminating blank line

# Receiving side: split on the blank line to get events back.
stream = sse("delta", {"text": "a"}) + sse("delta", {"text": "b"}) + sse("done", {})
print([block.split("\n")[0] for block in stream.split("\n\n") if block])
```

:::note SSE is not WebSockets
WebSockets are a separate, two-way protocol (the connection is upgraded away from HTTP). Use them when the client also streams data *back* continuously: voice, multiplayer, collaborative editing. A chat reply is one request and one streamed answer, so SSE is simpler, and it works with everything that already works for HTTP (auth, proxies, curl).
:::

## Why `fetch` and not `EventSource`

The browser has a built-in `EventSource` for SSE, but it can only send a **GET** with no body. Our request must carry the user's text, so it is a **POST** and the client reads the response body as a stream itself ([The React client](#/l/react-client) covers that code). The trade-off: `EventSource` reconnects automatically, which here would be *wrong*, because re-sending the POST would duplicate the user's message.

## The pipeline: an async generator

FastAPI's `StreamingResponse` accepts any **async generator of strings** and sends each `yield` to the browser as it happens. So the route does the ordinary checks, then hands over a generator. The generator is `stream_reply`. It looks long because it has to be correct in every way a reply can end, so we read it in pieces.

:::def Async generator
A function with `async def` that contains `yield`. It produces values one at a time, and can `await` (wait for the network) between them. Each `yield` here is one SSE event, and while it waits for the next model token the server is free to serve other users.
:::

### Part 1: the setup, and the pessimistic default

```python src="AI/app/chat/streaming.py" from="assistant_id = turn.assistant_message" to="    })"
```

Look at `outcome = "cancelled"`. The function **assumes the worst** and only changes it to `"complete"` or `"error"` when it positively reaches one of those. Anything else that unwinds the generator (the browser disconnecting, the task being cancelled, the response being closed) *is* a cancellation, and gets the right treatment automatically.

### Part 2: forwarding the stream

```python src="AI/app/chat/streaming.py" from="upstream = llm.stream(" to='yield sse("delta"'
```

Four things happen for every event:

1. **Is the browser still there?** `request.is_disconnected()` is the first way to notice a Stop (below).
2. **First token?** The first `TextDelta` records the TTFT.
3. **Checkpoint.** About once a second (`PARTIAL_FLUSH_INTERVAL_S`) the text so far is written to the database. Writing on *every* token would be wasteful, but holding the whole answer only in memory would lose it in a crash.
4. **Forward.** The delta becomes an SSE `delta` event, and the browser shows it immediately.

### Part 3: tools and the healthy end

```python src="AI/app/chat/streaming.py" from="elif isinstance(event, ToolCallStarted):" to='outcome = "complete"'
```

The `else` on a `for`/`async for` loop runs only when the loop finished **without** `break`. A healthy stream must have ended with a `Completed` event; a stream that just stops is an *error*, never silently marked complete (that would store a truncated answer as if it were whole).

### Part 4: the two unhappy endings

```python src="AI/app/chat/streaming.py" from="except asyncio.CancelledError:" to="public_error = exc.public_message if isinstance(exc, LLMError) else UNAVAILABLE"
```

Two kinds of trouble, handled very differently:

- **Cancelled** (you pressed Stop): do nothing special except re-raise; the `finally` block below saves what we have.
- **Error** (the model or network failed): the technical detail goes to the **server log only**. The browser gets `public_error`, a short safe sentence, because raw exception text can leak URLs, request ids and configuration.

### Part 5: one `finally` for every ending

```python src="AI/app/chat/streaming.py" from="    finally:" to="db.touch_conversation(conversation_id)"
```

This is the most important block in the file. Whatever happened (success, handled error, disconnect or cancellation), control arrives here and writes the final state to the database. Notice two deliberate choices:

- **There is no `await` inside it.** The database writes are plain synchronous calls (on pooled connections: see the persistence lesson), so even a task that is *being cancelled* cannot be interrupted halfway through saving.
- **Partial text is never replaced by a blank.** An errored or stopped reply keeps whatever arrived, and usage is stored only if the provider actually reported it.

## The status machine

Every reply is a small state machine. The status is a column in the database, so the UI can always tell a finished answer from one that died:

```text
                 ┌──────────────► complete    the model finished; usage, cost and stop reason saved
   streaming ────┼──────────────► cancelled   the user pressed Stop; partial text kept
                 └──────────────► error       model or network failed; partial text kept, safe message shown
```

## What Stop really does, end to end

:::flow Stop, end to end
Click | the Stop button calls `abort()` on the browser's AbortController
Drop | the browser closes the TCP connection
Notice | the server learns of it two ways: `request.is_disconnected()` between events, or the task being cancelled while it waits for the next token
Close | the upstream OpenAI stream is closed, so the model stops generating tokens you would pay for
Save | the `finally` block stores the partial text with status `cancelled`
Show | the UI marks the reply "Stopped" and keeps the text
:::

Why *two* signals? A poll of `is_disconnected()` cannot run while the code is blocked waiting for OpenAI's next token. In that window only task cancellation can wake it up. Both are needed.

:::warn Hiding the button is not cancelling
If the UI merely stopped showing text but the server kept reading, the model would carry on generating, and billing, tokens nobody will read. Real cancellation must reach the provider call. Streambot has a test that starts a real server, opens a real TCP connection, hangs up mid-stream, and checks that the model stream was closed and the partial text saved.
:::

## When the server itself dies

If the process is killed mid-reply (`kill -9`, power loss), nothing gets to run a `finally`. The row would be stuck at `streaming` for ever. So on **startup**, one query fixes every orphan:

```python src="AI/app/db.py" fn="Database.recover_interrupted"
```

At startup no stream can possibly be live, so any `streaming` row is an orphan. It becomes `error` with the reason `interrupted`, and the partial text flushed before the crash is kept. This is why the assistant row is created *before* the model is called.

## The route and its three headers

```python src="AI/app/main.py" from="return StreamingResponse(" to='"X-Accel-Buffering": "no",'
```

- `Cache-Control: no-cache` stops proxies and browsers from caching a live stream.
- `X-Accel-Buffering: no` tells nginx to forward each chunk immediately. Without it a reverse proxy may collect the whole answer and release it at once, which makes your streaming look broken *only in production*.

You can watch the raw stream yourself, with `curl -N` (no buffering) against a running server:

```bash
curl -N -X POST http://localhost:8501/conversations/<id>/messages \
  -H "Content-Type: application/json" -d '{"text": "hello", "web_search": false}'
```

:::keypoints
- Streaming improves **time to first token**, the delay users actually feel; it does not shorten total generation time.
- **SSE** is plain text over HTTP: `event:` and `data:` lines, ended by a blank line. `fetch` is used instead of `EventSource` because the request is a POST.
- The pipeline is an **async generator**: each `yield` is one event, and waiting for the model never blocks other users.
- The reply's status is a small **state machine** (`streaming` → `complete` / `cancelled` / `error`), stored in the database.
- **One `finally` block** writes the final state for every ending, with no `await`, and never blanks a partial answer.
- **Stop** works through two signals and must reach the provider call, or you keep paying.
- **Crash recovery** turns orphaned `streaming` rows into `error` at startup, which only works because the row exists before the model is called.
:::

:::exercise Which ending?
For each situation, say which status the reply ends with and whether an SSE event is sent afterwards: (a) the model finishes normally, (b) OpenAI returns HTTP 503 after 30 words, (c) you press Stop after 10 words, (d) the server is killed after 20 words and restarted.
:::

:::solution Show the answer
(a) `complete`; a `done` event follows. (b) `error`; an `error` event follows, with the 30 words kept. (c) `cancelled`; no event is sent, because nobody is listening, and the 10 words are kept. (d) the row is left as `streaming` until startup recovery marks it `error` (`interrupted`); no event is sent, and the text up to the last checkpoint (about one second behind) is kept.
:::

```quiz
? What does the blank line at the end of an SSE event do?
- It is just formatting.
+ It marks the event as complete, so the client can dispatch it.
- It tells the server to close the connection.
> SSE is line based; the empty line means "event finished". Without it the client keeps waiting.

? Why is `outcome` initialised to "cancelled"?
- Because most replies are cancelled.
+ So any unexpected way out of the generator (disconnect, cancellation, closed response) is treated as a cancellation and still saved properly.
- To avoid a None value.
> The function only upgrades it to "complete" or "error" when it positively reaches one of those.

? Why does the `finally` block contain no `await`?
- await is not allowed in finally.
+ So a task that is being cancelled cannot be interrupted halfway through saving the final state.
- Because the database is slow.
> The database writes are plain synchronous calls: nothing can interrupt them halfway, so the save either runs completely or not at all.
```

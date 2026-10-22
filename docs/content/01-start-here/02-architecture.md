---
title: The architecture and the life of one message
level: easy
time: 25 min
summary: How the layers fit together, what each one is allowed to know, and exactly what happens from the moment you press Enter to the moment the answer finishes.
tags: architecture, layers, request lifecycle, data model
---

Before any detail, you need the one picture that everything else hangs on. Streambot has a handful of layers, each with **one job** and a strict list of things it is *not* allowed to know. That separation is the whole design.

:::flow The layers, outside in
Browser | React: shows the chat, reads the stream, lets you press Stop
Routes | main.py: checks the request, hands it on (HTTP only, no logic)
Pipeline | chat/streaming.py: saves messages, asks the model, forwards events, records the result
Model layer | llm/client.py: the ONLY code that talks to OpenAI
Tools | tools/: web search, run on the server with the server's key
Storage | db.py: SQLite, every query in one file
:::

## The walls between the layers

A "wall" means a layer is forbidden to import or know about something. These rules are what make the app testable and changeable.

| Layer | Its one job | It must **not** know about |
|---|---|---|
| `main.py` (routes) | turn HTTP into function calls and back | OpenAI, SQL, token counting |
| `chat/streaming.py` (pipeline) | glue the database, the model and the SSE stream together | HTTP routing, OpenAI's types |
| `llm/client.py` (model layer) | talk to OpenAI, retry, translate errors | the database, HTTP |
| `chat/history.py`, `chat/memory.py` | decide *what the model sees* | everything else (they are plain functions) |
| `llm/pricing.py`, `llm/usage.py` | tokens in, money out | everything else |
| `tools/` | run one tool safely | OpenAI |
| `db.py` | SQL | HTTP, OpenAI |

:::analogy Why walls pay off
When OpenAI shipped a new major version of its SDK, the only file that had to change was `llm/client.py`. When a different model needs a different request shape, again only that file. And every other layer is tested with a scripted stand-in for the model, because nothing outside `client.py` ever sees an OpenAI object.
:::

## The life of one message

You type "What is the latest stable Python?" and press Enter. This is everything that happens, in order. Read it twice; the rest of the guide is this list in slow motion.

:::flow One message, end to end
1. Send | the browser POSTs `{text, web_search}` to `/conversations/{id}/messages`
2. Check | rate limit, size limit, does the chat exist, empty text, is a reply already running (409)
3. Save | the user's message is written to SQLite; an EMPTY assistant row is created with status `streaming`
4. Build | the history is rebuilt FROM THE DATABASE: the summary plus the recent messages, within the token budget
5. Start | an SSE `start` event tells the browser the real message ids
6. Ask | OpenAI is called with the history, the instructions, the tools and a cache key, streaming
7. Forward | every text delta becomes an SSE `delta` event; the text is also checkpointed to SQLite about once a second
8. Tool | if the model asks to search, the server runs Tavily, sends `tool_call` / `tool_result` events, and asks the model again
9. Finish | the final text, token usage, cost, sources and status are written in one `finally` block
10. Done | an SSE `done` event carries usage, cost and the cache savings
11. After | in the background: a summary update if the chat is long; a title for the chat if it is new
:::

Notice three things already.

1. **The database is written before and during the stream, not only at the end.** Step 3 creates the assistant row *before* calling the model. If the server dies halfway, there is a record of what was in progress. That one decision gives crash recovery and Stop-keeps-the-partial-answer for free.
2. **The history comes from the database, never from the browser.** The browser only sends the new text. The server decides what the model sees, so a user cannot forge earlier messages.
3. **Everything that can fail with a normal HTTP status happens before streaming starts** (steps 1 to 4). Once the first byte of the stream is sent, the status code `200` is already gone, so later problems must travel inside the stream as an `error` event.

## Step 3 in code

Here is the preparation, the first half of the pipeline. It is short on purpose.

```python src="AI/app/chat/streaming.py" fn="Turn,begin_turn"
```

Read the comments in `begin_turn`. Each numbered step is a design decision with a reason, and you will meet every one of them again: *why the placeholder row exists* ([Streaming with SSE](#/l/streaming-sse)), *why history is built here* ([History and the token budget](#/l/history-budget)), *why the summary is passed in* ([Rolling-summary memory](#/l/rolling-memory)).

## The data model

Two tables carry the product, and a few more carry the accounting:

```text
conversations    id, title, created_at, updated_at, summary, summary_through_id
messages         id, conversation_id -> conversations (ON DELETE CASCADE),
                 role, content, status, input_tokens, cached_input_tokens, output_tokens,
                 reasoning_tokens, total_tokens, cost_usd, stop_reason, model, served_model,
                 ttft_ms, latency_ms, sources, web_searches, created_at
llm_calls        titles and summaries (their cost belongs to no single message)
archived_replies the usage numbers of replies whose chat was deleted (never their text)
```

A message is a **process**, not a fact: while the model is writing, its row has `status = 'streaming'`. It ends as `complete`, `cancelled` (you pressed Stop) or `error`. You will see this state machine in the next lessons.

## Design principles you will see everywhere

:::keypoints
- **One job per layer, with walls.** The model layer is the only code that imports `openai`.
- **The database is the source of truth.** Chats survive refreshes and restarts because nothing important lives only in memory or in the browser.
- **Fail early, with a message that says what to fix.** Bad configuration crashes at startup, not on the first user's first message.
- **Never invent a number.** A cost that cannot be known is `NULL` and shows as "cost n/a", never `$0`. Token counts are the provider's reported numbers, never estimates from text length.
- **Errors shown to users are safe.** Raw exceptions can leak URLs, request ids and keys. The browser sees a short fixed sentence; the details go to the server log.
- **Test without the network.** A scripted stand-in plays the model, so tests are fast, free and deterministic.
:::

```quiz
? Why is the empty assistant row created BEFORE the model is called?
- To reserve a database id.
+ So that a crash or a Stop mid-stream still leaves a record with a status and any partial text, instead of the reply simply vanishing.
- Because SQLite needs the row to exist before it can be updated.
> With the row in place, startup recovery can mark orphaned `streaming` rows as `error`, and Stop can save the partial answer.

? Where does the conversation history sent to the model come from?
- The browser sends it with every request.
+ The server rebuilds it from the database for every request.
- OpenAI stores it and the server asks for it.
> The model is stateless and the browser is untrusted, so the server reads the stored messages itself (and sends `store=False` so OpenAI keeps nothing).

? Why must request validation happen before the stream starts?
- Validation is slow.
+ Once streaming begins the HTTP status 200 is already sent, so any later failure can only be reported as an event inside the stream.
- FastAPI cannot validate inside a generator.
> That is why 404, 422, 413, 429 and 409 are all raised before `StreamingResponse` is returned.
```

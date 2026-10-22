---
title: Testing an LLM app without the network
level: medium
time: 35 min
summary: Test doubles for the model and the SDK, a safety net that makes accidental real calls fail, the tests that guard the hard-won rules, what was verified against the real API, and what the tests do not prove.
tags: testing, pytest, test doubles, stubs, Vitest, real-API verification
---

Testing an LLM app has two awkward problems. The model is **slow, costs money and gives different answers each time**, and so is the network. If every test called OpenAI, the suite would be slow, expensive and flaky. Streambot's answer is to test *everything around the model* thoroughly and cheaply, and to verify the model integration against the real API only occasionally, by hand, with a hard cost ceiling.

At the time of writing there are **more than 440 backend tests** (pytest, about 25 seconds, running against a real PostgreSQL) and **70 frontend tests** (Vitest). **None of them calls OpenAI or Tavily.**

## The strategy: one wall, two stand-ins

The "wall" around OpenAI (the model layer lesson) pays off here. Because the pipeline depends only on a small `LLM` protocol, tests can swap in two different stand-ins, each testing a different layer.

:::flow Which stand-in tests which layer
Real SDK | never used in tests
SDK stub | a pretend OpenAI client: tests OUR adapter (`llm/client.py`) against the SDK's event shapes
ScriptedLLM | a pretend model: tests the pipeline, the routes, memory and cost end to end
Pure functions | no stand-in needed: history, memory planning, pricing, the usage report, the parsers
:::

### The scripted model

```python src="AI/tests/helpers.py" fn="ScriptedLLM,ok_script"
```

A **script** is a list of events and exceptions: items that are events are yielded, and items that are exceptions are *raised at that point*. So "stream `Hello `, `world`, then crash" is a one-liner: deterministic, free, and instant. It also records what the app sent it (`histories`, `tools_seen`, `cache_keys`, `summary_requests`), which lets tests assert on the *request* as well as the response.

### The SDK stub

To test the adapter itself, a tiny fake mimics only the parts of the OpenAI client the code touches:

```python src="AI/tests/test_llm.py" fn="StubStream,StubResponses,client_with"
```

With it, a test can feed the adapter any sequence of SDK events, including ones that raise, and check how `client.py` maps them.

## A real database, and fakes for everything that costs money

The model and the network are replaced; **the database is not**. Constraints, cascades, the pgvector indexes and the hybrid search query are *the database's* behaviour, so a mock would prove nothing. Instead, every test gets its **own throwaway PostgreSQL database**, cloned from a template that has all migrations applied (cloning is a file copy inside Postgres, tens of milliseconds):

```python src="AI/tests/conftest.py" fn="database_url,db"
```

For the document features there are three more doubles. `FakeEmbedder` returns deterministic vectors (hashed words) and can be told to fail; `make_pdf` and `make_docx` build **real** files in memory, so the real parsers run; and the OpenAI embedder itself is tested against a stub SDK client, like the chat adapter.

```python src="AI/tests/helpers.py" fn="FakeEmbedder"
```

## A safety net, not just good intentions

Good intentions do not stop a future change from letting a real client slip through and quietly spend money. So there is a fixture that makes it **fail loudly**:

```python src="AI/tests/conftest.py" fn="forbid_real_network"
```

It is `autouse`, so it applies to every test. If anything tries to send a real HTTP request (OpenAI's SDK client, or the exchange-rate lookup), the test fails with a clear message instead of silently costing money.

## Tests that guard the hard-won rules

Every rule in this guide has a test that would fail if the rule were broken. A few worth reading:

**Never retry after the first token** (the model layer lesson):

```python src="AI/tests/test_llm.py" fn="test_failure_after_first_token_is_not_retried"
```

**The summary boundary moves in big steps** (the memory lesson). Right after summarising, adding a few messages must *not* trigger another summary:

```python src="AI/tests/test_memory.py" fn="test_the_boundary_moves_in_big_steps_not_every_turn"
```

**Billing never uses a text-length estimate.** This test *reads the source code* of the billing modules and fails if the estimate ever appears in them:

```python src="AI/tests/test_usage_cost.py" fn="test_billing_code_contains_no_text_length_estimation"
```

**Deleting a chat does not change spend history** (a bug found by a user, then guarded):

```python src="AI/tests/test_usage_report.py" fn="test_a_deleted_chats_usage_stays_on_the_day_it_happened"
```

## What each test file proves

| File | Covers |
|---|---|
| `test_history.py` | the budget is respected, newest preferred, failed/empty skipped, starts with a user turn |
| `test_memory.py` | when to summarise (hysteresis), history with a summary, the background manager: merge, failure, concurrency, deleted chat |
| `test_llm.py` | the adapter against the SDK stub: request shape, event mapping, error classification, the **agent loop**, summed usage, cache key |
| `test_retry.py` | retry before the first token, none after, bounded, backoff, `Retry-After` |
| `test_streaming_failure.py` | a stream failing mid-way keeps the partial text, no leaked internals, cancellation, checkpointing |
| `test_api.py` | the endpoints, history reaching the model, titles, 413/422/409/429, restart persistence, web-search events, deleting a chat, `/usage` |
| `test_cancellation_live.py` | a **real** Uvicorn server and a **real TCP disconnect** |
| `test_usage_cost.py`, `test_pricing.py` | exact cost arithmetic, cached and reasoning tokens, unknown prices |
| `test_usage_report.py` | totals, cache stats, time zones, deleted chats, unpriced calls |
| `test_web_search.py` | Tavily parsing, URL filtering, safe errors, citation numbering |
| `test_db.py`, `test_migrations.py` | constraints, crash recovery, summaries, archive-on-delete; the **migration runner** (order, only-missing, rollback of a failing file) |
| `test_auth.py`, `test_cli.py` | passwords, JWT (expiry, tampering, `alg: none`), sign-up/login, a walk of **every route** proving it needs a token, users isolated from each other; the admin commands |
| `test_rag_extract.py`, `test_rag_chunker.py` | reading PDF/Word/text safely and refusing disguised files, zip bombs, encrypted or empty PDFs; chunk size, overlap, pages and headings |
| `test_rag_store.py`, `test_rag_ingest.py` | chunks and **hybrid search on real pgvector**, isolation between chats and users, cascades, atomic writes; background indexing outcomes, the OpenAI embedder, the document tool |
| `test_attachments_api.py` | upload/list/delete over HTTP, every limit and refusal, another user's access, and a full "ask about the file, get a cited answer" round trip |
| `test_rag_quality.py` | a small **gold set**: hit@k and MRR for hybrid vs vector-only retrieval |
| `test_limits_config.py`, `test_fx.py`, `test_sse.py` | rate limiter, configuration, exchange rate, the wire format |

The live cancellation test deserves a mention. In-process tests cannot prove that a server notices a client hanging up, so this one starts a genuine server, opens a streaming request, **closes the socket mid-stream** (exactly what `AbortController.abort()` does in a browser), and checks the database and that the model stream was closed.

## The frontend tests

```text
sse.test.ts     the parser fed one character at a time, split in the middle of a terminator, CRLF, comments
cite.test.ts    [1] and [2][3] become links only for real sources, never inside code
auth.test.ts    token expiry read without verifying, storage that may throw, the 401 hook
documents.test.ts  which files are accepted, list updates, status text, real file drags only
math.test.ts    \( \) and \[ \] rewritten, code left alone, dollar amounts untouched
chart.test.ts   axis maths, label thinning, tiny values stay visible
format.test.ts  money formatting: USD and INR side by side, "cost n/a" for unknown
```

These are the *pure* parts of the client. Browser end-to-end checks (Playwright driving the real UI against a real backend and a real PostgreSQL, with a scripted model and the fake embedder) were run by hand for sign-up and login, isolation between two accounts, streaming, Stop, delete, citations, and the whole attachment flow (refused file, draft chat, drag and drop, status, a cited answer, removal), but they are not part of the committed suite.

## Verified against the real API, by hand

The stand-ins prove the code handles the *shapes* the SDK produces. They cannot prove the real API still behaves that way. So each integration was verified once against the real API, with a strict ceiling on calls (ten allowed; eight used in total):

| Check | Result |
|---|---|
| streaming through the whole stack | real `delta` events, exact usage, a `done` event, rows persisted |
| web search | the model searched **twice on its own**, got 10 sources, answered and titled the chat |
| rolling memory | the real model summarised, then **merged** new messages into the summary |
| prompt caching | the same prompt twice: **2,439 of 2,442** tokens cached the second time, about **86% cheaper** |
| **attachments (embeddings)** | **not yet verified.** Everything around the embedding call is tested with stand-ins, but the real `text-embedding-3-small` call, its reported usage and the price entry have not been exercised against the live API |

:::note Stand-ins prove the adapter, not the model
None of this proves the model gives *good* answers. For that you need **evaluations** ("evals"): a fixed set of questions scored automatically or by a judge model. Streambot has a small **retrieval** gold set (does the right passage come back? see the retrieval lesson), but it runs with a stand-in embedder, so it checks the plumbing, not OpenAI's embedding quality. It has **no answer-quality evals**, and saying so is part of being honest about what is and is not proven.
:::

## What is not tested

- **Answer quality** (no evals), and **retrieval quality with the real embedding model** on real documents.
- **The real embedding API** end to end (the next verification to do, and a cheap one).
- **Load and concurrency** at scale (it is a single-process app).
- **A committed end-to-end browser suite**.
- **Failures that only happen against the real API**: a genuine 429 or a mid-stream network drop are simulated, not observed.

## Run them

```bash
docker compose up -d postgres                             # the tests use a real PostgreSQL
cd AI && source .venv/bin/activate && pytest -q          # backend: no OpenAI, about 25 seconds
cd frontend && npm run typecheck && npm test              # frontend: types, then the unit tests
```

:::keypoints
- Test **everything around the model** thoroughly and cheaply; verify the real integration rarely, by hand, with a cost ceiling.
- Two stand-ins: an **SDK stub** (tests the adapter) and a **scripted model** (tests the pipeline).
- A scripted model's events and **exceptions** make failure cases one-liners.
- Add a **safety net** (`autouse` fixture) so an accidental real call fails loudly.
- Every hard-won rule has a test; some tests even **read the source** to enforce a rule.
- A **live** test (real server, real socket) is the only way to prove disconnect handling.
- Use a **real database** for anything the database enforces; fake only what costs money or needs the network.
- State what the tests do **not** prove: answer quality needs **evals**, and an unverified integration should be called unverified.
:::

:::exercise Write the test first
You decide titles must be at most 60 characters. Which existing test file would you extend, what would the test assert, and why is the function easy to test?
:::

:::solution Show the answer
`test_llm.py` already has a parametrised `test_clean_title` covering `clean_title`. Add a case with a 200-character string and assert `len(result) == 60`. It is easy to test because `clean_title` is a **pure function**: a string in, a string out, no network, no database, no model.
:::

```quiz
? Why does Streambot have an autouse fixture that blocks real network calls in tests?
- To make tests faster.
+ So a future change cannot silently let a real OpenAI call (and its cost) slip into the test suite; it fails loudly instead.
- Because OpenAI forbids testing.
> It turns "we try not to" into "it cannot happen".

? A test needs to simulate "the model streams 40 words, then the connection drops". What is the simplest tool?
- Call the real API and unplug the network.
+ A scripted model whose script lists the 40 words as events followed by an exception.
- Mock the database.
> Scripts yield events and raise exceptions at the exact point you choose.

? Which statement is true about the stand-in tests?
- They prove the model gives good answers.
+ They prove our code handles the shapes the SDK produces, but not the model's quality.
- They replace the need to ever use the real API.
> That is why the integration was also verified once, by hand, against the real API.
```

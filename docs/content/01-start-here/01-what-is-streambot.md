---
title: What Streambot is
level: easy
time: 15 min
summary: What the app does, what it is built with, a tour of the repository, and how to run it. Start here.
tags: overview, FastAPI, React, OpenAI, SSE
---

**Streambot** is a chat application in the style of ChatGPT, built from scratch to understand how a real LLM product works. You type a question, the answer appears word by word, you can press **Stop**, your chats are saved, and the app can **search the web** and **cite its sources**. Underneath, it tracks every token and every rupee, remembers long conversations by summarising them, and uses OpenAI's prompt cache to keep the bill small.

It is deliberately *not* built on LangChain or any agent framework. The OpenAI SDK is used directly, so every important step is visible in the code, and every step is explained in this guide.

:::analogy A restaurant, not a recipe
A model API is like a very talented chef who works in a sealed kitchen and forgets everyone the moment a dish leaves. Streambot is **the restaurant around that chef**: it takes the order (your message), reminds the chef what was said earlier (history and memory), passes dishes out as they are ready (streaming), sends a runner to buy missing ingredients (web search), keeps the bill (cost tracking), and makes sure one bad evening does not close the place (retries, error handling, tests). The chef is one part. The restaurant is the product.
:::

## What it can do

| Feature | What you see | Where it is explained |
|---|---|---|
| **Streaming replies** | words appear live, with a blinking cursor | [Streaming with SSE](#/l/streaming-sse) |
| **Stop button** | the reply halts, the partial answer is kept | [Streaming with SSE](#/l/streaming-sse) |
| **Saved chats** | a sidebar of conversations that survives a refresh or a server restart | [Persistence](#/l/persistence) |
| **Long conversations** | the app keeps context by summarising old messages | [Rolling memory](#/l/rolling-memory) |
| **Web search with citations** | "Searching the web for…", numbered source chips, source cards | [Tool calling](#/l/tool-calling), [Web search and citations](#/l/web-search-citations) |
| **Per-reply cost** | input tokens, output tokens, cost in dollars and rupees | [Usage and cost](#/l/usage-and-cost) |
| **Prompt caching** | "saved ₹0.04" on the reply, a cache hit rate | [Caching and the Usage page](#/l/caching-and-usage-page) |
| **Usage page** | spend over time, by purpose, by model, and your costliest chats | [Caching and the Usage page](#/l/caching-and-usage-page) |
| **Markdown, code, maths** | tables, syntax-coloured code with Copy buttons, rendered equations | [The React client](#/l/react-client) |
| **Delete a chat** | removes the content, keeps the spend history true | [Persistence](#/l/persistence) |

## What it is built with

| Layer | Technology | Why this one |
|---|---|---|
| Model API | **OpenAI Responses API** (the `openai` Python SDK) | streaming events, tool calling and prompt caching in one API |
| Server | **FastAPI** + **Uvicorn** (Python 3.10+) | async, so one slow model call never blocks other users; automatic validation with Pydantic |
| Streaming | **Server-Sent Events (SSE)** over plain HTTP | one-way streaming that works through any proxy and can be read by eye |
| Storage | **SQLite** (Python's built-in `sqlite3`) | zero setup, one file, ACID, plenty for a single-user app |
| Web search | **Tavily** | a search API built for LLMs: clean snippets, simple JSON |
| Client | **React 19** + **TypeScript** + **Vite** | typed events end to end, fast dev loop |
| Rendering | `react-markdown`, `remark-gfm`, `remark-math` + KaTeX, `rehype-highlight` | safe Markdown, tables, equations and syntax colours |
| Tests | **pytest** (+ pytest-asyncio) and **Vitest** | over 250 backend tests and about 40 frontend tests, none of which call OpenAI |

:::note What "from scratch" means here
There is no framework between you and the model. When you read `AI/app/llm/client.py` you see the actual request sent to OpenAI, the actual stream of events that comes back, and the actual retry logic. That is the point: LLM frameworks are wrappers around exactly these steps, and once you know the steps, any framework is easy to read.
:::

## A tour of the repository

```text
Streaming-Chat-App/
├── AI/                         the Python service
│   ├── app/
│   │   ├── main.py             HTTP routes (thin)
│   │   ├── config.py           every setting, read once from .env
│   │   ├── schemas.py          the shapes of request and response bodies
│   │   ├── db.py               SQLite: schema and every query
│   │   ├── chat/
│   │   │   ├── streaming.py    THE pipeline: model events -> database -> SSE
│   │   │   ├── history.py      what the model gets to see (token budget)
│   │   │   ├── memory.py       the rolling summary
│   │   │   ├── sse.py          the SSE wire format
│   │   │   └── usage_report.py the numbers for the Usage page
│   │   ├── llm/
│   │   │   ├── client.py       the ONLY module that talks to OpenAI
│   │   │   ├── usage.py        token usage as the provider reported it
│   │   │   ├── pricing.py      tokens -> money, and what the cache saved
│   │   │   └── accounting.py   recording side calls (titles, summaries)
│   │   ├── tools/
│   │   │   ├── web_search.py   Tavily search
│   │   │   └── registry.py     the tool definition and per-reply citation numbering
│   │   └── core/
│   │       ├── limits.py       rate limit and request-size cap
│   │       └── fx.py           the USD -> INR display rate
│   ├── data/                   prices.toml (you fill it in) and chat.db (created on first run)
│   └── tests/                  every behaviour above, with no network
├── frontend/src/               the React app (Chat, Message, Usage, sse.ts, api.ts ...)
├── docs/                       this guide
├── run.sh                      starts the backend and the frontend together
└── .env                        your keys and settings (git-ignored, never committed)
```

## Run it

You need Python 3.10+ and Node 20+. Create `.env` in the repository root with at least an OpenAI key and a model name, then:

```bash
./run.sh
```

That creates the virtual environment on first run, validates your configuration (a clear error if the key or model is missing), and starts the backend on **http://localhost:8501** and the frontend on **http://localhost:3100**. Starting the app makes no OpenAI request; only sending a message does.

:::warn Messages cost real money
Each message you send calls the real OpenAI API. Streambot has no fake model any more (it had one while it was being built, and it was removed). The cost is shown under every reply so you always know what a conversation costs.
:::

## How to read this guide

The lessons go from the outside in: first the **architecture** (how the pieces fit), then the **LLM concepts** every chat app needs (tokens, statelessness, streaming), then the harder layers (**memory**, **tool calling**, **caching and cost**), then the **React client**, then **testing**.

Every code block is extracted from the repository when this guide is built, by function name, so what you read is what runs. If a function is renamed and this guide still refers to the old name, the build fails instead of showing stale code. Blocks marked **▶ Run** execute the app's real functions right here in your browser; edit them and run again.

:::keypoints
- Streambot is a ChatGPT-style app: streaming, saved chats, web search with citations, rolling memory, cost tracking and prompt caching.
- It uses the OpenAI SDK directly (no framework), FastAPI on the server, SQLite for storage and React on the client.
- The model is only one component. Most of the engineering is the product around it.
- Everything in this guide is quoted from the real code.
:::

```quiz
? Why does Streambot call the OpenAI SDK directly instead of using a framework like LangChain?
- Frameworks cannot stream responses.
+ So every step (the request, the event stream, the retries) is visible in the code and can be explained.
- The OpenAI SDK is the only one that supports tools.
> Frameworks wrap the same steps. Seeing the steps directly is the way to understand any framework afterwards.

? Which statement about starting the app is true?
- Starting the app sends a test message to OpenAI.
+ Starting the app makes no OpenAI request; only sending a chat message does.
- The app needs a fake model to start.
> Configuration is validated at startup, but no model call is made until a user sends a message.
```

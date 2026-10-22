---
title: Prompt caching and the Usage page
level: medium
time: 45 min
summary: How OpenAI's prompt cache cuts the bill, what keeps it working (an identical prefix), how Streambot measures what it saved, and how the Usage page is computed and drawn.
tags: prompt caching, prefix, cache key, usage report, time zones, charts, dashboard
---

Every time Streambot sends a message it re-sends the *whole conversation* (the model is stateless). Most of that is identical to the previous request: the same instructions, the same old messages, with one new message at the end. **Prompt caching** is the provider noticing that, and charging much less for the repeated part.

## How prompt caching works

:::def Prompt caching
The provider remembers the processed **beginning** (prefix) of recent prompts. If a new request starts with exactly the same tokens, it reuses that work: it is **faster** and the repeated tokens are billed at a **much lower rate**. It is automatic for long prompts (about 1,024 tokens and up), and applies only to an **exact prefix match**.
:::

The three words that matter are *exact*, *prefix* and *long enough*. The cache matches from the very first token and stops at the first difference. Everything *before* the first difference is cached; everything after is not.

:::analogy The bookmark in a book
Imagine the model reading your prompt like a book, with a bookmark that remembers how far it already read last time. If you only add a new page at the end, it resumes from the bookmark. But if you change a single word on page one, the bookmark is useless and it must read everything again.
:::

Run this illustration. It measures how much of a new prompt matches the previous one, from the start:

```python run title="Why an exact prefix is everything (illustration)" level=easy
def cached_prefix(previous, new):
    """How many leading words are identical: what a prefix cache can reuse."""
    n = 0
    for a, b in zip(previous.split(), new.split()):
        if a != b:
            break
        n += 1
    return n

instructions = "You are a helpful assistant. Answer clearly. " * 3
turn_1 = instructions + "User: hello  Assistant: hi!  User: what is SSE?"
turn_2 = turn_1 + "  Assistant: a streaming format.  User: and WebSockets?"   # append-only

print("append-only history     :", cached_prefix(turn_1, turn_2), "of", len(turn_2.split()), "words reused")

# One harmless-looking change at the START destroys the cache for everything after it:
turn_2_with_clock = "Current time: 18:52:07. " + turn_2
print("timestamp put first     :", cached_prefix(turn_1, turn_2_with_clock), "words reused")

# Changing the instructions between turns (for example toggling a feature) also misses:
turn_2_new_rules = turn_2.replace("Answer clearly.", "Answer in one line.")
print("instructions changed    :", cached_prefix(turn_1, turn_2_new_rules), "words reused")
```

## What Streambot does to keep the cache working

The request is deliberately assembled in a stable order, from least to most likely to change:

```text
static instructions  ->  static tool definitions  ->  rolling summary  ->  append-only history  ->  the new message
(never changes)          (never changes)               (changes rarely)    (only grows at the end)
```

Each part earns its place:

- **No timestamps or per-request data in the instructions**, ever.
- The **tool definitions** are a stable list, identical on every request *of a given chat*. One honest wrinkle: the `search_documents` tool is only offered once a chat has a ready attachment, so the list changes **once** (the first request after a file becomes ready) and the cache for that chat misses that time. After that it is stable again. Putting the tools *before* the history is what keeps that a one-off, not a per-message cost.
- The **summary** only changes when the boundary moves, in *big, rare steps* (the hysteresis from the memory lesson exists partly for this).
- **History is append-only**: older messages never change, new ones are added at the end.

And one request parameter helps the provider route requests to the same cache:

```python src="AI/app/llm/client.py" from="# Prompt caching is automatic for long prompts" to='extra["prompt_cache_key"] = cache_key'
```

The cache key is the **conversation id**, so all turns of one chat share it. There is also a test that proves the prefix property: turn 2's input starts with exactly turn 1's, and the instructions are identical.

:::note What can still miss
Toggling web search on or off changes the instructions and tools (the guidance text is added only when tools are on), so the first message after a toggle will not hit the cache. A chat that is idle for long enough also loses its cache. Both are normal and cheap.
:::

## Proof from the real API

This was verified against the real OpenAI API, not assumed. The same prompt of about 2,400 tokens was sent twice, a few seconds apart, with the same cache key:

| Call | Input tokens | Cached tokens | Cost |
|---|---|---|---|
| 1 (cold) | 2,442 | **0** | $0.000511 |
| 2 (same prefix) | 2,442 | **2,439** | $0.000070 |

The second call was about **86% cheaper**, and 2,439 of 2,442 tokens (99.9%) came from the cache. The three tokens that did not are the ones after the last cache boundary.

## Measuring what the cache saved

Caching is invisible unless you measure it. The saving per reply is what the cached tokens *would* have cost at the normal rate minus what they actually cost:

$$\text{savings} = \frac{\text{cached} \times (p_{in} - p_{cached})}{10^6}$$

```python src="AI/app/llm/pricing.py" fn="cache_savings"
```

Reproduce the table's saving from the real numbers:

```python run include="AI/app/llm/pricing.py:_PER,ModelPrice,cache_savings" title="What did the cache save? (the app's own function)" level=easy
LUNA = ModelPrice(input_per_million=0.20, output_per_million=1.20, cached_input_per_million=0.02)

print("saved:", cache_savings(LUNA, input_tokens=2442, cached_input_tokens=2439))   # 2439 x (0.20 - 0.02) / 1e6
print("nothing cached:", cache_savings(LUNA, input_tokens=2442, cached_input_tokens=0))
print("no cached price known:", cache_savings(ModelPrice(0.2, 1.2), input_tokens=100, cached_input_tokens=40))  # None: unknown
print("never more than what was cached:", cache_savings(LUNA, 100, 5000) == cache_savings(LUNA, 100, 100))
```

The saving is **informational**: it is shown ("saved ₹0.04") but never subtracted from the cost again, because the cost already charged the cached part at the cheap rate.

## The Usage page: where the numbers come from

The page answers four questions: *how much did I spend, on what, when, and how much did caching save?* Everything is computed from rows the app already stored, **for the signed-in user only**; nothing is estimated.

Three sources are combined:

```python src="AI/app/db.py" fn="Database.usage_rows"
```

1. **Reply rows** in `messages` (each carries its own tokens and cost),
2. **Side calls** in `llm_calls` (titles, summaries and document embeddings),
3. **Archived replies** from deleted chats (so deleting a chat never changes your spend).

Then one function folds them into the report:

```python src="AI/app/chat/usage_report.py" fn="build_usage_report"
```

Things worth noticing:

- **Unpriced calls are counted, not hidden.** They are excluded from the money totals and reported as `unpriced_calls`, so the page can say "spend is a lower bound".
- **Days follow *your* clock.** The browser sends its UTC offset, and the server buckets each call into the user's local day. Timestamps are stored in UTC, so without this the late-evening replies in India would land on tomorrow's bar.
- **The daily series is continuous**: days with no activity are present as zeros, so the chart has no gaps.
- **Cache hit rate** is `cached / input` over the period, and the savings use each call's own model price.

Time zones are a classic bug, so here is the idea in isolation:

```python run title="Bucketing a UTC timestamp into the user's day (illustration)" level=easy
from datetime import datetime, timedelta, timezone

stored = datetime.fromisoformat("2026-10-21T23:00:00.000+00:00")   # 11 pm UTC on the 21st

for label, offset_minutes in [("UTC", 0), ("India (UTC+5:30)", 330), ("New York (UTC-4)", -240)]:
    local = stored + timedelta(minutes=offset_minutes)
    print(f"{label:18} -> {local.date()}")
```

## Drawing it

The chart is drawn by hand with HTML and CSS, with no chart library, following a few simple rules:

| Choice | Reason |
|---|---|
| **One colour** for daily spend | it is one series; colour should carry meaning, not decoration |
| **Spend by purpose** also uses one colour | the categories (replies, titles, summaries, document indexing) have no natural order, so a colour ramp would invent one |
| Thin columns, 2px gaps, hairline gridlines | the data is the only thing allowed to be loud |
| Only the **peak** is labelled | a number on every bar is noise; the tooltip carries the rest |
| A **tooltip on hover *and* keyboard focus**, and a **table view** | nothing is reachable only by hovering |
| While refetching, the old chart stays, dimmed | no layout jump or flash |

The axis maths are small pure functions with their own tests:

```ts src="frontend/src/chart.ts" fn="niceMax,axisTicks,barPercent,labelIndexes"
```

`niceMax` rounds the top of the axis up to a clean number (1, 2, 2.5, 5 × a power of ten), `axisTicks` spaces labels evenly, and `labelIndexes` thins the date labels so they never collide, always keeping the last day.

### A bug worth learning from

Costs here are tiny and long (`$0.006922`). In a fixed-width tile, the big hero figure was clipped by its neighbour. The fix is a CSS trick: let each figure size itself to **the width of its own tile**, using container query units:

```ts src="frontend/src/Usage.tsx" fn="fit"
```

```css src="frontend/src/styles.css" from=".fit {" to="}"
```

`--len` is the number of characters and `--max` the largest allowed size. `100cqw` is the container's width, so the font size becomes "the biggest size at which this many characters fit". It was tested at six screen widths and four amounts, from `$0.000069` to `$1234.5`, with zero overflows.

## What gets measured, and what does not

The Usage page reports what **OpenAI reported**. It does not include Tavily's charges (searches are *counted* but not priced), and a model with no entry in `prices.toml` is unpriced. A page that says what it leaves out is more trustworthy than one that pretends to be complete.

:::keypoints
- **Prompt caching** discounts the repeated *exact prefix* of a prompt (long prompts only). Anything changed near the start loses the rest.
- Streambot orders the request **stable first**: instructions, tools, summary, then append-only history, and sends a **cache key** (the conversation id).
- Verified live: the second identical call had 2,439 of 2,442 tokens cached and cost about **86% less**.
- **Savings** = cached × (input rate − cached rate) / 10⁶: informational, never subtracted twice.
- The Usage page combines replies, side calls and archived replies; it counts **unpriced** calls honestly and buckets days in **the user's time zone**.
- Charts follow rules: one colour for one series, thin marks, selective labels, a tooltip *and* a table, and figures that **fit their tile**.
:::

:::exercise Break the cache on purpose
In the prefix illustration, put the user's name at the very start of the instructions ("Hello Mohit. You are a helpful assistant…") and change it between two turns. How much of the second prompt can be reused? Where *should* per-user information go instead?
:::

:::solution Show the answer
Almost nothing can be reused, because the first words differ and the match stops there. Per-user or per-request information belongs **at the end** of the prompt (in the newest message or after the stable parts), so the long, stable beginning stays identical and cached.
:::

```quiz
? What must be true for a prompt cache hit?
- The same user sends the request.
+ The new prompt starts with exactly the same tokens as a recent one (an exact prefix), and it is long enough.
- The response must be identical.
> The cache matches from the first token and stops at the first difference.

? Why is the cache key set to the conversation id?
- So conversations are encrypted.
+ It groups the requests of one chat, helping the provider route them to the same cache.
- Because OpenAI requires a unique key per message.
> All turns of one chat share a prefix, so they should share a key.

? A user in India sends a message at 00:30 local time. Why must the server use the user's UTC offset?
- To translate the message.
+ Timestamps are stored in UTC, so without the offset the reply would be counted on the previous day's bar.
- To save space.
> Buckets are the user's local days; the browser sends its offset with the request.
```

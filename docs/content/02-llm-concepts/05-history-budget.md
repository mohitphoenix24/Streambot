---
title: History and the token budget
level: easy
time: 30 min
summary: How the app decides which past messages the model gets to see: the rules for what counts, the newest-first walk, and the edge cases that make it robust.
tags: history, token budget, context window, truncation, pure functions
---

Every request must carry the conversation (the model is stateless), but a conversation grows without limit, and every token you resend costs money and time. So the app sends only as much recent history as fits a **budget**.

:::analogy Packing a suitcase with a weight limit
You pack the things you need *most* first (the latest messages), and when the bag is full the oldest items stay behind. You do not leave a sock out of the middle of the pile: you stop at the first thing that does not fit. And whatever else you pack, the bag must always contain the question you are asking right now.
:::

## Two numbers, easy to confuse

| Number | Whose limit | Default |
|---|---|---|
| **Context window** | the *model's* hard limit on input + output in one request | depends on the model |
| **`HISTORY_BUDGET_TOKENS`** | *our* deliberately smaller limit for the past-messages part | 6000 |

We spend less than the window on history because the window must also hold the instructions, the new message and the reply itself. History is the only part the budget caps.

## What counts as history

Not every stored message should be sent to the model. The first filter is a tiny function:

```python src="AI/app/chat/history.py" fn="_eligible,eligible_messages"
```

- **`error`** replies are skipped: feeding a broken, half-written answer back as if the assistant had said it would confuse the model.
- **`streaming`** replies (still being written, or orphaned by a crash) are skipped.
- **Empty** messages carry no information.
- **`complete` and `cancelled`** are kept. A stopped answer is still something the user *saw*, so it is part of what was said.

## The newest-first walk

```python src="AI/app/chat/history.py" fn="build_history"
```

The algorithm in plain words:

:::flow build_history, step by step
Filter | keep only eligible messages
Skip | drop everything the rolling summary already covers (next lesson)
Walk | go from the NEWEST message backwards, adding messages while they fit the budget
Stop | at the first message that does not fit, stop (never skip it and keep older ones)
Fix | if the newest user message alone exceeds the budget, still send it
Open | the list must start with a USER turn; drop an orphaned leading assistant answer
Return | the messages in chronological order, plus whether anything was dropped
:::

Each rule exists for a reason:

- **Newest first**, because recent turns matter most to the next answer; when something must be dropped, it is the oldest.
- **Stop at the first misfit** rather than skipping it. Skipping a large message and keeping older ones would leave a **hole in the middle** of the conversation, and the model would see replies to questions it cannot see.
- **Always keep the newest user message**, even if it alone is over budget. Sending *nothing* would make the model answer a question it never saw. Its size is capped separately by `MAX_MESSAGE_CHARS` (default 8000).
- **Start with a user turn.** If the budget cut landed between a question and its answer, the list would open with an orphaned assistant message. That is dropped.

## Try it

This is the app's real `build_history`, running in your browser. Change the budget, add an `error` message, or make the last message huge and watch what happens.

```python run include="AI/app/chat/history.py:approx_tokens,HistoryMessage,History,_eligible,eligible_messages,summary_message,build_history" title="build_history: the budget in action" level=easy
def msg(i, role, text, status="complete"):
    return {"id": f"m{i}", "role": role, "content": text, "status": status}

# 6 question/answer pairs of ~100 characters each (about 29 estimated tokens each)
messages = []
for i in range(6):
    messages.append(msg(2 * i,     "user",      f"question {i} ".ljust(100, ".")))
    messages.append(msg(2 * i + 1, "assistant", f"answer {i} ".ljust(100, ".")))

def show(budget, rows=messages):
    h = build_history(rows, budget_tokens=budget)
    kept = [m["content"][:10].strip(" .") for m in h.messages]
    print(f"budget {budget:5}: kept {len(h.messages):2} messages, truncated={h.truncated}, ~{h.approx_tokens} tokens")
    print("            ", kept)

show(10_000)     # everything fits
show(150)        # only the most recent ones fit; the oldest are dropped
show(60)         # even fewer, but it always starts with a USER turn

# A failed reply is never sent back to the model:
with_error = messages[:-1] + [msg(99, "assistant", "half an ans", status="error")]
print("\nwith a failed last reply:")
show(10_000, with_error)

# The newest user message is ALWAYS kept, even if it alone is over budget:
huge = messages + [msg(100, "user", "x" * 8000)]
print("\nwith a giant last message and a tiny budget:")
show(100, huge)
```

## `approx_tokens`, again

The whole budget runs on the rough estimate from the concepts lesson, `len(text)//4 + 4`. It only needs to be *roughly* right, because the budget leaves a safety margin below the context window, and billing never uses it. The upgrade path is to drop in the model's real tokenizer behind the same function signature.

## Truncation is now the safety net

Plain truncation forgets the oldest messages. For a long chat that loses facts ("my name is Mohit", "the deadline is Friday"). Streambot therefore tries something smarter first: it **summarises** the oldest messages and sends the summary plus the recent ones. Truncation remains underneath as a guarantee: if the summary is late, missing, or the recent part is still too large, the budget is still respected.

:::keypoints
- The model is stateless, so history is resent each turn; a **budget** keeps cost and latency bounded.
- Only messages the user actually saw count: `complete` and `cancelled` yes, `error`, `streaming` and empty no.
- Walk **newest to oldest**, stop at the **first misfit** (no holes), always keep the **newest user message**, always **start with a user turn**.
- `build_history` is a **pure function**: rows in, messages out. No HTTP, no database, no OpenAI, which makes it trivial to test.
- The budget is an estimate; billing uses the provider's exact usage.
:::

:::exercise Skipping instead of stopping
Suppose a 4,000-token message sits in the middle of a conversation and the budget is 5,000. A "skip the big one and keep going" version would keep more messages. Describe what the model would see, and why Streambot refuses to do it.
:::

:::solution Show the answer
The model would see the recent messages, then a gap where the big message was removed, then older messages: replies to a question it can no longer see, and a question with no reply. The conversation would not make sense. Stopping at the first message that does not fit guarantees the model sees one **continuous** recent stretch of the conversation.
:::

```quiz
? Which stored messages are NOT sent back to the model?
- Cancelled replies.
+ Failed (`error`) replies, replies still `streaming`, and empty messages.
- User messages.
> A stopped answer was seen by the user so it stays; a broken partial answer would only confuse the model.

? Why does `build_history` stop at the first message that doesn't fit instead of skipping it?
- It is faster.
+ Skipping would leave a hole in the middle of the conversation.
- The budget is measured in messages.
> The model must see one continuous recent stretch of the conversation.

? What happens when the newest user message alone is bigger than the budget?
- The request is rejected.
- The message is cut in half.
+ It is still sent, because an empty prompt would be worse.
> Its size is already capped separately by `MAX_MESSAGE_CHARS`.
```

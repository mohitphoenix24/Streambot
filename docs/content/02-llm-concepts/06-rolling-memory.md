---
title: Rolling-summary memory
level: medium
time: 40 min
summary: How long conversations keep their context: fold the oldest messages into a running summary, only in big rare steps, in the background, without ever blocking or breaking a chat.
tags: memory, summarisation, hysteresis, background tasks, asyncio, prompt stability
---

Plain truncation has a nasty flaw. Once a chat grows past the budget, the oldest messages are simply **forgotten**, including facts that still matter: "my name is Mohit", "the deadline is Friday", "I prefer short answers". Ask about them 40 messages later and the model has no idea.

:::analogy Meeting minutes
Nobody re-reads a three-hour meeting transcript before the next meeting. They read the **minutes** (a short summary of everything decided so far) plus the notes from the last few minutes. Streambot gives the model exactly that: a summary of the old part of the chat, then the recent messages word for word.
:::

```text
old messages ──summarise──▶ [summary]  +  recent messages (verbatim)  ──▶ LLM
```

## The design in five decisions

1. **Summarise only when needed**, and then in *big, rare steps* (not every turn).
2. **Do it in the background**, after a reply has finished, so it can never delay an answer.
3. **Never let it break a chat.** If the summary call fails, keep the old summary and carry on.
4. **Never delete or change messages.** Only *what is sent to the model* changes.
5. **Merge, don't restart.** Each new summary updates the previous one with the newly old messages.

## When to summarise: hysteresis

It is tempting to summarise a little on every turn. That would be wasteful (one extra model call per message) and, more subtly, it would **change the start of the prompt every time**, which wrecks prompt caching (see [Prompt caching](#/l/caching-and-usage-page): caching needs an *identical* prefix).

So the rule has two thresholds, like a thermostat:

| Threshold | Value | Meaning |
|---|---|---|
| start summarising | when the not-yet-summarised messages exceed **80 %** of the history budget | the chat is getting long |
| stop folding | when only about **40 %** of the budget remains verbatim | leave plenty of recent context |

With the default budget of 6000 tokens: nothing happens until the unsummarised part passes 4,800 tokens; then the oldest messages are folded until about 2,400 tokens remain word for word. The summary boundary moves in **big, rare steps**, and *between* steps the start of the prompt is byte-for-byte identical.

:::def Hysteresis
A rule with two different thresholds for "start" and "stop", so the system does not flip back and forth around a single line. Here it prevents the summary boundary from moving on every message.
:::

## The planning function

Deciding *what* to fold is a **pure function**: rows in, plan out. No database, no model, no HTTP.

```python src="AI/app/chat/memory.py" fn="SUMMARIZE_ABOVE,KEEP_RECENT,SummaryPlan,plan_summary"
```

Read it against the table above. `pending` is everything after the last summarised message. If it is under 80 % of the budget, return `None` (nothing to do). Otherwise walk back from the newest message to find the "recent" share that stays verbatim (40 %), make sure that part **opens with a user turn**, and fold everything before it.

Here it is running. This simulation grows a chat one question-and-answer pair at a time, calls `plan_summary` after each pair, and pretends the model has summarised whenever the plan says so. Watch *when* it triggers:

```python run include="AI/app/chat/history.py:approx_tokens,_eligible,eligible_messages|AI/app/chat/memory.py:SUMMARIZE_ABOVE,KEEP_RECENT,SummaryPlan,plan_summary" title="plan_summary: a chat growing past the budget" level=medium
BUDGET = 1000               # a small budget so the effect shows quickly (the app default is 6000)
messages, through_id, i = [], None, 0

def add_pair():
    global i
    messages.append({"id": f"m{2*i}",   "role": "user",      "content": f"question {i} ".ljust(100, "."), "status": "complete"})
    messages.append({"id": f"m{2*i+1}", "role": "assistant", "content": f"answer {i} ".ljust(100, "."),   "status": "complete"})
    i += 1

for turn in range(1, 41):
    add_pair()
    plan = plan_summary(messages, through_id, BUDGET)
    if plan:
        through_id = plan.through_id
        done = [m["id"] for m in messages].index(through_id) + 1
        kept = len(messages) - done
        print(f"turn {turn:2}: SUMMARISE  -> folded {len(plan.messages):2} messages "
              f"(summary now covers the first {done}); {kept} stay verbatim")

# 40 turns = 80 messages, but the model was asked to summarise only a handful of times,
# and the boundary moved in big steps. Try BUDGET = 6000 (the real default).
```

:::note Why an unsummarised chat is not the problem
Even before the first summary, the history is still within budget (the truncation safety net sees to that). The summary exists so that what *falls off the end* is remembered, not forgotten.
:::

## How the history uses the summary

`build_history` (previous lesson) accepts the summary and the id of the last message it covers. It sends a **summary message first**, then only the messages after that boundary:

```python src="AI/app/chat/history.py" fn="summary_message"
```

The role is `developer`: in the Responses API it carries standing context from the application, not something the user said. The summary also **counts against the budget**, and the verbatim part still always opens with a user turn.

## The manager: a background task with good manners

```python src="AI/app/chat/memory.py" fn="MemoryManager.schedule,MemoryManager.run,MemoryManager._update"
```

Every detail here is deliberate:

- **`asyncio.create_task`**, not `await`: the reply has already finished; the summary runs on the side. The task is stored in `self._tasks` because the event loop keeps only a *weak* reference to tasks, and a task nobody holds can be garbage collected before it finishes.
- **One lock per conversation.** If two replies finish close together, only one summarises; the other waits, re-checks, and finds nothing to do.
- **`run` never raises.** Memory is a best-effort extra, not something allowed to break a chat. A failure is logged and the old summary stays.
- **`_update` re-checks the chat exists** after the (slow) model call, because you may have deleted the chat while it was summarising.
- **The cost is recorded** (`record_llm_call`), so summaries show up on the Usage page.

## Where it is triggered

```python src="AI/app/chat/streaming.py" from="if memory is not None:" to="memory.schedule(conversation_id)"
```

It is scheduled *before* the first `yield` of the `done` event on purpose: a client that hangs up the instant it receives `done` must not stop the summary from being kept up to date.

## The summarising call

The model is told exactly what to keep:

```python src="AI/app/llm/client.py" fn="SUMMARY_INSTRUCTIONS,OpenAILLM.summarize"
```

Notice the prompt design. It lists what matters (names, preferences, decisions, file names, numbers, TODOs), what to drop (small talk, superseded facts), a length limit, and the rule **"Never invent details"**. It receives the *current summary* plus the *new messages*, so it **merges** rather than starting again. Each message is truncated to 4,000 characters so one giant paste cannot make the summary call huge, and the output cap is generous (1,500 tokens) because reasoning models spend output tokens thinking.

### A real run

This is not a mock-up. The real model was given a short chat, then a second batch of messages to merge:

```text
First summary:
- User's name: Mohit.
- Building a chat app called **Streambot** using **FastAPI** and **React**.

After merging the next messages:
- User's name: Mohit.
- Building a chat app called **Streambot** using **FastAPI** and **React**.
- User wants rolling-summary memory.
- User's deadline is **Friday**.
- User prefers **short answers**.
```

The name, project, deadline and style preference all survived the merge, in under 300 words.

## Trade-offs, honestly

| Benefit | Cost |
|---|---|
| keeps long-range facts in a few tokens | an extra model call each time the boundary moves |
| old messages are never deleted | a summary is **lossy**: it can drop or distort a detail |
| runs in the background | the summary lags: it is updated *after* a reply, used on the *next* turn |

It can be switched off (`MEMORY_SUMMARY_ENABLED=false`), and a different, cheaper model can write the summaries (`OPENAI_SUMMARY_MODEL`, defaulting to the title model). The UI says "earlier messages are summarised so the model keeps the context" so the user knows.

:::keypoints
- Truncation forgets facts. A **rolling summary** keeps them: `[summary] + recent messages`.
- **Hysteresis** (start at 80 %, cut to 40 %) moves the summary boundary in big, rare steps, which also keeps the prompt prefix stable for caching.
- `plan_summary` is a **pure function**: easy to test, no side effects.
- The work is a **background task**, with a lock per chat, that **never raises** and never changes stored messages.
- Each summary **merges** into the previous one, via a prompt that says what to keep and "never invent details".
- Summaries are lossy and lag one turn behind: say so, and keep truncation as the safety net.
:::

:::exercise Change the thresholds
In the simulation, set `BUDGET = 6000` and run it. How many messages does it take before the first summary? Then think: what would change if `KEEP_RECENT` were 0.8 instead of 0.4?
:::

:::solution Show the answer
At 6000 tokens the threshold is 4,800; each pair is about 58 tokens, so the first summary happens after roughly 83 pairs. With `KEEP_RECENT = 0.8` the recent share would be almost as big as the trigger, so each summary would fold only a few messages and would trigger *again* very soon: the "big, rare steps" benefit disappears, and the prompt prefix changes much more often.
:::

```quiz
? Why does Streambot wait until 80% of the budget before summarising instead of summarising every turn?
- The model cannot summarise short chats.
+ An extra call per message is wasteful, and changing the start of the prompt every turn would defeat prompt caching.
- Summaries are free only above 80%.
> Hysteresis moves the boundary rarely, so between steps the prompt prefix is identical.

? What happens if the summary call fails?
- The chat shows an error.
+ The old summary is kept and the chat carries on; truncation still bounds the prompt.
- The conversation is deleted.
> Memory is best-effort and `run` never raises.

? Why is the summary task stored in a set (`self._tasks`)?
- To count them.
+ The event loop holds only a weak reference to tasks, so an unreferenced task could be garbage collected before it finishes.
- To cancel them on startup.
> Keeping a strong reference until the task is done is the standard `create_task` pattern.
```

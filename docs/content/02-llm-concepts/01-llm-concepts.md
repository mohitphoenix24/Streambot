---
title: Tokens, statelessness and the request
level: easy
time: 30 min
summary: What a model actually is from an app's point of view, what a token is, why every request carries the whole conversation, and the exact request Streambot sends to OpenAI.
tags: tokens, context window, statelessness, Responses API, usage
---

Every LLM app, however fancy, rests on a few facts about the model. If you hold these in your head, almost every design choice in Streambot will feel obvious.

## Fact 1: the model is stateless

:::def Stateless
A stateless service remembers nothing between calls. Every request must contain everything the service needs. An LLM API is stateless: it has no memory of your previous question unless you send it again.
:::

When you ask ChatGPT "What is my name?" two messages after saying "I am Mohit", it can answer only because the app **re-sent your whole conversation** with the new question. The model itself learned nothing and kept nothing.

```text
request 1:  [user: My name is Mohit.]
request 2:  [user: My name is Mohit., assistant: Nice to meet you., user: What is my name?]
            -> "Your name is Mohit."
```

So "memory" in a chat product is something the *app* does: store the messages, and send the relevant ones each time. Streambot stores them in PostgreSQL and rebuilds the list on every request. That is the whole of [History and the token budget](#/l/history-budget), and the smarter version is [Rolling-summary memory](#/l/rolling-memory).

:::analogy The goldfish consultant
Imagine hiring a brilliant consultant with a three-second memory. To get a useful answer you hand over a folder with the whole case file every single time. A good assistant app is the person who keeps that folder tidy: not too thick (cost and speed), not missing anything important (quality).
:::

## Fact 2: models read and write tokens, and tokens cost money

:::def Token
The unit a model reads and writes. Not a word and not a character: a chunk of text, often a short word or part of one. In English, roughly 4 characters or three-quarters of a word. Code, numbers, emoji and non-English text usually take more tokens per character.
:::

You are billed per **million tokens**, separately for what you **send** (input) and what the model **writes** (output). Output is several times more expensive than input. A request has these counts, all reported by the provider afterwards:

| Count | Meaning |
|---|---|
| `input_tokens` | everything you sent: instructions + history + the new message (it **includes** the cached part) |
| `cached_input_tokens` | the part of the input served from OpenAI's prompt cache, billed at a lower rate (a **subset** of input) |
| `output_tokens` | everything the model generated (it **includes** reasoning tokens) |
| `reasoning_tokens` | tokens a "reasoning" model spent thinking before the visible answer (a **subset** of output, billed as output) |
| `total_tokens` | input + output |

:::warn Subsets are not extra
`cached_input_tokens` is *inside* `input_tokens`, and `reasoning_tokens` is *inside* `output_tokens`. A classic bug is adding them on top again and double-charging. Streambot's cost code treats them as subsets, and has a test for exactly this.
:::

Streambot needs to know roughly how big a message is *before* sending it (to fit a budget), but it cannot call the model just to count. So it uses a cheap estimate. Run it and see how rough it is:

```python run include="AI/app/chat/history.py:approx_tokens" title="Estimating tokens (the app's own function)" level=easy
samples = {
    "short English": "Hello, how are you today?",
    "a longer sentence": "Streaming makes an answer feel instant because the first words arrive immediately.",
    "code": "def build_history(messages, budget_tokens): return messages[-budget_tokens:]",
    "Hindi": "नमस्ते, आप कैसे हैं? मैं आज बहुत अच्छा महसूस कर रहा हूँ।",
    "numbers": "3.14159265358979323846264338327950288419716939937510",
}
for name, text in samples.items():
    print(f"{name:20} {len(text):4} chars  ->  ~{approx_tokens(text):3} tokens")

# Try it: the estimate is len(text)//4 + 4. A real tokenizer would count Hindi and
# long numbers as MORE tokens per character. That is why this is only used for budgeting,
# never for billing.
```

:::note An estimate for budgeting, the real numbers for billing
The estimate decides what fits in the history budget. **Billing never uses it.** Cost is computed from the `usage` object OpenAI returns, which is exact. Mixing the two up is a common mistake, and Streambot has a test that scans the billing modules to make sure the estimate is never used there.
:::

## Fact 3: there is a limit on how much the model can read

:::def Context window
The maximum number of tokens the model can handle in a single request, input and output together. Send more and the request fails (`context_length_exceeded`).
:::

Streambot keeps its own, **smaller** budget for the history part, because the window must also hold everything else:

$$\text{context window} \;\ge\; \text{instructions} + \text{HISTORY\_BUDGET} + \text{new message} + \text{MAX\_OUTPUT\_TOKENS}$$

The setting `HISTORY_BUDGET_TOKENS` (default 6000) is *not* the model's window. It is "how much of the past we are willing to pay to resend".

## Fact 4: the request has a few well-defined parts

Streambot uses OpenAI's **Responses API**. Here is the request for a reply, in the order the app builds it. Do not worry about every line yet; each parameter is explained below and again in the lesson where it matters.

```python src="AI/app/llm/client.py" from="stream = await self._client.responses.create(" to="**extra,"
```

| Parameter | What it is | Why Streambot sets it |
|---|---|---|
| `model` | which model to use | a **setting**, never hard-coded: model ids change and differ per account |
| `instructions` | the **system prompt**: standing rules for the model's behaviour | kept apart from the conversation turns; when tools are on, the tool guidance is appended |
| `input` | the conversation so far (our history, converted) | the whole point of statelessness |
| `max_output_tokens` | hard ceiling on what the model may generate | bounds both **cost** and **latency** |
| `store=False` | tell OpenAI not to keep the conversation | the app owns the history in its own database |
| `stream=True` | send the answer as a stream of events | words appear live ([Streaming with SSE](#/l/streaming-sse)) |
| `prompt_cache_key` | groups requests that share a prompt prefix | makes the prompt cache hit ([Prompt caching](#/l/caching-and-usage-page)) |
| `tools`, `include` | tool definitions, and encrypted reasoning for multi-step tool use | web search ([Tool calling](#/l/tool-calling)) |

:::def System prompt (instructions)
A message from the developer, not the user, that sets the model's behaviour: its role, tone and rules. In the Responses API it is the `instructions` parameter. The user cannot see or edit it.
:::

Our history is a list of plain dictionaries, and one small function converts it to the provider's format. Today the two shapes are identical, and that is exactly why the function exists: the day a provider wants a different shape, only this function changes.

```python src="AI/app/llm/client.py" fn="to_openai_input"
```

## Fact 5: what comes back

With `stream=True` the SDK gives a stream of typed events. Streambot cares about three:

| Event | Meaning |
|---|---|
| `response.output_text.delta` | a new piece of text (usually a token or a few characters) |
| `response.completed` / `response.incomplete` | the model is done; this event carries the final **usage** |
| `response.failed`, `error` | the generation broke |

Everything else (`response.created`, `output_item.added`, …) is bookkeeping we ignore on purpose.

The final event also says **why the model stopped**, and the app must handle each reason:

```python src="AI/app/llm/client.py" fn="_stop_reason"
```

`completed` is a normal finish. `max_output_tokens` means *we* cut the answer off with our own limit, and the UI must say "Response was cut off" so the user does not think the model finished its thought.

Finally the usage object is turned into our own small record. This is the code that makes the app honest: if OpenAI reports no usage, the result is `None`, and the UI shows "usage unavailable" instead of an invented number.

```python run include="AI/app/llm/usage.py:log,Usage,_get,_count,parse_usage" title="Parsing the provider's usage (the app's own functions)" level=easy
from types import SimpleNamespace as NS

# What the OpenAI SDK returns, as plain objects:
raw = NS(
    input_tokens=1200, output_tokens=300, total_tokens=1500,
    input_tokens_details=NS(cached_tokens=1024),
    output_tokens_details=NS(reasoning_tokens=250),
)
usage = parse_usage(raw)
print(usage)
print("billed as cached:", usage.cached_input_tokens, "of", usage.input_tokens, "input tokens")
print("reasoning is INSIDE output:", usage.reasoning_tokens, "of", usage.output_tokens)

# Missing optional fields are None ("unknown"), which is not the same as 0:
print(parse_usage(NS(input_tokens=10, output_tokens=5)))
# And no usage at all stays None; nothing is ever invented:
print(parse_usage(None))
```

## Reasoning models

Some models "think" before answering. Those thinking tokens are **not shown to the user but are billed as output tokens**. Two practical consequences appear in the code:

- A small `max_output_tokens` can be eaten entirely by thinking, leaving an *empty* answer. That is why the title call uses a cap of 128 instead of something tiny, and the summary call uses 1500.
- Reasoning tokens are stored for observability only (`reasoning_tokens` column) and are **never** added to the cost again, because they are already part of `output_tokens`.

:::keypoints
- A model is **stateless**: the app re-sends the conversation every time, and "memory" is the app's job.
- **Tokens** are the unit of both cost and limits. Input and output are billed separately; cached input is cheaper; reasoning tokens are part of output.
- The **context window** is the model's limit. `HISTORY_BUDGET_TOKENS` is our smaller, deliberate budget for the past-messages part.
- The request has a few clear parts: `model`, `instructions`, `input`, `max_output_tokens`, `store=False`, `stream=True`.
- Use **estimates for budgeting** and the **provider's reported usage for billing**. Never mix them.
- Handle **why the model stopped**: `max_output_tokens` must be shown to the user.
:::

:::exercise Predict the budget
A conversation has 20 messages of about 400 characters each. Using `approx_tokens`, roughly how many tokens is that? With `HISTORY_BUDGET_TOKENS = 6000`, will all 20 fit? Edit the estimate demo above to check your answer.
:::

:::solution Show the answer
Each 400-character message is about `400 // 4 + 4 = 104` tokens, so 20 messages are about 2,080 tokens, far below 6000: they all fit. You would need about 58 such messages before the budget starts dropping or summarising anything.
:::

```quiz
? A user says "I am Mohit" and two messages later the model knows their name. Why?
- The model remembers earlier requests.
- OpenAI stores the conversation on its side.
+ The app sent the earlier messages again along with the new question.
> Models are stateless. Streambot also sets `store=False`, so OpenAI keeps nothing at all.

? Which is true about `cached_input_tokens`?
- It is added on top of `input_tokens`.
+ It is a subset of `input_tokens`, billed at a lower rate.
- It counts tokens the model generated from cache.
> Adding it again would double count. The cost formula charges the non-cached part at the normal rate and the cached part at the cached rate.

? Why does Streambot use `len(text)//4 + 4` for budgeting but never for billing?
- Billing is done by the browser.
+ It is only a rough estimate, while billing needs the exact `usage` the provider reports.
- The tokenizer is not installed.
> An estimate is fine for deciding what fits. For money, only exact reported numbers are acceptable.
```

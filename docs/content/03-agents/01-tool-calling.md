---
title: Tool calling and the agent loop
level: medium
time: 45 min
summary: How a model that cannot browse the web ends up searching it: function calling, the loop that runs tools and asks again, and why every detail (reasoning items, retries, usage) needs care.
tags: tool calling, function calling, agent loop, encrypted reasoning, JSON Schema, parallel calls
---

A language model only produces text. It cannot browse the web, check today's date, run code, or look at your database. **Tool calling** is how an app gives it those abilities. The model never runs anything itself. It *asks*, and the app does the work.

:::def Tool calling (function calling)
A protocol where you describe functions to the model (a name, a description and a JSON Schema for the arguments). When the model decides one would help, instead of text it returns a **function call**: the name plus arguments as JSON. Your code runs the function and sends the **result** back, and the model continues with that new information.
:::

:::analogy A consultant with a phone
The model is a consultant locked in a room with no internet. It can slide notes under the door. You give it a card: "If you need facts, write `SEARCH: <query>` and I will phone the research desk." It writes `SEARCH: latest Python release`; you phone the desk, slide the answer back under the door, and the consultant writes its reply using it. The consultant never dials. You do.
:::

## From a single call to an agent loop

Without tools, one request produces one answer. With tools, the answer may need *several* requests:

:::flow The agent loop
Ask | send the conversation, the instructions and the tool definitions
Think | the model replies with TEXT (done) or with a FUNCTION CALL
Run | the app runs the tool and captures the result (a search, in our case)
Return | the call's output is appended to the input and the model is asked again
Repeat | until a round ends with text and no function call: that round is the answer
:::

:::def Agent
A program that lets a model decide *what to do next* in a loop, using tools, until it has what it needs to answer. The word covers a huge range, but the core is always this loop: model → tool → model → … → answer.
:::

## Describing a tool

The model learns what a tool does from its **description and schema**. They are effectively a prompt:

```python src="AI/app/tools/registry.py" fn="WEB_SEARCH,WEB_SEARCH_GUIDANCE"
```

- `type: "function"` and `name` identify it.
- `description` tells the model *when* it is useful. Models pick tools based on this text, so it is worth writing carefully.
- `parameters` is a **JSON Schema**: one required string, `query`. With `strict: True` and `additionalProperties: False`, the model is held to exactly that shape.
- `WEB_SEARCH_GUIDANCE` is added to the system prompt **only when tools are enabled**. It says when to search (recent events, versions, prices) and when not (greetings, arithmetic), and how to cite.

## The request grows `tools` and `include`

When tools are on, the request gets two extra parameters:

```python src="AI/app/llm/client.py" from="extra: dict[str, Any] = {}" to='extra["include"] = ["reasoning.encrypted_content"]'
```

The second one deserves explanation. Streambot sets `store=False`, so **OpenAI keeps nothing** between requests. But a reasoning model builds up internal reasoning during a round, and to continue sensibly after a tool call it needs that reasoning back. `include=["reasoning.encrypted_content"]` asks OpenAI to hand the reasoning back (encrypted, so you cannot read it), and the app sends it in the next round. Without it, the model would "forget what it was thinking" between the tool call and the answer.

## The loop, in code

```python src="AI/app/llm/client.py" fn="OpenAILLM._stream_agent"
```

Read it as the diagram above, line by line:

1. `input_items` starts as our history. Each loop pass is **one streamed request** (a *round*); text deltas are forwarded as they arrive.
2. When a round ends, `end.calls` holds any function calls the model made. If there are none, **this round was the answer**: yield `Completed` and return.
3. Otherwise the model's output items (including the encrypted reasoning) are appended to `input_items`, and each call is run: `ToolCallStarted` → run the tool → `ToolCallFinished`.
4. Each call's result is appended as a `function_call_output` item, **linked to the call by `call_id`**. The model needs an output for *every* call it made.
5. The `while True` goes round again.

To see how the input list grows, here is the same loop with a pretend model and a pretend search. This is an illustration, not the app's code, but the **shape of `input_items`** is exactly what is sent to OpenAI:

```python run title="Illustration: how the input grows across rounds" level=medium
import json

def fake_model(input_items):
    """Pretend model: searches once, then answers using the result."""
    already_searched = any(i.get("type") == "function_call_output" for i in input_items)
    if not already_searched:
        return [{"type": "reasoning", "id": "rs_1", "encrypted_content": "<opaque>"},
                {"type": "function_call", "call_id": "call_1", "name": "web_search",
                 "arguments": json.dumps({"query": "latest stable Python release"})}]
    return [{"type": "message", "text": "The latest stable release is Python 3.15.1 [1]."}]

def fake_search(query):
    return "[1] Python Releases (https://www.python.org/downloads/)\nPython 3.15.1 is the latest stable release."

input_items = [{"role": "user", "content": "What is the latest stable Python?"}]
for round_no in range(1, 5):
    output = fake_model(input_items)
    calls = [o for o in output if o["type"] == "function_call"]
    print(f"--- round {round_no}: the model returned {[o['type'] for o in output]}")
    if not calls:                                   # no function call: this round IS the answer
        print("ANSWER:", output[-1]["text"])
        break
    input_items.extend(output)                      # the model's own items go back (incl. reasoning)
    for call in calls:
        result = fake_search(json.loads(call["arguments"])["query"])
        input_items.append({"type": "function_call_output", "call_id": call["call_id"], "output": result})

print("\nThe final input list had", len(input_items), "items:")
for item in input_items:
    print("  ", item.get("type") or item.get("role"))
```

## Turning SDK objects into plain dicts

The model's output items must go back as input. The SDK returns typed objects; the app converts them to plain dictionaries and picks out the function calls:

```python src="AI/app/llm/client.py" fn="_output_items,_call_query"
```

`model_dump(exclude_none=True, by_alias=True)` produces a clean dictionary (dropping empty fields, and honouring aliases like `async`). The function calls are collected as `{call_id, name, arguments}` and the arguments stay a JSON *string* until something needs the query.

## Why retries wrap a round, not the loop

Retries (see [The model layer](#/l/llm-layer)) protect each *round*:

```python src="AI/app/llm/client.py" from="async with aclosing(" to="base_delay=self._retry_base_delay,"
```

And tools run **only after a round has completed**, outside the retry wrapper. Together these mean **a retried request can never repeat a search**. If round 2 hits a network blip before its first token, round 2 is retried, and the tool that ran after round 1 is not run again. There is a test for exactly this: three requests, one search.

## Usage across rounds

A tool-using reply makes several requests, each billed. The usage is **summed**, so the reply's cost covers everything:

```python src="AI/app/llm/usage.py" fn="Usage"
```

```python run include="AI/app/llm/usage.py:Usage" title="Summing usage across rounds (the app's own class)" level=medium
round_1 = Usage(input_tokens=900,  output_tokens=60,  total_tokens=960,  cached_input_tokens=0,    reasoning_tokens=40)
round_2 = Usage(input_tokens=1400, output_tokens=210, total_tokens=1610, cached_input_tokens=1024, reasoning_tokens=None)

total = round_1 + round_2
print(total)
# cached/reasoning stay None ONLY when neither round reported them:
print((Usage(5, 5, 10) + Usage(5, 5, 10)).cached_input_tokens)
```

There is one more honest detail in the loop: if **any** round reports no usage at all, the total is `None` ("unknown") rather than a partial sum, because a partial sum would quietly under-report the cost.

## Several calls at once

A model can ask for **several tools in one round** ("parallel tool calls"). The loop runs them one after another and appends an output for each call id. The API requires an output for every call the model made, so none may be skipped. A test covers this.

## There is no search limit

Streambot deliberately sets no cap on how many searches a reply may make; the model keeps going until it has enough to answer. Two things still bound it: each round is limited by `MAX_OUTPUT_TOKENS`, and the **Stop** button cancels the reply and the model call at any moment. The honest risk is cost: every extra round resends the growing conversation plus all the search results. A cheap safety net (for example a very high ceiling to catch a runaway loop) is a sensible addition for a public deployment.

## How it surfaces in the UI

Two events join the stream, and the pipeline checkpoints the sources to the database the moment each search returns, so a crash would still remember what the answer was going to cite:

| SSE event | Payload | The browser shows |
|---|---|---|
| `tool_call` | `{id, name, query}` | "Searching the web for “…”" |
| `tool_result` | `{id, name, sources, error}` | "Searched “…” · 5 sources", or the error |

:::keypoints
- **Tool calling**: you describe functions; the model returns a *call*; your code runs it and sends the result back. The model never runs anything.
- The **agent loop** is: ask → function call? → run → append output → ask again → … → text answer.
- A tool's **description and JSON Schema** are effectively a prompt: write them with care.
- With `store=False`, reasoning must travel back: `include=["reasoning.encrypted_content"]`.
- Retries wrap **one round**, and tools run **after** a round completes, so a retry never repeats a search.
- **Sum usage** over rounds (and treat a missing round as unknown, not zero).
- Every function call needs a `function_call_output` with the same `call_id`.
:::

:::exercise Add a calculator tool
Suppose you wanted the model to be able to do exact arithmetic. List the changes you would make: where is the tool defined, where is it run, and does the loop in `_stream_agent` need to change?
:::

:::solution Show the answer
Add a second definition (name, description, a schema with an `expression` string) to the `definitions` list in `tools/registry.py`, and a branch in `ToolRunner.run` that evaluates the expression **safely** (never `eval` on user-influenced text; use a small parser). The loop in `_stream_agent` needs **no change at all**: it runs whatever calls the model makes through `tools.run(name, arguments)`. That is the benefit of the `ToolExecutor` protocol.
:::

```quiz
? What does the model return when it wants a tool to be used?
- It runs the tool itself.
- It returns the tool's result.
+ A function call: the tool's name and its arguments as JSON.
> Your code runs the function and sends the result back with the same `call_id`.

? Why is `include=["reasoning.encrypted_content"]` needed here?
- To make answers longer.
+ With `store=False` OpenAI keeps nothing, so the reasoning from one round must be passed back for the model to continue properly.
- To encrypt the API key.
> The reasoning comes back encrypted (unreadable to you) and goes into the next request.

? Why can a retried request never run the same web search twice?
- Searches are cached.
+ Tools run only after a round has completed, outside the retry wrapper that protects each individual round.
- OpenAI de-duplicates tool calls.
> Retries re-open a single round; the tool call that finished before it is not re-run.
```

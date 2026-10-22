---
title: Usage, pricing and cost accounting
level: medium
time: 40 min
summary: How reported token usage becomes exact money, why costs use Decimal, why an unknown price is None and never zero, and how titles, summaries and the rupee display are handled.
tags: cost, pricing, Decimal, NULL, usage, exchange rate
---

An LLM bill is the sum of thousands of tiny charges, and a cost display that is *slightly wrong* destroys trust. Streambot therefore treats money with the same care as the streaming code: exact arithmetic, honest unknowns, and no guessing.

## The data flow

:::flow From tokens to rupees
Provider | OpenAI reports exact `usage` for the reply
Parse | `parse_usage` turns it into our `Usage` record (missing fields become None)
Price | `PriceTable` finds the model's price in `prices.toml` and applies the formula
Store | `finalize_complete` writes the tokens and cost on the assistant message row
Show | the UI displays USD, and an INR equivalent beside it
:::

## The formula

Providers quote **USD per one million tokens**, with a lower rate for cached input and a higher one for output:

$$\text{cost} = \frac{\text{non\_cached} \times p_{in} + \text{cached} \times p_{cached} + \text{output} \times p_{out}}{10^6}, \qquad \text{non\_cached} = \text{input} - \text{cached}$$

Two details are easy to get wrong, and both are tested:

- `input_tokens` **already includes** the cached tokens. Charge the cached part at the cached rate and only the **rest** at the normal rate, never both.
- `output_tokens` **already includes** reasoning tokens. They are billed as output, so they are not added again.

## Why `Decimal`

Floating-point numbers cannot represent most decimal fractions exactly, and cost sums are full of tiny fractions:

```python run title="Why money is not a float" level=easy
from decimal import Decimal

print(0.1 + 0.2)                                   # 0.30000000000000004
print(Decimal("0.1") + Decimal("0.2"))             # 0.3

# 1,000,000 uncached tokens at $0.20 per million, summed token by token:
print(sum(0.20 / 1_000_000 for _ in range(1_000_000)))
print(sum(Decimal("0.20") / 1_000_000 for _ in range(1_000_000)))
```

The pricing module therefore calculates in `Decimal` and converts to `float` only at the edge (JSON and SQLite's `REAL`).

## The code

```python src="AI/app/llm/pricing.py" fn="ModelPrice,CostBreakdown,cache_savings,calculate_cost"
```

Read `calculate_cost` for the guard rails: if cached tokens exceed input tokens it clamps (and logs), and if cached tokens were reported but the model has **no cached price**, it returns `None`: it would have to *guess* the cached rate, and the app refuses to guess.

Here it is, with the real price of `gpt-5.6-luna` ($0.20 input, $0.02 cached input, $1.20 output per million). Change the token counts and watch each part of the bill:

```python run include="AI/app/llm/pricing.py:_PER,log,ModelPrice,CostBreakdown,cache_savings,calculate_cost" title="calculate_cost with real prices (the app's own function)" level=medium
LUNA = ModelPrice(input_per_million=0.20, output_per_million=1.20, cached_input_per_million=0.02)

# The real reply from a test run: 2,442 input tokens, of which 2,439 came from the cache, and 17 output tokens.
c = calculate_cost(LUNA, input_tokens=2442, cached_input_tokens=2439, output_tokens=17)
print(f"non-cached input : ${c.input_cost:.8f}")
print(f"cached input     : ${c.cached_input_cost:.8f}")
print(f"output           : ${c.output_cost:.8f}")
print(f"TOTAL            : ${c.total_cost:.8f}")
print(f"saved by the cache: ${c.cache_savings:.8f}   (informational: never subtracted again)")

# The same call with no cache hit, for comparison:
cold = calculate_cost(LUNA, input_tokens=2442, cached_input_tokens=0, output_tokens=17)
print(f"\nwithout the cache : ${cold.total_cost:.8f}  ({cold.total_cost / c.total_cost:.1f}x as much)")

# A model with NO cached price can't price cached tokens, so the cost is unknown, not guessed:
no_cache_price = ModelPrice(input_per_million=0.20, output_per_million=1.20)
print("\ncached tokens but no cached price ->", calculate_cost(no_cache_price, input_tokens=100, cached_input_tokens=50, output_tokens=5))
```

## Prices live in a file, not in code

```toml title="AI/data/prices.toml"
[models."gpt-5.6-luna"]
input        = 0.20    # USD per 1M non-cached input tokens
cached_input = 0.02    # USD per 1M cached input tokens (optional)
output       = 1.20    # USD per 1M output tokens
```

Prices change and differ by account, so they are **configuration**. The file ships with *no invented prices*: you fill in what your account actually pays. If a model has no entry, its cost is `None` and the UI says "cost n/a", never `$0`.

## Which price applies? Aliases and snapshots

You ask for `gpt-x`, and OpenAI may serve a dated snapshot `gpt-x-2026-03-17`. The table decides which entry applies with four rules, in order:

```python src="AI/app/llm/pricing.py" fn="PriceTable.resolve,PriceTable.cost"
```

The rule worth remembering is the last: if the served model is something *else entirely*, there is no price: `None`. Better a blank than a wrong number.

```python run include="AI/app/llm/usage.py:log,Usage|AI/app/llm/pricing.py:_PER,log,ModelPrice,CostBreakdown,cache_savings,calculate_cost,PricingError,PriceTable" title="Which price applies? (the app's own PriceTable)" level=medium
table = PriceTable({"gpt-x": ModelPrice(1.0, 4.0, 0.1)})
usage = Usage(input_tokens=1000, output_tokens=100, total_tokens=1100)

for served in [None, "gpt-x", "gpt-x-2026-03-17", "gpt-x-mini", "some-other-model"]:
    cost = table.cost("gpt-x", served, usage)
    print(f"served={served!s:18} -> price key: {table.resolve('gpt-x', served)!s:6}  cost: {cost.total_cost if cost else None}")
```

## Saving the numbers

Everything is stored on the assistant row, next to the text it belongs to, so "what did this reply cost?" is a column read and "what did this conversation cost?" is a `SUM`:

```python src="AI/app/chat/streaming.py" fn="finalize_complete"
```

Notice the comment at the bottom of its docstring: *the streamed text is only the text to display. Token counts and cost never come from its length.* A test scans the billing modules to make sure no text-length estimate ever creeps in.

## Side calls have a cost too

A title and a summary are separate model calls that belong to no single message. They are recorded in their own table, with their own usage and cost, so no spend is invisible:

```python src="AI/app/llm/accounting.py" fn="usage_columns,record_llm_call"
```

## Rupees: a display conversion, not a charge

OpenAI bills in USD, so **USD is the stored, real amount**. The UI shows an INR equivalent beside every cost. The exchange rate changes, so it is fetched, cached and never allowed to break the app:

```python src="AI/app/core/fx.py" fn="UsdInrProvider"
```

Three modes, set by `USD_TO_INR`: `auto` (fetch the live rate from a free public API, cache it six hours, fall back to a configured rate if the lookup fails), a fixed number, or `0` to hide INR. A lock ensures only **one** fetch happens at a time, not one per request, and the `except Exception` is deliberate: *never break the app over a display nicety*.

The browser formats the two currencies side by side:

```ts src="frontend/src/format.ts" fn="formatCost,formatInr,formatMoney"
```

Notice `formatCost`: costs are fractions of a cent, so it shows six decimals under one cent, and `"cost n/a"` for `null`.

:::keypoints
- Cost comes from the provider's **reported usage** and a **price table you maintain**, never from text length.
- Cached tokens are a **subset** of input and reasoning tokens a **subset** of output; charge each part once.
- Use **`Decimal`** for money and convert to float only at the edges.
- An unknown price is **`None`**, shown as "cost n/a", never `$0`; `None` is also used when cached tokens cannot be priced.
- Titles and summaries are recorded in **`llm_calls`** so no spend is hidden.
- INR is a **display conversion**; the stored amount is USD. The rate is fetched, cached, locked against stampedes, and falls back gracefully.
:::

:::exercise Compute by hand
A reply used 10,000 input tokens (6,000 cached) and 500 output tokens on `gpt-5.6-luna`. Compute the cost by hand, then check with the demo. How much did caching save?
:::

:::solution Show the answer
Non-cached input is 4,000 tokens: 4,000 × $0.20 / 1M = $0.0008. Cached input: 6,000 × $0.02 / 1M = $0.00012. Output: 500 × $1.20 / 1M = $0.0006. Total ≈ **$0.00152**. Without the cache the 6,000 tokens would have cost 6,000 × $0.20 / 1M = $0.0012, so caching saved $0.0012 − $0.00012 = **$0.00108**.
:::

```quiz
? Why does the pricing code use Decimal instead of float?
- Decimal is faster.
+ Floats cannot represent most decimal fractions exactly, and cost sums are full of tiny fractions.
- OpenAI requires it.
> Decimal avoids noise like 0.30000000000000004; conversion to float happens only at the edge.

? A model has no entry in prices.toml. What does the UI show for its cost?
- $0.
- The cost of the cheapest model.
+ "cost n/a", because the cost is None (unknown).
> A wrong number in a cost display is worse than an honest blank.

? Reported usage: input 1000 (400 cached), output 200 (150 reasoning). How many output tokens are billed?
- 350
+ 200
- 50
> Reasoning tokens are already inside the output count; adding them would double count.
```

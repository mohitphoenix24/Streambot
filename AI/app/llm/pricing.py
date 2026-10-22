"""Actual token usage -> money.

    OpenAI API -> actual usage -> usage parser -> [this module] -> database -> UI

    non_cached_input = input_tokens - cached_input_tokens

    cost =  non_cached_input   x input_price        / 1,000,000
          + cached_input       x cached_input_price / 1,000,000
          + output_tokens      x output_price       / 1,000,000

Providers quote USD per MILLION tokens, with a lower rate for cached input (the
part of the prompt served from OpenAI's prompt cache) and a higher rate for output.

Two details that are easy to get wrong:
  * `input_tokens` already INCLUDES the cached tokens. Charge the cached part at
    the cached rate and only the REST at the normal input rate (never both).
  * `output_tokens` already INCLUDES reasoning tokens. They are billed as output,
    so they are not added on top again (double counting).

Prices live in prices.toml, not in code (they change, and differ per model and
account). If we have no price we return None ("unknown"), never a guess and never
$0: a wrong number in a cost UI is worse than an honest blank.

Costs use Decimal internally so fractions of a cent don't pick up float noise,
and are converted to float only at the edge (JSON / SQLite REAL).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.llm.usage import Usage

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

log = logging.getLogger("chat.pricing")

_PER = Decimal(1_000_000)


class PricingError(ValueError):
    """prices.toml exists but is malformed."""


@dataclass(frozen=True)
class ModelPrice:
    input_per_million: float
    output_per_million: float
    # Optional: some models have no cache discount. If cached tokens are reported
    # for a model with no cached price we cannot price them correctly -> cost None.
    cached_input_per_million: float | None = None


@dataclass(frozen=True)
class CostBreakdown:
    input_cost: float  # NON-cached input tokens
    cached_input_cost: float
    output_cost: float  # includes reasoning tokens (they are part of output_tokens)
    total_cost: float
    # What the prompt cache saved: the cached tokens at the normal input rate minus what
    # they actually cost. 0.0 when nothing was cached; None only if it cannot be known.
    cache_savings: float | None = None

    def as_dict(self) -> dict[str, float | None]:
        return {
            "input_cost": self.input_cost,
            "cached_input_cost": self.cached_input_cost,
            "output_cost": self.output_cost,
            "total_cost": self.total_cost,
            "cache_savings": self.cache_savings,
        }


def cache_savings(price: ModelPrice, input_tokens: int, cached_input_tokens: int | None) -> float | None:
    """Money the prompt cache saved on one call: cached x (input rate - cached rate) / 1M.

    This is the difference between what those tokens WOULD have cost uncached and what
    they cost. None if cached tokens were reported but the model has no cached price.
    """
    cached = min(cached_input_tokens or 0, input_tokens)
    if cached == 0:
        return 0.0
    if price.cached_input_per_million is None:
        return None
    saved = Decimal(cached) * (Decimal(str(price.input_per_million)) - Decimal(str(price.cached_input_per_million))) / _PER
    return float(max(saved, Decimal(0)))


def calculate_cost(
    price: ModelPrice,
    *,
    input_tokens: int,
    cached_input_tokens: int | None,
    output_tokens: int,
) -> CostBreakdown | None:
    """Pure arithmetic for ONE priced model. None if cached tokens can't be priced."""
    cached = cached_input_tokens or 0  # not reported => no cached discount was reported
    if cached > input_tokens:
        log.warning("cached_tokens (%d) > input_tokens (%d); clamping", cached, input_tokens)
        cached = input_tokens
    if cached > 0 and price.cached_input_per_million is None:
        return None  # would have to guess the cached rate

    non_cached = input_tokens - cached
    input_cost = Decimal(non_cached) * Decimal(str(price.input_per_million)) / _PER
    cached_cost = (
        Decimal(cached) * Decimal(str(price.cached_input_per_million)) / _PER
        if cached
        else Decimal(0)
    )
    output_cost = Decimal(output_tokens) * Decimal(str(price.output_per_million)) / _PER
    return CostBreakdown(
        input_cost=float(input_cost),
        cached_input_cost=float(cached_cost),
        output_cost=float(output_cost),
        total_cost=float(input_cost + cached_cost + output_cost),
        cache_savings=cache_savings(price, input_tokens, cached_input_tokens),
    )


class PriceTable:
    def __init__(self, prices: dict[str, ModelPrice] | None = None) -> None:
        self._prices = dict(prices or {})
        self._warned: set[str] = set()  # warn once per model, not once per message

    def get(self, model: str | None) -> ModelPrice | None:
        return self._prices.get(model) if model else None

    def resolve(self, requested_model: str | None, served_model: str | None) -> str | None:
        """Which price entry applies?

        We ask for an ALIAS ("gpt-x") and OpenAI may serve a dated SNAPSHOT of it
        ("gpt-x-2026-03-17"). Rules, in order:
          1. the served model has its own entry          -> use it
          2. the served model is exactly `<requested>-YYYY-MM-DD` (a snapshot of the
             alias we priced)                            -> use the alias price
          3. nothing reported                            -> use the requested model
          4. anything else (a different model!)          -> None: don't guess.
        """
        if served_model and served_model in self._prices:
            return served_model
        if not served_model:
            return requested_model if requested_model in self._prices else None
        if (
            requested_model in self._prices
            and re.fullmatch(rf"{re.escape(requested_model)}-\d{{4}}-\d{{2}}-\d{{2}}", served_model)
        ):
            return requested_model
        return None

    def cache_savings_for(
        self, requested_model: str | None, served_model: str | None,
        input_tokens: int | None, cached_input_tokens: int | None,
    ) -> float | None:
        """Cache savings for stored usage numbers (used when reading rows back)."""
        if input_tokens is None:
            return None
        key = self.resolve(requested_model, served_model)
        price = self.get(key)
        return cache_savings(price, input_tokens, cached_input_tokens) if price else None

    def calculate_cost(
        self,
        model: str,
        input_tokens: int,
        cached_input_tokens: int | None,
        output_tokens: int,
    ) -> CostBreakdown | None:
        """Exact-name lookup + calculation. None (with a warning) if not priceable."""
        price = self.get(model)
        if price is None:
            self._warn_once(model, f"Pricing not configured for model: {model}")
            return None
        breakdown = calculate_cost(
            price,
            input_tokens=input_tokens,
            cached_input_tokens=cached_input_tokens,
            output_tokens=output_tokens,
        )
        if breakdown is None:
            self._warn_once(
                f"{model}:cached",
                f"Model {model} reported cached input tokens but prices.toml has no "
                "`cached_input` price for it; cost left unknown",
            )
        return breakdown

    def cost(
        self, requested_model: str | None, served_model: str | None, usage: Usage | None
    ) -> CostBreakdown | None:
        """Cost of one reply, or None if usage or a price is unavailable."""
        if usage is None:
            return None  # no reported usage -> nothing to price (never estimate)
        key = self.resolve(requested_model, served_model)
        if key is None:
            self._warn_once(
                f"{requested_model}/{served_model}",
                f"Pricing not configured for model: {served_model or requested_model} "
                f"(requested {requested_model})",
            )
            return None
        return self.calculate_cost(key, usage.input_tokens, usage.cached_input_tokens, usage.output_tokens)

    def _warn_once(self, key: str, message: str) -> None:
        if key not in self._warned:
            self._warned.add(key)
            log.warning(message)

    @classmethod
    def load(cls, path: Path) -> "PriceTable":
        if not path.exists():
            log.warning("prices_file_missing path=%s (all costs will be unknown)", path)
            return cls()
        with path.open("rb") as f:
            data = tomllib.load(f)

        def number(name: str, label: str, value: Any) -> float:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                raise PricingError(f'[models."{name}"] {label} must be a number >= 0, got {value!r}')
            return float(value)

        prices: dict[str, ModelPrice] = {}
        for name, entry in (data.get("models") or {}).items():
            try:
                inp, out = entry["input"], entry["output"]
            except (KeyError, TypeError):
                raise PricingError(f'[models."{name}"] needs both `input` and `output`') from None
            cached = entry.get("cached_input")
            prices[name] = ModelPrice(
                number(name, "input", inp),
                number(name, "output", out),
                number(name, "cached_input", cached) if cached is not None else None,
            )
        return cls(prices)

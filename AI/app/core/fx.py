"""USD -> INR display rate.

OpenAI bills in USD, so USD is the real amount. The UI also shows INR next to it,
using an exchange rate that CHANGES, so a hardcoded number goes stale (88 was
about 9% off when this was written). Modes, via USD_TO_INR:

    auto (default)  fetch the current rate from a free public API, cache it for
                    FX_TTL_S, fall back to USD_TO_INR_FALLBACK if it can't be fetched
    <number>        use exactly this fixed rate
    0               hide INR

The INR figure is only a display conversion of the stored USD cost; the database
and all calculations stay in USD. It is a market reference rate, not what your
bank or card will actually charge.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

log = logging.getLogger("chat.fx")

FX_URL = "https://open.er-api.com/v6/latest/USD"  # free, no API key


@dataclass(frozen=True)
class FxRate:
    rate: float | None  # INR per 1 USD; None = hide INR
    source: str  # "live" | "fallback" | "fixed" | "off"
    as_of: str | None = None  # provider's "last updated" timestamp for live rates


async def _fetch_live() -> dict[str, Any]:
    import httpx  # imported lazily: only needed in auto mode

    async with httpx.AsyncClient(timeout=3.0) as client:
        response = await client.get(FX_URL)
        response.raise_for_status()
        return response.json()


class UsdInrProvider:
    def __init__(
        self,
        fixed: float | None,
        fallback: float,
        ttl_s: float = 6 * 3600,
        retry_after_s: float = 60.0,
        fetch: Callable[[], Awaitable[dict[str, Any]]] = _fetch_live,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._fixed, self._fallback, self._ttl, self._retry = fixed, fallback, ttl_s, retry_after_s
        self._fetch, self._clock = fetch, clock
        self._cached: FxRate | None = None
        self._fresh_until = 0.0
        self._lock = asyncio.Lock()  # one fetch at a time, not one per request

    async def get(self) -> FxRate:
        if self._fixed is not None:
            return FxRate(self._fixed, "fixed") if self._fixed > 0 else FxRate(None, "off")

        if self._cached and self._clock() < self._fresh_until:
            return self._cached
        async with self._lock:
            if self._cached and self._clock() < self._fresh_until:
                return self._cached  # another request refreshed it while we waited
            try:
                data = await self._fetch()
                rate = float(data["rates"]["INR"])
                if not rate > 0:
                    raise ValueError(f"implausible rate {rate!r}")
                self._cached = FxRate(rate, "live", data.get("time_last_update_utc"))
                self._fresh_until = self._clock() + self._ttl
            except Exception as exc:  # network, bad JSON, missing key: never break the app over a display nicety
                log.warning("fx_fetch_failed error_type=%s; using %s", type(exc).__name__,
                            "last known rate" if self._cached else "fallback rate")
                self._fresh_until = self._clock() + self._retry  # don't hammer the API
                if self._cached is None:
                    self._cached = FxRate(self._fallback, "fallback")
            return self._cached

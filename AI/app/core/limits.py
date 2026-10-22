"""Abuse and cost protection: request-size cap and a minimal rate limiter.

Every chat request spends real money (input tokens) and holds a connection open
for seconds. Without limits, one script can:
  * burn the API budget          (cost)
  * overflow the model's context (a huge paste cannot be answered anyway)
  * exhaust memory / connections (denial of service)

This is the "minimum responsible" version for a single-process learning project.
See README "Production improvements" for the real thing (per-user auth, shared
rate-limit store such as Redis, a gateway/WAF, spend alerts).
"""
from __future__ import annotations

import time
from collections import deque
from typing import Callable

from fastapi.responses import JSONResponse


class BodySizeLimitMiddleware:
    """Reject oversized request bodies BEFORE they are read into memory.

    A pure ASGI middleware, deliberately not Starlette's @app.middleware("http"):
    that decorator wraps responses in a way that interferes with streaming and
    client-disconnect detection, which this app depends on.

    Limitation: it trusts the Content-Length header. A client using chunked
    transfer encoding sends none, so production deployments also set a body limit
    at the reverse proxy (e.g. nginx client_max_body_size).
    """

    def __init__(self, app, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http":
            for name, value in scope["headers"]:
                if name == b"content-length" and value.isdigit() and int(value) > self.max_bytes:
                    response = JSONResponse({"detail": "Request body too large"}, status_code=413)
                    await response(scope, receive, send)
                    return
        await self.app(scope, receive, send)


class SlidingWindowLimiter:
    """At most `limit` hits per `window_s` seconds per key (e.g. client IP).

    In-memory and per-process: fine for one server, wrong for several (each
    process would count separately), which is when you move it to Redis or the
    gateway. limit <= 0 disables it.
    """

    def __init__(
        self, limit: int, window_s: float = 60.0, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.limit = limit
        self.window_s = window_s
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}

    def check(self, key: str) -> float | None:
        """Record a hit. Returns None if allowed, else seconds until a slot frees up."""
        if self.limit <= 0:
            return None
        now = self._clock()
        hits = self._hits.setdefault(key, deque())
        while hits and now - hits[0] >= self.window_s:
            hits.popleft()
        if len(hits) >= self.limit:
            return max(0.0, self.window_s - (now - hits[0]))
        hits.append(now)
        if len(self._hits) > 10_000:  # bound memory: forget keys with no recent hits
            self._hits = {k: h for k, h in self._hits.items() if h and now - h[-1] < self.window_s}
        return None

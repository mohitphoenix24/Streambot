"""Web search through Tavily: query in, a few cited sources out.

The model never talks to Tavily. It asks OUR server to run `web_search`; we call
Tavily with OUR key, trim the answer to the fields we need, and hand back plain
`Source` records. Anything that goes wrong becomes a `ToolError` with a short,
safe message (no URLs, keys or raw provider text), the same rule the OpenAI
layer follows.

`fetch` is injectable so tests never touch the network, like UsdInrProvider.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

log = logging.getLogger("chat.tools.search")

TAVILY_URL = "https://api.tavily.com/search"
SNIPPET_CHARS = 500  # tokens are money: a snippet is a hint, not the whole page


@dataclass(frozen=True)
class Source:
    title: str
    url: str
    snippet: str


class ToolError(Exception):
    """A tool failed. `public_message` is safe to show the user and to tell the model."""

    def __init__(self, public_message: str) -> None:
        super().__init__(public_message)
        self.public_message = public_message


Fetch = Callable[[str, int], Awaitable[dict[str, Any]]]


async def _fetch_tavily(query: str, max_results: int, *, api_key: str) -> dict[str, Any]:
    import httpx  # imported lazily: only needed when a search actually runs

    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(
            TAVILY_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            json={"query": query, "max_results": max_results, "search_depth": "basic"},
        )
        response.raise_for_status()
        return response.json()


def _is_web_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


class TavilySearch:
    def __init__(self, api_key: str, max_results: int = 5, fetch: Fetch | None = None) -> None:
        self._max_results = max_results
        if fetch is None:
            async def fetch(query: str, n: int) -> dict[str, Any]:
                return await _fetch_tavily(query, n, api_key=api_key)
        self._fetch = fetch

    async def search(self, query: str) -> list[Source]:
        query = query.strip()
        if not query:
            raise ToolError("The search query was empty.")
        try:
            data = await self._fetch(query[:400], self._max_results)
        except Exception as exc:  # network, HTTP status, bad JSON: all the same to the caller
            log.warning("web_search_failed error_type=%s", type(exc).__name__)
            raise ToolError("Web search is unavailable right now.") from exc

        sources: list[Source] = []
        for item in (data.get("results") or [])[: self._max_results]:
            url = str(item.get("url") or "")
            if not _is_web_url(url):  # a javascript:/data: URL must never become a link
                continue
            sources.append(
                Source(
                    title=str(item.get("title") or urlparse(url).netloc).strip()[:200],
                    url=url,
                    snippet=" ".join(str(item.get("content") or "").split())[:SNIPPET_CHARS],
                )
            )
        return sources

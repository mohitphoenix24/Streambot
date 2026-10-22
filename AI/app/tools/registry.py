"""The tools the model may call, and the runner that executes them for ONE reply.

Why a runner per reply: citations are numbered [1], [2], ... across every search
made while answering, so the numbering state lives with the reply.

Layering: `app.llm.client` only knows the small `ToolExecutor` shape (definitions,
instructions, `run`); it never imports this module. This module knows nothing
about OpenAI.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from app.tools.web_search import Source, TavilySearch, ToolError

log = logging.getLogger("chat.tools")

WEB_SEARCH = {
    "type": "function",
    "name": "web_search",
    "description": (
        "Search the web for current or specific information. Returns numbered results "
        "with a title, URL and snippet."
    ),
    "parameters": {
        "type": "object",
        "properties": {"query": {"type": "string", "description": "A concise search query."}},
        "required": ["query"],
        "additionalProperties": False,
    },
    "strict": True,
}

# Appended to the system prompt ONLY when tools are enabled for the reply.
WEB_SEARCH_GUIDANCE = (
    "You can search the web with the web_search tool. Use it for recent events, current "
    "facts, prices, versions, or anything you are not sure about. Do not search for "
    "greetings, arithmetic, or general knowledge you are confident about. "
    "After searching, answer from the results and cite inline as [1], [2], using exactly the "
    "numbers shown in the results. Cite only claims the results support, and never invent a "
    "source or a number. Citation numbers belong to the current reply only: never reuse [n] "
    "markers from earlier messages, and if you did not search this turn, add no [n] markers."
)


@dataclass(frozen=True)
class ToolOutcome:
    query: str | None
    model_text: str  # what the MODEL reads
    sources: tuple[dict[str, Any], ...] = ()  # what the UI shows: {n, title, url, snippet}
    error: str | None = None


@dataclass
class ToolRunner:
    search: TavilySearch
    definitions: list[dict[str, Any]] = field(default_factory=lambda: [WEB_SEARCH])
    instructions: str = WEB_SEARCH_GUIDANCE
    _numbered: list[dict[str, Any]] = field(default_factory=list)

    @property
    def sources(self) -> list[dict[str, Any]]:
        return list(self._numbered)

    async def run(self, name: str, arguments: str) -> ToolOutcome:
        if name != "web_search":
            return ToolOutcome(None, f"Error: unknown tool {name!r}.", error="Unknown tool.")
        try:
            args = json.loads(arguments or "{}")
            query = str(args["query"])
        except (ValueError, KeyError, TypeError):
            return ToolOutcome(None, "Error: web_search needs a string `query`.", error="Bad search request.")

        try:
            found: list[Source] = await self.search.search(query)
        except ToolError as exc:
            return ToolOutcome(query, f"Error: {exc.public_message}", error=exc.public_message)

        fresh: list[dict[str, Any]] = []
        for s in found:
            fresh.append({"n": len(self._numbered) + len(fresh) + 1, "title": s.title, "url": s.url, "snippet": s.snippet})
        self._numbered.extend(fresh)
        if not fresh:
            return ToolOutcome(query, "No results found.")
        text = "\n\n".join(f"[{s['n']}] {s['title']} ({s['url']})\n{s['snippet']}" for s in fresh)
        return ToolOutcome(query, text, tuple(fresh))

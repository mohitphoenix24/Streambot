"""Shared test doubles. Nothing here touches the network."""
from __future__ import annotations

import json
from typing import Any, AsyncIterator

from app.llm.client import Completed, Completion, Generated, HistoryMessage, LLMEvent, TextDelta, Usage


class ScriptedLLM:
    """A fake provider that plays back a fixed script of events / exceptions.

    Items in `script`: LLMEvent instances are yielded; Exception instances are
    raised at that point. That makes "stream 'Hello ', 'world', then crash"
    a one-liner, deterministic, and free.
    """

    model = "test-model"

    def __init__(self, script: list[LLMEvent | Exception], title: str | None = "Scripted Title",
                 model: str | None = None) -> None:
        if model:
            self.model = model  # instance attribute shadows the class default
        self.script = script
        self.title = title
        self.histories: list[list[HistoryMessage]] = []  # what the app sent us, per call
        self.upstream_closed = False  # did the app close the provider stream?
        self.title_requests: list[str] = []
        self.tools_seen: list = []  # the ToolExecutor (or None) passed for each reply
        self.summary_requests: list[tuple[str | None, list[HistoryMessage]]] = []  # (previous summary, folded messages)
        self.summary_text: str | None = "SCRIPTED SUMMARY"  # None = the summary call fails

    async def stream(self, history: list[HistoryMessage], tools=None) -> AsyncIterator[LLMEvent]:
        self.histories.append(history)
        self.tools_seen.append(tools)
        try:
            for item in self.script:
                if isinstance(item, Exception):
                    raise item
                yield item
        finally:
            self.upstream_closed = True

    async def complete(self, history: list[HistoryMessage]) -> Completion:
        self.histories.append(history)
        return Completion("complete-text", "completed", Usage(10, 5, 15))

    async def generate_title(self, first_user_text: str) -> Generated | None:
        self.title_requests.append(first_user_text)
        return Generated(self.title, Usage(30, 6, 36), "title-snapshot", self.model) if self.title else None

    async def summarize(self, previous_summary, messages) -> Generated | None:
        self.summary_requests.append((previous_summary, messages))
        if self.summary_text is None:
            return None
        return Generated(self.summary_text, Usage(200, 40, 240), "summary-snapshot", self.model)


def ok_script(*chunks: str, usage: Usage | None = Usage(100, 50, 150), stop: str = "completed",
              served_model: str | None = None):
    return [*(TextDelta(c) for c in chunks), Completed(stop, usage, served_model)]


def parse_sse(text: str) -> list[tuple[str, dict[str, Any]]]:
    """Parse a full SSE body into [(event, data_dict), ...] (test-side mirror of sse.ts)."""
    events = []
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        name, data = "message", None
        for line in block.split("\n"):
            if line.startswith("event: "):
                name = line[len("event: "):]
            elif line.startswith("data: "):
                data = json.loads(line[len("data: "):])
        events.append((name, data))
    return events

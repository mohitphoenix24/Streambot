"""Split a document into chunks: the passages that get embedded and retrieved.

Why chunk at all? A model reads a prompt of limited size, and a vector is one summary of ONE
piece of text. Embed a whole 200-page PDF as a single vector and it means "kind of about
everything"; embed 500-token passages and each vector means something specific, so a question
can find the exact passage that answers it.

Rules (each one is there for a reason):

  * Cut at PARAGRAPH boundaries, not in the middle of a sentence: a passage should read as a unit.
  * Aim for about `target_chars` (~450 tokens). Too small loses context, too big blurs the meaning.
  * OVERLAP each chunk with the end of the previous one: an answer that straddles a boundary is
    then fully inside at least one chunk.
  * Remember the PAGE and the nearest HEADING: they become the citation ("report.pdf, p. 12").
  * A paragraph longer than the target is split at sentence ends, then by length as a last resort.

Pure functions: pages in, chunks out. No database, no network.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.chat.history import approx_tokens
from app.rag.extract import Page

_HEADING = re.compile(r"^(#{1,6})\s+(\S.*)$")
_PARAGRAPHS = re.compile(r"\n\s*\n")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


@dataclass(frozen=True)
class Chunk:
    ordinal: int
    page: int | None
    heading: str | None
    content: str
    tokens: int


def _split_long(paragraph: str, limit: int) -> list[str]:
    """Break an over-long paragraph at sentence ends, and hard-split anything still too long."""
    pieces, current = [], ""
    for sentence in _SENTENCE_END.split(paragraph):
        while len(sentence) > limit:  # a "sentence" with no full stop (a table, a code listing)
            cut = sentence.rfind(" ", 0, limit)
            cut = cut if cut > limit // 2 else limit
            pieces.append(sentence[:cut].strip())
            sentence = sentence[cut:].strip()
        if current and len(current) + 1 + len(sentence) > limit:
            pieces.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        pieces.append(current)
    return [p for p in pieces if p]


def _tail(text: str, overlap: int) -> str:
    """The last ~`overlap` characters of `text`, starting at a word boundary."""
    if overlap <= 0 or len(text) <= overlap:
        return text if overlap > 0 else ""
    tail = text[-overlap:]
    space = tail.find(" ")
    return tail[space + 1:] if 0 <= space < len(tail) - 1 else tail


def chunk_pages(pages: list[Page], target_chars: int = 1800, overlap_chars: int = 200) -> list[Chunk]:
    # 1. flatten into units, each remembering its page and the heading in force where it starts
    units: list[tuple[int | None, str | None, str]] = []
    heading: str | None = None
    for page in pages:
        for paragraph in _PARAGRAPHS.split(page.text):
            paragraph = paragraph.strip()
            if not paragraph:
                continue
            match = _HEADING.match(paragraph.split("\n", 1)[0])
            if match:
                heading = match.group(2).strip()[:200]
            for piece in (_split_long(paragraph, target_chars) if len(paragraph) > target_chars else [paragraph]):
                units.append((page.number, heading, piece))

    # 2. pack units into chunks of about target_chars, overlapping the previous chunk's tail
    chunks: list[Chunk] = []
    buffer = ""
    start: tuple[int | None, str | None] | None = None

    def flush() -> None:
        nonlocal buffer, start
        content = buffer.strip()
        if content and start is not None:
            chunks.append(Chunk(len(chunks), start[0], start[1], content, approx_tokens(content)))

    for page_number, unit_heading, text in units:
        if buffer and len(buffer) + 2 + len(text) > target_chars:
            flush()
            buffer = _tail(buffer, overlap_chars)  # carry the end of the last chunk into the next
            start = (page_number, unit_heading)  # the new chunk is located where its NEW text begins
        elif not buffer:
            start = (page_number, unit_heading)
        buffer = f"{buffer}\n\n{text}" if buffer else text
    flush()

    # Drop trivial fragments, then number what is left 0, 1, 2, ... in reading order.
    kept = [c for c in chunks if len(c.content) >= 20 or len(chunks) == 1]
    return [Chunk(i, c.page, c.heading, c.content, c.tokens) for i, c in enumerate(kept)]

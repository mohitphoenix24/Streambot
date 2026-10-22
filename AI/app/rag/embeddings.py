"""Embeddings: turn text into a vector of numbers that captures its MEANING.

An embedding model reads a passage and returns a list of 1536 numbers (a point in a 1536-
dimensional space). The useful property: passages with similar meaning land close together, so
"How do I cancel my subscription?" sits near "Steps to terminate your plan" even though they
share no words. Searching by meaning is then just "find the stored points nearest to the
question's point".

This is the second place (after llm/client.py) that talks to OpenAI, and it follows the same
rules: key only on the server, bounded retries, errors translated into a safe sentence, usage
reported so the cost can be recorded.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Protocol

import httpx2
import openai
from openai import AsyncOpenAI

from app.config import Settings
from app.llm.client import LLMError, translate_openai_error

log = logging.getLogger("chat.rag.embeddings")

EMBEDDING_DIMENSIONS = 1536  # text-embedding-3-small; the `vector(1536)` column must match
BATCH_SIZE = 64  # texts per request (the API allows far more; small batches fail small)


@dataclass(frozen=True)
class Embedded:
    vectors: list[list[float]]
    input_tokens: int | None  # as REPORTED by the provider; None = not reported
    served_model: str | None
    model: str  # the model we asked for


class Embedder(Protocol):
    """What the app needs from an embedding provider (tests plug in a deterministic stand-in)."""

    model: str

    async def embed(self, texts: list[str]) -> Embedded: ...


class OpenAIEmbedder:
    def __init__(self, settings: Settings, client: AsyncOpenAI | None = None) -> None:
        self.model = settings.embedding_model
        self._max_attempts = settings.llm_max_attempts
        self._base_delay = settings.retry_base_delay_s
        self._client = client or AsyncOpenAI(
            api_key=settings.openai_api_key,
            max_retries=0,  # we own the retry policy, as in llm/client.py
            timeout=httpx2.Timeout(settings.openai_timeout_s, connect=5.0),
        )

    async def embed(self, texts: list[str]) -> Embedded:
        vectors: list[list[float]] = []
        tokens: int | None = 0
        served: str | None = None
        for i in range(0, len(texts), BATCH_SIZE):
            batch = texts[i:i + BATCH_SIZE]
            response = await self._request(batch)
            ordered = sorted(response.data, key=lambda d: d.index)  # results are matched to inputs by index
            if len(ordered) != len(batch):
                raise LLMError("The embedding service returned an unexpected response.")
            for item in ordered:
                if len(item.embedding) != EMBEDDING_DIMENSIONS:
                    log.error("embedding_dimension_mismatch got=%d expected=%d", len(item.embedding), EMBEDDING_DIMENSIONS)
                    raise LLMError("The embedding model returned vectors of an unexpected size.")
                vectors.append(list(item.embedding))
            usage = getattr(response, "usage", None)
            reported = getattr(usage, "total_tokens", None) if usage is not None else None
            tokens = None if (reported is None or tokens is None) else tokens + reported
            served = getattr(response, "model", None) or served
        return Embedded(vectors, tokens, served, self.model)

    async def _request(self, batch: list[str]) -> Any:
        attempt = 1
        while True:
            try:
                return await self._client.embeddings.create(model=self.model, input=batch)
            except (openai.OpenAIError, httpx2.TransportError) as exc:
                error = translate_openai_error(exc)
                if not error.retryable or attempt >= self._max_attempts:
                    raise error from exc
                await asyncio.sleep(min(8.0, self._base_delay * 2 ** (attempt - 1)))
                attempt += 1

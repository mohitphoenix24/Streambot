"""Index an uploaded document in the background: extract, chunk, embed, store.

Embedding a long PDF takes seconds, so the upload endpoint returns immediately with the document
in state 'processing', and this manager finishes the job. The browser polls the document list
until the state becomes 'ready' (or 'failed', with a short reason).

Every outcome is handled: a file that cannot be read, a provider error, a document deleted while
it was being indexed, a server killed halfway (startup marks those 'failed'). Nothing here may
raise into the void: an unhandled error in a background task would leave the document 'processing'
for ever.
"""
from __future__ import annotations

import asyncio
import logging

import psycopg
from fastapi.concurrency import run_in_threadpool

from app.config import Settings
from app.db import Database
from app.llm.accounting import record_llm_call
from app.llm.client import Generated, LLMError
from app.llm.pricing import PriceTable
from app.llm.usage import Usage
from app.rag.chunker import chunk_pages
from app.rag.embeddings import Embedded, Embedder
from app.rag.extract import ExtractionError, extract

log = logging.getLogger("chat.rag.ingest")


def embedding_call(embedded: Embedded) -> Generated:
    """An embedding request as a cost record (input tokens only: there is no output)."""
    tokens = embedded.input_tokens
    usage = Usage(input_tokens=tokens, output_tokens=0, total_tokens=tokens) if tokens is not None else None
    return Generated("", usage, embedded.served_model, embedded.model)


class DocumentManager:
    def __init__(self, db: Database, embedder: Embedder, prices: PriceTable, settings: Settings) -> None:
        self._db, self._embedder, self._prices, self._settings = db, embedder, prices, settings
        self._tasks: set[asyncio.Task] = set()  # strong references: a task nobody holds can be garbage collected

    def schedule(self, document_id: str, user_id: str, conversation_id: str, kind: str, data: bytes) -> None:
        task = asyncio.create_task(self.run(document_id, user_id, conversation_id, kind, data))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def run(self, document_id: str, user_id: str, conversation_id: str, kind: str, data: bytes) -> None:
        s, db = self._settings, self._db
        try:
            # Parsing a big PDF is CPU work: do it in a worker thread so the server keeps serving others.
            extracted = await run_in_threadpool(extract, kind, data, s.rag_max_pages, s.rag_max_chars)
            chunks = chunk_pages(extracted.pages, s.rag_chunk_chars, s.rag_overlap_chars)
            if not chunks:
                raise ExtractionError("No readable text was found in this file.")
            if len(chunks) > s.rag_max_chunks:
                raise ExtractionError("This document is too large to index.")

            embedded = await self._embedder.embed([c.content for c in chunks])
            record_llm_call(db, self._prices, user_id, conversation_id, "embedding", embedding_call(embedded))

            db.complete_document(
                document_id,
                extracted.page_count,
                [
                    {"user_id": user_id, "conversation_id": conversation_id, "ordinal": c.ordinal, "page": c.page,
                     "heading": c.heading, "content": c.content, "tokens": c.tokens, "embedding": vector}
                    for c, vector in zip(chunks, embedded.vectors)
                ],
            )
            log.info("document_ready document_id=%s chunks=%d pages=%s", document_id, len(chunks), extracted.page_count)
        except ExtractionError as exc:
            db.mark_document_failed(document_id, exc.public_message)
        except LLMError as exc:
            log.warning("document_embedding_failed document_id=%s", document_id, exc_info=True)
            db.mark_document_failed(document_id, exc.public_message)
        except psycopg.errors.ForeignKeyViolation:
            log.info("document_deleted_while_processing document_id=%s", document_id)  # the user removed it: nothing to do
        except Exception:
            log.error("document_processing_failed document_id=%s", document_id, exc_info=True)
            db.mark_document_failed(document_id, "This file could not be processed.")

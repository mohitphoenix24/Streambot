"""Search the documents attached to ONE conversation of ONE user.

This is the retrieval half of RAG. Given the model's query it

  1. embeds the query (the same model that embedded the document chunks),
  2. asks the database for the best-matching passages (vector + keyword, merged), and
  3. returns them as `Source` records the answer can cite as [n].

The search is scoped to (user_id, conversation_id) in SQL itself, so it can never return another
user's text, and a document attached to one chat is invisible to every other chat.
"""
from __future__ import annotations

import logging

from app.config import Settings
from app.db import Database
from app.llm.accounting import record_llm_call
from app.llm.pricing import PriceTable
from app.rag.embeddings import Embedder
from app.rag.ingest import embedding_call
from app.tools.web_search import Source, ToolError

log = logging.getLogger("chat.tools.documents")


class DocumentSearch:
    def __init__(
        self, db: Database, embedder: Embedder, prices: PriceTable, settings: Settings,
        user_id: str, conversation_id: str,
    ) -> None:
        self._db, self._embedder, self._prices, self._settings = db, embedder, prices, settings
        self._user_id, self._conversation_id = user_id, conversation_id

    async def search(self, query: str) -> list[Source]:
        query = query.strip()
        if not query:
            raise ToolError("The search query was empty.")
        try:
            embedded = await self._embedder.embed([query[:2000]])
            record_llm_call(self._db, self._prices, self._user_id, self._conversation_id, "embedding", embedding_call(embedded))
            rows = self._db.search_chunks(
                self._user_id, self._conversation_id, embedded.vectors[0], query,
                candidates=max(20, self._settings.rag_top_k * 4), limit=self._settings.rag_top_k,
            )
        except Exception as exc:
            log.warning("document_search_failed error_type=%s", type(exc).__name__)
            raise ToolError("Searching your documents failed. Please try again.") from exc
        return [
            Source(
                title=r["filename"] + (f" \u00b7 {r['heading']}" if r["heading"] else ""),
                url="",
                snippet=r["content"],
                kind="document",
                page=r["page"],
                document_id=r["document_id"],
            )
            for r in rows
        ]

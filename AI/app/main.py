"""FastAPI application: the HTTP surface of the chat backend.

Quick vocabulary (see LEARNING_NOTES.md for the long version):

  * HTTP endpoint  - a (method, path) pair the server answers, e.g. GET /health.
  * FastAPI        - a Python web framework. You write a function, decorate it
                     with its route, and FastAPI handles parsing, validation and
                     JSON serialisation.
  * Uvicorn        - the ASGI *server*. It owns the sockets, speaks HTTP, and
                     calls into FastAPI. `uvicorn app:app` means "import the
                     module app.py and serve its variable named app".
  * async          - while one request is waiting on the network (an LLM can
                     take seconds per reply), the event loop serves other
                     requests instead of blocking a whole thread. A streaming
                     chat is mostly *waiting*, so async is a natural fit.
"""
from __future__ import annotations

import logging
import math
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse

from app.config import Settings
from app.db import Database
from app.core.fx import UsdInrProvider
from app.core.limits import BodySizeLimitMiddleware, SlidingWindowLimiter
from app.llm.client import LLM, LLMError, build_llm
from app.llm.pricing import PriceTable
from app.schemas import ConversationDetail, ConversationOut, MessageOut, SendMessageIn
from app.chat.streaming import begin_turn, finalize_complete, stream_reply

log = logging.getLogger("chat.app")


def create_app(
    settings: Settings | None = None, llm: LLM | None = None, fx: UsdInrProvider | None = None
) -> FastAPI:
    """App factory.

    A factory (instead of building everything at import time) lets tests create
    an app with a temp database and a scripted LLM without touching the environment.
    """
    settings = settings or Settings.from_env()
    db = Database(settings.database_path)
    prices = PriceTable.load(settings.prices_path)
    limiter = SlidingWindowLimiter(settings.rate_limit_per_minute)
    fx = fx or UsdInrProvider(settings.usd_to_inr, settings.usd_to_inr_fallback)
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Runs once when the server starts. A bad config crashes HERE, with a
        # readable message, rather than on a user's first request.
        settings.validate()
        app.state.llm = llm or build_llm(settings)
        db.init_schema()
        orphaned = db.recover_interrupted()
        if orphaned:
            log.warning("recovered_interrupted_messages count=%d", orphaned)
        yield

    app = FastAPI(title="Streaming Chat", lifespan=lifespan)
    app.state.settings = settings
    app.state.db = db

    # Body-size cap: added BEFORE CORS so CORS ends up outermost and even our 413
    # responses carry CORS headers (otherwise the browser shows an opaque network
    # error instead of a readable "too large"). Budget per char: JSON can escape a
    # non-BMP character as 12 bytes (two \uXXXX), plus envelope.
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_message_chars * 12 + 1024)

    # CORS: the React dev server (localhost:3100+) and this API (localhost:8501)
    # are different *origins*, so the browser blocks the page from reading our
    # responses unless we explicitly allow that origin.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_origin_regex=settings.cors_origin_regex,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    def get_llm(request: Request) -> LLM:
        return request.app.state.llm

    def require_conversation(conversation_id: str) -> dict:
        conv = db.get_conversation(conversation_id)
        if conv is None:
            raise HTTPException(status_code=404, detail="Conversation not found")
        return conv

    def prepare_send(request: Request, conversation_id: str, body: SendMessageIn) -> str:
        """Every check a chat request must pass BEFORE any model money is spent."""
        # request.client is the direct peer. Behind a reverse proxy that is the
        # proxy's IP for everyone; production must use a trusted X-Forwarded-For.
        retry_after = limiter.check(request.client.host if request.client else "unknown")
        if retry_after is not None:
            raise HTTPException(
                status_code=429,
                detail="Too many requests. Please slow down.",
                headers={"Retry-After": str(math.ceil(retry_after))},
            )
        require_conversation(conversation_id)
        text = validated_text(body)
        # One reply at a time per conversation. Two concurrent generations would
        # interleave their messages and each would see a history missing the other.
        if db.has_streaming_message(conversation_id):
            raise HTTPException(
                status_code=409,
                detail="A reply is still being generated in this conversation. Wait for it or press Stop.",
            )
        return text

    def validated_text(body: SendMessageIn) -> str:
        if not body.text.strip():
            raise HTTPException(status_code=422, detail="Message is empty")
        if len(body.text) > settings.max_message_chars:
            raise HTTPException(
                status_code=413,
                detail=f"Message too long (max {settings.max_message_chars} characters)",
            )
        return body.text

    # ── Conversations ────────────────────────────────────────────────────
    # Everything is read from SQLite on every request, so a page refresh (or a
    # server restart) loses nothing: the database, not memory, is the truth.

    @app.post("/conversations", response_model=ConversationOut, status_code=201)
    async def create_conversation() -> dict:
        return db.create_conversation()

    @app.get("/conversations", response_model=list[ConversationOut])
    async def list_conversations() -> list[dict]:
        return db.list_conversations()

    @app.get("/conversations/{conversation_id}", response_model=ConversationDetail)
    async def get_conversation(conversation_id: str) -> ConversationDetail:
        conv = db.get_conversation(conversation_id)
        if conv is None:
            raise HTTPException(status_code=404, detail="Conversation not found")
        return ConversationDetail(
            **conv,
            messages=[MessageOut(**m) for m in db.list_messages(conversation_id)],
            **db.conversation_totals(conversation_id),
        )

    @app.delete("/conversations/{conversation_id}", status_code=204)
    async def delete_conversation(conversation_id: str) -> Response:
        require_conversation(conversation_id)
        # Deleting under a live stream would orphan the reply being written.
        if db.has_streaming_message(conversation_id):
            raise HTTPException(
                status_code=409,
                detail="A reply is still being generated in this conversation. Press Stop first.",
            )
        db.delete_conversation(conversation_id)
        return Response(status_code=204)

    # ── The streaming endpoint (what the React app calls) ────────────────
    #
    # It is a POST (we must send the user's text in a body), and the response is
    # an SSE stream. The browser's built-in EventSource can only issue GET with
    # no body, so the frontend uses fetch() and reads the response body itself.
    @app.post("/conversations/{conversation_id}/messages")
    async def send_message(
        conversation_id: str, body: SendMessageIn, request: Request, llm: LLM = Depends(get_llm)
    ) -> StreamingResponse:
        started_at = time.perf_counter()  # t0 for time-to-first-token
        text = prepare_send(request, conversation_id, body)
        turn = begin_turn(db, llm, settings, conversation_id, text)
        return StreamingResponse(
            stream_reply(
                request=request, db=db, llm=llm, settings=settings, prices=prices,
                turn=turn, started_at=started_at,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",  # never cache a live stream
                # nginx (and similar proxies) buffer responses by default, which
                # would hold back our deltas and defeat streaming. This header
                # asks nginx not to.
                "X-Accel-Buffering": "no",
            },
        )

    # ── Phase 3: the NON-streaming request, kept as a teaching reference ──
    #
    #   user text -> FastAPI -> OpenAI Responses API -> (wait...) -> full answer -> JSON
    #
    # The client sees nothing until the model has finished generating, so a long
    # answer feels like a frozen page. The streaming endpoint (added next) fixes
    # exactly that; it is the one the React app uses.
    @app.post("/conversations/{conversation_id}/messages/sync", response_model=MessageOut)
    async def send_message_sync(
        conversation_id: str, body: SendMessageIn, request: Request, llm: LLM = Depends(get_llm)
    ) -> dict:
        started_at = time.perf_counter()
        text = prepare_send(request, conversation_id, body)
        # Same preparation as the streaming path: save the user message, create
        # the placeholder assistant row, build budgeted history from the DB.
        turn = begin_turn(db, llm, settings, conversation_id, text)
        assistant_id = turn.assistant_message["id"]
        try:
            result = await llm.complete(turn.history)
        except LLMError as exc:
            log.error(
                "sync_reply_failed conversation_id=%s message_id=%s error_type=%s",
                conversation_id, assistant_id, type(exc.__cause__ or exc).__name__,
            )
            db.update_message(assistant_id, status="error", stop_reason="error")
            raise HTTPException(status_code=502, detail=exc.public_message) from exc

        finalize_complete(
            db, prices, assistant_id=assistant_id, model=llm.model,
            served_model=result.served_model, content=result.text,
            stop_reason=result.stop_reason, usage=result.usage, ttft_ms=None,
            latency_ms=round((time.perf_counter() - started_at) * 1000, 1),
        )
        db.touch_conversation(conversation_id)
        return db.get_message(assistant_id)

    @app.get("/info")
    async def info(request: Request) -> dict[str, str | float | None]:
        """Which model this server uses plus the display exchange rate. No secrets."""
        rate = await fx.get()
        return {
            "model": request.app.state.llm.model,
            "usd_to_inr": rate.rate,  # INR per 1 USD, or None (hidden)
            "usd_to_inr_source": rate.source,  # live | fallback | fixed | off
            "usd_to_inr_as_of": rate.as_of,
        }

    return app


# `uvicorn app.main:app --reload` looks for this module-level name.
app = create_app()

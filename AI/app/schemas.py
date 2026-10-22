"""Pydantic models: the shapes of our HTTP request and response bodies.

Pydantic gives us two things for free: validation of incoming JSON (a wrong type
becomes a clean 422 instead of a crash deep in our code) and a typed, documented
response shape (visible at /docs).
"""
from __future__ import annotations

from pydantic import BaseModel


class SendMessageIn(BaseModel):
    # Length is enforced in the route, using the configurable MAX_MESSAGE_CHARS,
    # so the limit can change via env var without editing this class.
    text: str


class MessageOut(BaseModel):
    id: str
    role: str
    content: str
    status: str  # streaming | complete | cancelled | error
    # Provider-reported usage; None = unavailable (never estimated).
    input_tokens: int | None = None
    cached_input_tokens: int | None = None  # subset of input_tokens
    output_tokens: int | None = None
    reasoning_tokens: int | None = None  # subset of output_tokens
    total_tokens: int | None = None
    cost_usd: float | None = None  # None = unknown (no price configured), NOT free
    stop_reason: str | None = None
    model: str | None = None  # requested model
    served_model: str | None = None  # model OpenAI reports it served
    ttft_ms: float | None = None
    latency_ms: float | None = None
    created_at: str


class ConversationOut(BaseModel):
    id: str
    title: str
    created_at: str
    updated_at: str


class ConversationDetail(ConversationOut):
    messages: list[MessageOut]
    total_cost_usd: float
    total_tokens: int
    total_input_tokens: int
    total_cached_input_tokens: int
    total_output_tokens: int
    unpriced_messages: int  # replies with usage but no price -> total is a lower bound

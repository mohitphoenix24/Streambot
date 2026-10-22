"""Cancellation against a REAL uvicorn server and a REAL TCP disconnect.

In-process tests can't prove the server notices a client hanging up. This one
starts a genuine server (scripted LLM, no OpenAI), opens a streaming request, closes
the socket mid-stream (exactly what AbortController.abort() does in a browser),
and checks the database.
"""
import socket
import threading
import time

import httpx
import pytest
import uvicorn

from app.main import create_app
from app.config import Settings
from app.db import Database
from app.llm.client import Completed, TextDelta
from tests.helpers import ScriptedLLM


class EndlessLLM(ScriptedLLM):
    """Streams a token every 20 ms for ~60 s unless the app stops listening."""

    async def stream(self, history):
        import asyncio

        self.histories.append(history)
        try:
            for i in range(3000):
                await asyncio.sleep(0.02)
                yield TextDelta(f"tok{i} ")
            yield Completed("completed", None)
        finally:
            self.upstream_closed = True


@pytest.fixture
def live_server(tmp_path):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    settings = Settings(openai_api_key="test-key", openai_model="test-model", database_path=tmp_path / "live.db", rate_limit_per_minute=0,
                        partial_flush_interval_s=0.1)
    llm = EndlessLLM([])
    server = uvicorn.Server(uvicorn.Config(create_app(settings, llm), host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 5
    while not server.started and time.time() < deadline:
        time.sleep(0.02)
    assert server.started, "server did not start"
    yield f"http://127.0.0.1:{port}", Database(settings.database_path), llm
    server.should_exit = True
    thread.join(timeout=5)


def wait_for(predicate, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    return None


def test_closing_the_connection_cancels_the_reply_and_stops_the_model(live_server):
    base, db, llm = live_server
    cid = httpx.post(f"{base}/conversations").json()["id"]

    deltas = 0
    with httpx.stream("POST", f"{base}/conversations/{cid}/messages", json={"text": "go"}, timeout=10) as r:
        for line in r.iter_lines():
            if line.startswith("event: delta"):
                deltas += 1
            if deltas == 5:
                break  # leaving the block closes the socket, like AbortController.abort()

    row = wait_for(lambda: next((m for m in db.list_messages(cid) if m["role"] == "assistant" and m["status"] != "streaming"), None))
    assert row is not None, "server never finalized the reply after the client disconnected"
    assert row["status"] == "cancelled"
    assert row["content"].startswith("tok0 tok1 ")  # partial text saved

    # the model loop was torn down (its `finally` ran) instead of producing 60 s of tokens
    assert wait_for(lambda: llm.upstream_closed), "upstream stream was never closed -> still generating/billing"

    saved = len(row["content"])
    time.sleep(0.4)
    assert len(db.get_message(row["id"])["content"]) == saved  # nothing more is being generated

    # and the conversation is immediately usable again
    assert not db.has_streaming_message(cid)

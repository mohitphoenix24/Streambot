import httpx
import httpx2
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.config import Settings
from tests.helpers import ScriptedLLM, ok_script


@pytest.fixture(autouse=True)
def forbid_real_network(monkeypatch):
    """SAFETY NET: unit tests must never reach OpenAI.

    The OpenAI SDK sends requests through httpx2.AsyncClient. If any test (or a
    future change) lets a real client slip through, this makes the test FAIL
    LOUDLY instead of silently spending money. (FastAPI's TestClient uses plain
    `httpx` and talks to the app in-process, so it is unaffected.)
    """

    async def blocked(self, *args, **kwargs):
        raise AssertionError("A test tried to make a real network request (possibly to OpenAI).")

    monkeypatch.setattr(httpx2.AsyncClient, "send", blocked)
    monkeypatch.setattr(httpx.AsyncClient, "send", blocked)  # also the live FX lookup


@pytest.fixture
def settings(tmp_path):
    return Settings(
        openai_api_key="test-key",  # never used: tests inject a ScriptedLLM
        openai_model="test-model",
        database_path=tmp_path / "chat.db",
        prices_path=tmp_path / "prices.toml",  # absent -> no prices unless a test writes one
        rate_limit_per_minute=0,
        usd_to_inr=88.0,  # fixed: tests must not fetch a live exchange rate
        max_message_chars=200,
        history_budget_tokens=6000,
        partial_flush_interval_s=0.0,
    )


@pytest.fixture
def make_client(settings):
    clients = []

    def _make(llm=None, **overrides):
        s = Settings(**{**settings.__dict__, **overrides})
        client = TestClient(create_app(s, llm or ScriptedLLM(ok_script("hi"))))
        client.__enter__()  # runs lifespan: schema creation + crash recovery
        clients.append(client)
        return client

    yield _make
    for c in clients:
        c.__exit__(None, None, None)

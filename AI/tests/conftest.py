import os
import uuid

import httpx
import httpx2
import psycopg
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.auth.passwords import hash_password
from app.auth.tokens import create_token
from app.config import Settings
from app.db import Database
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


# ── accounts for tests ───────────────────────────────────────────────────
# argon2 is slow ON PURPOSE (~100 ms and 64 MB per hash), so hash the test password ONCE and
# reuse it. The auth tests themselves use the real hasher.
TEST_PASSWORD = "correct-horse-battery"
TEST_PASSWORD_HASH = hash_password(TEST_PASSWORD)
TEST_JWT_SECRET = "test-secret-" + "x" * 40


# ── PostgreSQL for tests ─────────────────────────────────────────────────
# Every test gets its OWN throwaway database, cloned from a migrated template. Cloning is a
# file copy inside Postgres (tens of milliseconds), so tests stay isolated without
# re-running the migrations, and cannot interfere with each other or with your real data.
# Start the server first:  docker compose up -d postgres
ADMIN_URL = os.environ.get("TEST_DATABASE_URL", "postgresql://streambot:streambot@localhost:5440/postgres")
TEMPLATE = "streambot_test_template"


def _url_for(name: str) -> str:
    base, _, _ = ADMIN_URL.rpartition("/")
    return f"{base}/{name}"


@pytest.fixture(scope="session")
def template_database():
    try:
        admin = psycopg.connect(ADMIN_URL, autocommit=True, connect_timeout=5)
    except psycopg.OperationalError as exc:  # pragma: no cover - environment problem, say what to do
        pytest.exit(f"Cannot reach PostgreSQL for the tests ({exc}).\nStart it with: docker compose up -d postgres", returncode=2)
    with admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{TEMPLATE}" WITH (FORCE)')
        admin.execute(f'CREATE DATABASE "{TEMPLATE}"')
        template = Database(_url_for(TEMPLATE))
        template.init_schema()  # runs every migration once
        template.close()  # a template must have no open connections
        yield TEMPLATE
        admin.execute(f'DROP DATABASE IF EXISTS "{TEMPLATE}" WITH (FORCE)')


@pytest.fixture
def database_url(template_database):
    name = f"t_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}" TEMPLATE "{template_database}"')
        yield _url_for(name)
        admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture
def db(database_url):
    database = Database(database_url)
    yield database
    database.close()


@pytest.fixture
def user(db):
    """An account in the test database."""
    return db.create_user("user@example.com", TEST_PASSWORD_HASH)


@pytest.fixture
def uid(user):
    return user["id"]


@pytest.fixture
def settings(tmp_path, database_url):
    return Settings(
        openai_api_key="test-key",  # never used: tests inject a ScriptedLLM
        openai_model="test-model",
        database_url=database_url,
        jwt_secret=TEST_JWT_SECRET,
        auth_rate_limit_per_minute=0,  # the auth tests that need a limit turn it on
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

    def _make(llm=None, search=None, **overrides):
        s = Settings(**{**settings.__dict__, **overrides})
        app = create_app(s, llm or ScriptedLLM(ok_script("hi")), search=search)
        client = TestClient(app)
        client.__enter__()  # runs lifespan: schema creation + crash recovery
        clients.append(client)
        # Log in by default, so the endpoint tests exercise the real, protected API.
        # get-or-create: a test that "restarts the server" must get the SAME account back
        client.user = app.state.db.get_user_by_email("user@example.com") or app.state.db.create_user("user@example.com", TEST_PASSWORD_HASH)
        client.headers["Authorization"] = f"Bearer {create_token(client.user['id'], s.jwt_secret, 7)}"
        return client

    yield _make
    for c in clients:
        c.__exit__(None, None, None)


def client_for(client, email="other@example.com"):
    """A second logged-in user talking to the SAME app and database (for isolation tests)."""
    app = client.app
    other = TestClient(app)
    other.user = app.state.db.create_user(email, TEST_PASSWORD_HASH)
    other.headers["Authorization"] = f"Bearer {create_token(other.user['id'], app.state.settings.jwt_secret, 7)}"
    return other

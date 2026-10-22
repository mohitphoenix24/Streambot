"""Authentication: passwords, tokens, the login routes, and (most important) that nothing
else in the API is reachable without a valid token, and that one user can never see another's data."""
import time
import uuid

import jwt
import psycopg
import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.auth.passwords import hash_password, needs_rehash, verify_password
from app.auth.tokens import TokenError, create_token, decode_token
from app.config import Settings
from app.db import Database
from app.main import create_app
from app.migrations import MIGRATIONS_DIR, migrate
from tests.conftest import ADMIN_URL, TEST_JWT_SECRET, TEST_PASSWORD, _url_for, client_for
from tests.helpers import ScriptedLLM, ok_script

PUBLIC = {"/health", "/auth/signup", "/auth/login"}


def signup(client, email="new@example.com", password="a-good-password"):
    return client.post("/auth/signup", json={"email": email, "password": password})


@pytest.fixture
def anon(make_client):
    """A client with NO token."""
    logged_in = make_client()
    plain = TestClient(logged_in.app)
    return plain


# ── passwords ────────────────────────────────────────────────────────────
def test_a_password_is_hashed_salted_and_verifiable():
    h1, h2 = hash_password("s3cret-pass"), hash_password("s3cret-pass")
    assert h1.startswith("$argon2id$") and "s3cret-pass" not in h1
    assert h1 != h2  # a random salt: the same password never gives the same hash
    assert verify_password("s3cret-pass", h1) and not verify_password("wrong", h1)


@pytest.mark.parametrize("bad", ["", "!", "not a hash", "$argon2id$garbage"])
def test_verifying_against_a_malformed_hash_is_just_false(bad):
    assert verify_password("anything", bad) is False  # never raises, so a legacy account simply cannot log in


def test_a_fresh_hash_does_not_need_rehashing():
    assert needs_rehash(hash_password("x" * 10)) is False


# ── tokens ───────────────────────────────────────────────────────────────
def test_a_token_round_trips_to_the_user_id():
    assert decode_token(create_token("user-1", TEST_JWT_SECRET, 7), TEST_JWT_SECRET) == "user-1"


def test_an_expired_token_is_rejected():
    old = create_token("user-1", TEST_JWT_SECRET, 7, now=time.time() - 8 * 86400)
    with pytest.raises(TokenError):
        decode_token(old, TEST_JWT_SECRET)


def test_a_token_signed_with_another_secret_is_rejected():
    forged = create_token("user-1", "some-other-secret-" + "y" * 30, 7)
    with pytest.raises(TokenError):
        decode_token(forged, TEST_JWT_SECRET)


def test_a_tampered_payload_is_rejected():
    head, payload, sig = create_token("user-1", TEST_JWT_SECRET, 7).split(".")
    evil = jwt.utils.base64url_encode(b'{"sub":"admin","exp":9999999999}').decode()
    with pytest.raises(TokenError):
        decode_token(f"{head}.{evil}.{sig}", TEST_JWT_SECRET)


def test_the_alg_none_attack_is_rejected():
    unsigned = jwt.encode({"sub": "user-1", "exp": int(time.time()) + 999}, key=None, algorithm="none")
    with pytest.raises(TokenError):
        decode_token(unsigned, TEST_JWT_SECRET)


@pytest.mark.parametrize("claims", [{"sub": "u"}, {"exp": 9999999999}])
def test_a_token_missing_exp_or_sub_is_rejected(claims):
    with pytest.raises(TokenError):
        decode_token(jwt.encode(claims, TEST_JWT_SECRET, algorithm="HS256"), TEST_JWT_SECRET)


# ── signup ───────────────────────────────────────────────────────────────
def test_signup_returns_a_working_token_and_stores_only_a_hash(anon, db):
    r = signup(anon, "  New@Example.COM ")
    assert r.status_code == 201
    body = r.json()
    assert body["user"]["email"] == "new@example.com"  # trimmed and lowercased

    me = anon.get("/auth/me", headers={"Authorization": f"Bearer {body['token']}"})
    assert me.status_code == 200 and me.json()["id"] == body["user"]["id"]

    stored = db.get_user_by_email("new@example.com")
    assert stored["password_hash"].startswith("$argon2id$") and "a-good-password" not in stored["password_hash"]


def test_the_same_email_cannot_sign_up_twice_whatever_its_case(anon):
    assert signup(anon, "dup@example.com").status_code == 201
    assert signup(anon, "DUP@example.com").status_code == 409


@pytest.mark.parametrize("email", ["", "no-at-sign", "a@b", "two@@example.com", "spaces in@example.com", "x" * 300 + "@example.com"])
def test_signup_rejects_bad_emails(anon, email):
    assert signup(anon, email).status_code == 422


def test_signup_enforces_password_length_limits(anon):
    short = signup(anon, "a@example.com", "1234567")
    assert short.status_code == 422 and "at least 8" in short.json()["detail"]
    assert signup(anon, "b@example.com", "x" * 129).status_code == 422  # a megabyte "password" would be a cheap DoS


def test_signup_can_be_switched_off(make_client):
    closed = TestClient(make_client(signup_enabled=False).app)
    assert signup(closed).status_code == 403


# ── login ────────────────────────────────────────────────────────────────
def test_login_works_with_the_right_password(anon, db):
    signup(anon, "me@example.com", TEST_PASSWORD)
    r = anon.post("/auth/login", json={"email": "ME@example.com", "password": TEST_PASSWORD})
    assert r.status_code == 200 and r.json()["user"]["email"] == "me@example.com" and r.json()["token"]


def test_a_wrong_password_and_an_unknown_email_look_exactly_the_same(anon):
    signup(anon, "real@example.com", "the-real-password")
    wrong_password = anon.post("/auth/login", json={"email": "real@example.com", "password": "nope-nope-nope"})
    unknown_email = anon.post("/auth/login", json={"email": "ghost@example.com", "password": "nope-nope-nope"})
    assert wrong_password.status_code == unknown_email.status_code == 401
    assert wrong_password.json() == unknown_email.json()  # no hint about which emails have accounts


def test_the_placeholder_legacy_account_can_never_log_in(anon, db):
    db.create_user("legacy@streambot.invalid", "!")  # what the migration inserts for pre-login data
    r = anon.post("/auth/login", json={"email": "legacy@streambot.invalid", "password": "!"})
    assert r.status_code == 401


def test_login_attempts_are_rate_limited(make_client):
    limited = TestClient(make_client(auth_rate_limit_per_minute=2).app)
    codes = [limited.post("/auth/login", json={"email": "x@example.com", "password": "whatever1"}).status_code for _ in range(3)]
    assert codes == [401, 401, 429]
    blocked = limited.post("/auth/login", json={"email": "x@example.com", "password": "whatever1"})
    assert int(blocked.headers["Retry-After"]) >= 1


# ── protection: nothing is reachable without a valid token ───────────────
def test_every_route_except_the_public_ones_requires_a_token(anon):
    """Walks the app's real route table, so a route added later cannot be forgotten."""
    checked = []
    for route in anon.app.routes:
        if not isinstance(route, APIRoute) or route.path in PUBLIC:
            continue
        path = route.path.replace("{conversation_id}", "x").replace("{document_id}", "x")
        for method in route.methods - {"HEAD", "OPTIONS"}:
            r = anon.request(method, path, json={})
            assert r.status_code == 401, f"{method} {route.path} is reachable without logging in ({r.status_code})"
            checked.append(f"{method} {route.path}")
    assert len(checked) >= 8, checked  # sanity: we really walked the routes


def test_the_public_routes_really_are_public(anon):
    assert anon.get("/health").status_code == 200


def test_a_missing_or_malformed_authorization_header_is_a_401(anon):
    assert anon.get("/conversations").status_code == 401
    assert anon.get("/conversations", headers={"Authorization": "Token abc"}).status_code == 401
    assert anon.get("/conversations", headers={"Authorization": "Bearer"}).status_code == 401
    assert anon.get("/conversations", headers={"Authorization": "Bearer not.a.jwt"}).status_code == 401


def test_an_expired_token_is_a_401_on_a_real_route(anon, make_client):
    user = make_client().user
    old = create_token(user["id"], TEST_JWT_SECRET, 7, now=time.time() - 30 * 86400)
    assert anon.get("/conversations", headers={"Authorization": f"Bearer {old}"}).status_code == 401


def test_a_token_for_a_deleted_user_stops_working(make_client, db):
    client = make_client()
    assert client.get("/conversations").status_code == 200
    with db.connect() as conn:
        conn.execute("DELETE FROM users WHERE id = %s", (client.user["id"],))
    assert client.get("/conversations").status_code == 401


# ── isolation: one user can never see another's data ─────────────────────
def test_users_only_see_their_own_conversations(make_client):
    alice = make_client(ScriptedLLM(ok_script("hi")))
    bob = client_for(alice, "bob@example.com")
    mine = alice.post("/conversations").json()["id"]
    theirs = bob.post("/conversations").json()["id"]

    assert [c["id"] for c in alice.get("/conversations").json()] == [mine]
    assert [c["id"] for c in bob.get("/conversations").json()] == [theirs]


def test_someone_elses_conversation_looks_like_it_does_not_exist(make_client):
    alice = make_client(ScriptedLLM(ok_script("hi")))
    bob = client_for(alice, "bob@example.com")
    cid = alice.post("/conversations").json()["id"]
    alice.post(f"/conversations/{cid}/messages", json={"text": "my secret"})

    # 404, not 403: the answer is identical to "no such conversation", so ids can't be probed
    assert bob.get(f"/conversations/{cid}").status_code == 404
    assert bob.delete(f"/conversations/{cid}").status_code == 404
    assert bob.post(f"/conversations/{cid}/messages", json={"text": "hi"}).status_code == 404
    assert bob.post(f"/conversations/{cid}/messages/sync", json={"text": "hi"}).status_code == 404
    assert bob.get(f"/conversations/{uuid.uuid4().hex}").status_code == 404  # same as unknown
    # ...and nothing was damaged: alice still has everything
    assert len(alice.get(f"/conversations/{cid}").json()["messages"]) == 2


def test_usage_totals_are_per_user(make_client):
    alice = make_client(ScriptedLLM(ok_script("hi")))
    bob = client_for(alice, "bob@example.com")
    cid = alice.post("/conversations").json()["id"]
    alice.post(f"/conversations/{cid}/messages", json={"text": "hello"})

    assert alice.get("/usage?days=7").json()["totals"]["replies"] == 1
    assert bob.get("/usage?days=7").json()["totals"]["replies"] == 0  # bob's page does not include alice's spend


def test_the_chat_rate_limit_is_per_user_not_shared(make_client):
    alice = make_client(ScriptedLLM(ok_script("hi")), rate_limit_per_minute=1)
    bob = client_for(alice, "bob@example.com")
    a, b = alice.post("/conversations").json()["id"], bob.post("/conversations").json()["id"]

    assert alice.post(f"/conversations/{a}/messages", json={"text": "1"}).status_code == 200
    assert alice.post(f"/conversations/{a}/messages", json={"text": "2"}).status_code == 429
    assert bob.post(f"/conversations/{b}/messages", json={"text": "1"}).status_code == 200  # bob is unaffected


# ── the accounts migration keeps pre-login data ──────────────────────────
def _fresh_database():
    name = f"t_{uuid.uuid4().hex[:10]}"
    admin = psycopg.connect(ADMIN_URL, autocommit=True)
    admin.execute(f'CREATE DATABASE "{name}"')
    return name, admin, Database(_url_for(name))


def test_pre_login_data_is_kept_under_a_legacy_account(tmp_path):
    name, admin, old = _fresh_database()
    try:
        (tmp_path / "001_initial.sql").write_text((MIGRATIONS_DIR / "001_initial.sql").read_text())
        migrate(old, tmp_path)  # the database as it was BEFORE accounts existed
        with old.connect() as conn:
            conn.execute("INSERT INTO conversations (id, title, created_at, updated_at) VALUES ('c1', 'old chat', now(), now())")
            conn.execute("INSERT INTO llm_calls (id, conversation_id, purpose, model, created_at) VALUES ('l1', 'c1', 'title', 'm', now())")

        assert migrate(old)[0] == "002_users"  # now upgrade to the current schema (later migrations follow)

        with old.connect() as conn:
            assert conn.execute("SELECT user_id FROM conversations WHERE id = 'c1'").fetchone()["user_id"] == "legacy"
            assert conn.execute("SELECT user_id FROM llm_calls WHERE id = 'l1'").fetchone()["user_id"] == "legacy"
            assert conn.execute("SELECT email FROM users WHERE id = 'legacy'").fetchone()["email"] == "legacy@streambot.invalid"
    finally:
        old.close()
        admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.close()


def test_an_empty_database_gets_no_legacy_account(db):
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM users WHERE id = 'legacy'").fetchone()["n"] == 0


# ── configuration ────────────────────────────────────────────────────────
@pytest.mark.parametrize("secret", [None, "", "short", "your_jwt_secret", "change_me", "x" * 31])
def test_a_missing_or_weak_jwt_secret_stops_the_server_from_starting(secret):
    with pytest.raises(Exception, match="JWT_SECRET"):
        Settings(openai_api_key="k", openai_model="m", jwt_secret=secret).validate()


def test_a_long_random_secret_is_accepted():
    Settings(openai_api_key="k", openai_model="m", jwt_secret="a" * 32).validate()

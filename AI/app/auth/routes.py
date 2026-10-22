"""Sign up, log in, and the dependency that protects every other route.

    POST /auth/signup   {email, password}  ->  201 {token, user}
    POST /auth/login    {email, password}  ->  200 {token, user}
    GET  /auth/me       (needs a token)    ->  200 {id, email}

Everything else in the API depends on `current_user`: no valid token, no access.

Details that matter:

  * LOGIN ERRORS ARE DELIBERATELY VAGUE. "Incorrect email or password" for both a wrong
    password and an unknown email, so the endpoint cannot be used to discover which emails have
    accounts. For the same reason an unknown email still pays for a full password check
    (against a dummy hash), so the response time does not give it away either.
  * Hashing a password is slow ON PURPOSE (that is what makes stolen hashes hard to crack), so
    it runs in a worker thread and never blocks the event loop that serves everyone else.
  * Signup and login are rate limited per client IP, which is what slows password guessing.
  * Email is normalised to lowercase (the database enforces it too); passwords have a minimum
    length and a MAXIMUM (a megabyte-long "password" would be a cheap denial-of-service).
"""
from __future__ import annotations

import math
import re
from typing import Callable

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from app.auth.passwords import DUMMY_HASH, hash_password, needs_rehash, verify_password
from app.auth.tokens import TokenError, create_token, decode_token
from app.config import Settings
from app.core.limits import SlidingWindowLimiter
from app.db import Database

EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$")
MAX_PASSWORD_CHARS = 128


class Credentials(BaseModel):
    email: str
    password: str


class UserOut(BaseModel):
    id: str
    email: str


class AuthOut(BaseModel):
    token: str
    user: UserOut


def build_auth(settings: Settings, db: Database) -> tuple[APIRouter, Callable]:
    """The auth router, and the `current_user` dependency other routes use."""
    router = APIRouter(prefix="/auth", tags=["auth"])
    limiter = SlidingWindowLimiter(settings.auth_rate_limit_per_minute)
    bearer = HTTPBearer(auto_error=False)  # we raise our own, uniform 401

    def unauthorized(detail: str) -> HTTPException:
        return HTTPException(status_code=401, detail=detail, headers={"WWW-Authenticate": "Bearer"})

    def current_user(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)) -> dict:
        """The logged-in user, or 401. A token for a user that no longer exists is rejected too."""
        if credentials is None:
            raise unauthorized("Not authenticated")
        try:
            user_id = decode_token(credentials.credentials, settings.jwt_secret or "")
        except TokenError:
            raise unauthorized("Invalid or expired token") from None
        user = db.get_user(user_id)
        if user is None:
            raise unauthorized("Invalid or expired token")
        return {"id": user["id"], "email": user["email"]}

    def throttle(request: Request) -> None:
        wait = limiter.check(request.client.host if request.client else "unknown")
        if wait is not None:
            raise HTTPException(
                status_code=429,
                detail="Too many attempts. Please wait a moment and try again.",
                headers={"Retry-After": str(math.ceil(wait))},
            )

    def issue(user: dict) -> AuthOut:
        token = create_token(user["id"], settings.jwt_secret or "", settings.jwt_expires_days)
        return AuthOut(token=token, user=UserOut(id=user["id"], email=user["email"]))

    @router.post("/signup", response_model=AuthOut, status_code=201)
    async def signup(body: Credentials, request: Request) -> AuthOut:
        if not settings.signup_enabled:
            raise HTTPException(status_code=403, detail="Sign-ups are disabled on this server.")
        throttle(request)
        email = body.email.strip().lower()
        if len(email) > 254 or not EMAIL.match(email):
            raise HTTPException(status_code=422, detail="Enter a valid email address.")
        if len(body.password) < settings.password_min_length:
            raise HTTPException(
                status_code=422, detail=f"Password must be at least {settings.password_min_length} characters."
            )
        if len(body.password) > MAX_PASSWORD_CHARS:
            raise HTTPException(status_code=422, detail=f"Password must be at most {MAX_PASSWORD_CHARS} characters.")
        password_hash = await run_in_threadpool(hash_password, body.password)
        try:
            user = db.create_user(email, password_hash)
        except psycopg.errors.UniqueViolation:
            raise HTTPException(status_code=409, detail="An account with this email already exists.") from None
        return issue(user)

    @router.post("/login", response_model=AuthOut)
    async def login(body: Credentials, request: Request) -> AuthOut:
        throttle(request)
        user = db.get_user_by_email(body.email)
        # Unknown email: still do a full password check, so timing does not reveal it.
        ok = await run_in_threadpool(verify_password, body.password[: MAX_PASSWORD_CHARS * 2], user["password_hash"] if user else DUMMY_HASH)
        if user is None or not ok:
            raise unauthorized("Incorrect email or password")
        if needs_rehash(user["password_hash"]):  # hashing settings were strengthened since this account was made
            db.set_password_hash(user["id"], await run_in_threadpool(hash_password, body.password))
        return issue(user)

    @router.get("/me", response_model=UserOut)
    async def me(user: dict = Depends(current_user)) -> UserOut:
        return UserOut(**user)

    return router, current_user

"""JSON Web Tokens (JWT): how a logged-in browser proves who it is.

After a successful login the server hands the browser a signed token. The browser sends it
back on every request (Authorization: Bearer <token>) and the server only has to VERIFY the
signature, with no session table to look up.

A JWT is three base64 parts joined by dots: header.payload.signature.

    header     {"alg": "HS256", "typ": "JWT"}
    payload    {"sub": "<user id>", "iat": <issued at>, "exp": <expires at>}
    signature  HMAC-SHA256(header + "." + payload, JWT_SECRET)

The payload is NOT encrypted (anyone can read it), it is only SIGNED. Nobody can change it,
for example swap in another user id, without invalidating the signature, because they do not
have the secret. So never put anything private in a token.

The `exp` claim makes the token expire by itself (PyJWT rejects an expired token). That is
the whole logout story for a stateless token: the browser deletes it, and if it is stolen it
stops working at `exp`.
"""
from __future__ import annotations

import time

import jwt

ALGORITHM = "HS256"


class TokenError(Exception):
    """The token is missing, malformed, tampered with, or expired. Message is for logs only."""


def create_token(user_id: str, secret: str, expires_days: float, now: float | None = None) -> str:
    issued = int(now if now is not None else time.time())
    payload = {"sub": user_id, "iat": issued, "exp": issued + int(expires_days * 86400)}
    return jwt.encode(payload, secret, algorithm=ALGORITHM)


def decode_token(token: str, secret: str) -> str:
    """Return the user id inside a valid token, or raise TokenError."""
    try:
        payload = jwt.decode(
            token,
            secret,
            algorithms=[ALGORITHM],  # NEVER accept the algorithm named in the token itself ("alg: none" attack)
            options={"require": ["exp", "sub"]},
        )
    except jwt.PyJWTError as exc:
        raise TokenError(type(exc).__name__) from exc
    sub = payload.get("sub")
    if not isinstance(sub, str) or not sub:
        raise TokenError("bad subject")
    return sub

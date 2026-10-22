---
title: Accounts and login with JWT
level: medium
time: 50 min
summary: Why a multi-user app must know who is calling, how passwords are stored (argon2id), how a signed token (JWT) proves identity without a session table, how every query is scoped to the user, and the trade-offs of this design.
tags: authentication, JWT, argon2id, password hashing, authorization, isolation, rate limiting
---

For its first versions Streambot was a single-user app: anyone who could reach the API saw every conversation. The moment more than one person uses it, three questions appear:

1. **Authentication**: *who is calling?* (login)
2. **Authorization**: *what may they see and do?* (their own chats, nobody else's)
3. **Abuse control**: *how do we stop one caller from hurting the others?* (rate limits)

In Streambot **login is required**: without a valid token the chat endpoints answer `401`, and the browser shows only the sign-in screen.

:::flow From sign-up to a protected request
Sign up / log in | the browser sends `{email, password}` to `/auth/signup` or `/auth/login`
Verify | the server checks the password against a stored *hash* (never the password)
Issue | it answers with a **JWT**, a signed token that says "this is user X until time T"
Store | the browser keeps the token (in `localStorage`)
Every request | `Authorization: Bearer <token>`
Check | the server verifies the signature and expiry, loads the user, and filters every query by that user's id
:::

## Passwords are never stored

If the database leaks, anything stored in clear text is lost. So Streambot stores a **hash**: the output of a one-way function. To check a login it hashes the attempt the same way and compares.

Two ingredients make that safe:

- **A salt**: random bytes mixed in per user, so two people with the same password get different hashes and a precomputed table is useless.
- **A deliberately slow function.** SHA-256 is built to be *fast*, which is exactly what a password-cracking program wants. Password hashes (argon2id, bcrypt, scrypt) are built to be slow, and argon2id also needs lots of memory, so every guess costs an attacker real time and hardware.

```python src="AI/app/auth/passwords.py" fn="hash_password,verify_password,needs_rehash"
```

Streambot uses **argon2id**, the current OWASP recommendation. `needs_rehash` upgrades an old hash at the next successful login if the settings were strengthened since. Try the idea with the standard library (this demo stretches SHA-256 by hand to stay runnable in the browser; real argon2id is stronger):

```python run title="Salt and slowness (a teaching version of a password hash)" level=medium
import hashlib, hmac, os, time

def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)                        # a random salt, different for every user
    digest = hashlib.sha256(salt + password.encode()).digest()
    for _ in range(50_000):                              # make every guess expensive
        digest = hashlib.sha256(digest + salt).digest()
    return salt.hex() + "$" + digest.hex()

def verify_password(password: str, stored: str) -> bool:
    salt_hex, digest_hex = stored.split("$")
    again = hash_password(password, bytes.fromhex(salt_hex)).split("$")[1]
    return hmac.compare_digest(again, digest_hex)        # constant-time comparison

a = hash_password("correct-horse-battery")
b = hash_password("correct-horse-battery")
print("same password, two accounts:")
print("  ", a[:40] + "...")
print("  ", b[:40] + "...   <- different, because the salt differs")
print("right password:", verify_password("correct-horse-battery", a))
print("wrong password:", verify_password("correct-horse-batterY", a))

start = time.perf_counter()
for _ in range(3):
    hash_password("guess")
per_guess = (time.perf_counter() - start) / 3
print(f"\none guess costs about {per_guess * 1000:.0f} ms here, so an attacker with a stolen database pays that for EVERY guess.")
```

## What a JWT is

A **JSON Web Token** is three base64 pieces joined by dots: `header.payload.signature`.

```text
header     {"alg": "HS256", "typ": "JWT"}
payload    {"sub": "<user id>", "iat": <issued at>, "exp": <expires at>}
signature  HMAC-SHA256(header + "." + payload, JWT_SECRET)
```

:::def JWT: signed, not encrypted
Anyone can read the payload. What they cannot do is **change** it: the signature is computed with a secret only the server has, so editing the payload (say, swapping in another user's id) breaks the signature. Never put anything private in a token.
:::

Streambot's token code is short enough to read in full:

```python src="AI/app/auth/tokens.py" fn="create_token,decode_token"
```

The important lines are in `decode_token`:

- **`algorithms=[ALGORITHM]`** pins HS256. A famous attack sends a token whose header says `"alg": "none"` (no signature needed). A verifier that obeys the *token's* choice of algorithm accepts it; ours never does.
- **`require: ["exp", "sub"]`** means a token without an expiry or a subject is rejected.
- Any library error becomes a plain `TokenError`, so a bad token is always just "401".

Build the whole mechanism by hand and attack it:

```python run title="A JWT from scratch, and four ways to fail" level=medium
import base64, hashlib, hmac, json, time

def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

def unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))

def sign(payload: dict, secret: str) -> str:
    header = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    body = b64(json.dumps(payload).encode())
    signature = hmac.new(secret.encode(), f"{header}.{body}".encode(), hashlib.sha256).digest()
    return f"{header}.{body}.{b64(signature)}"

def verify(token: str, secret: str, now: float) -> str:
    header_b64, body_b64, signature_b64 = token.split(".")
    header = json.loads(unb64(header_b64))
    if header.get("alg") != "HS256":                      # never trust the algorithm the TOKEN asks for
        return "REJECTED: unexpected algorithm " + repr(header.get("alg"))
    expected = hmac.new(secret.encode(), f"{header_b64}.{body_b64}".encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(expected, unb64(signature_b64)):
        return "REJECTED: signature does not match (tampered, or signed with another secret)"
    payload = json.loads(unb64(body_b64))
    if payload["exp"] <= now:
        return "REJECTED: expired"
    return f"OK: logged in as user {payload['sub']}"

SECRET = "a-long-random-server-secret"
now = time.time()
token = sign({"sub": "user-42", "iat": int(now), "exp": int(now) + 7 * 86400}, SECRET)

# 1. The payload is only SIGNED, not hidden: anyone can read it.
print("payload anyone can read:", json.loads(unb64(token.split(".")[1])))
print("valid token            :", verify(token, SECRET, now))

# 2. Try to become user-1 by editing the payload but keeping the old signature.
head, body, sig = token.split(".")
forged_body = b64(json.dumps({"sub": "user-1", "iat": int(now), "exp": int(now) + 7 * 86400}).encode())
print("edited payload         :", verify(f"{head}.{forged_body}.{sig}", SECRET, now))

# 3. Sign your own token without knowing the secret.
print("signed with wrong key  :", verify(sign({"sub": "user-1", "exp": int(now) + 999}, "guess"), SECRET, now))

# 4. The 'alg: none' trick: a token that claims it needs no signature at all.
none_header = b64(json.dumps({"alg": "none", "typ": "JWT"}).encode())
print("alg none               :", verify(f"{none_header}.{forged_body}.", SECRET, now))

# 5. Time passes: after 7 days the token stops working by itself.
print("eight days later       :", verify(token, SECRET, now + 8 * 86400))
```

Streambot's tokens last **7 days** (`JWT_EXPIRES_DAYS`). The `JWT_SECRET` is the crown jewel: anyone who knows it can mint a login for *any* user, so the server refuses to start with a missing, placeholder or short (under 32 characters) secret.

## The login routes

```python src="AI/app/auth/routes.py" from='@router.post("/login", response_model=AuthOut)' to="return issue(user)"
```

Every detail is a defence:

- **The error is vague on purpose.** A wrong password and an unknown email both answer *"Incorrect email or password"*, so the endpoint cannot be used to discover which emails have accounts.
- **Timing is equalised.** For an unknown email the server still verifies the password against a *dummy hash*, so the response time does not give the secret away either.
- **Hashing runs in a worker thread** (`run_in_threadpool`). It is slow on purpose, and must not block the event loop that is serving everyone else's streams.
- **Rate limiting per IP** slows password guessing (`AUTH_RATE_LIMIT_PER_MINUTE`).
- Sign-up lower-cases the email, and the database *enforces* it (`CHECK (email = lower(email))`), so `A@x.com` and `a@x.com` can never be two accounts.

## One dependency protects everything

```python src="AI/app/auth/routes.py" from="def current_user(" to='return {"id": user["id"], "email": user["email"]}'
```

Every other route declares `user: dict = Depends(current_user)`. No token, a bad token, an expired token, or a token for a user that was deleted all end in the same `401`. A test walks the app's **real route table** and proves that every route except `/health` and the two login routes refuses an anonymous caller, so a route added next month cannot forget to be protected:

```python src="AI/tests/test_auth.py" fn="test_every_route_except_the_public_ones_requires_a_token"
```

## Authorization: your data is yours

Knowing *who* is calling is only half of it. Every conversation, document, chunk and cost row has an owner, and **every query filters on it**:

```python src="AI/app/db.py" fn="Database.get_conversation,Database.list_conversations"
```

Asking for someone else's chat finds *nothing*, which is the same answer as a chat that does not exist. The API turns that into **`404`, not `403`**: a `403` would confirm that the id exists, and ids could be used to probe other accounts.

```python src="AI/app/main.py" from="def require_conversation(" to="return conv"
```

Background work (a summary, a title) starts from a conversation id the *server* already trusts, never from a request, which is why `conversation_owner` exists as a separate, clearly labelled method. And document search filters on `user_id` *and* `conversation_id` inside the SQL itself (see the retrieval lesson).

## Rate limits are per user

The chat limit (`RATE_LIMIT_PER_MINUTE`) used to be per IP address. Behind an office or a phone network many people share one IP, so it is now **per user**: one noisy account cannot lock out the others, and one person cannot dodge the limit by changing IP. Login attempts remain per IP, because at that point there is no user yet.

## In the browser

The token is kept in `localStorage` and sent by one wrapper around `fetch`; a `401` anywhere signs you out once (see the client lesson). The known weakness of `localStorage` is that a script running in the page could read the token, which is why LLM output is rendered through a sanitising pipeline with no raw HTML and no images.

## Trade-offs, honestly

| Choice | Benefit | Cost |
|---|---|---|
| **Stateless JWT** | no session table; any server instance can verify | cannot be revoked before `exp` (stolen = valid until it expires); logout just deletes it client-side |
| **7-day lifetime** | fewer sign-ins | a longer window if a token is stolen |
| **`localStorage`** | simple, works with any API client | readable by injected scripts (XSS); an `httpOnly` cookie is stricter but needs CSRF protection |
| **One shared secret (HS256)** | simple | every verifier can also mint tokens; asymmetric keys (RS256/EdDSA) separate the two |

Production steps beyond this project: refresh tokens with server-side revocation, an `httpOnly` cookie, email verification and password reset, OAuth login, and account lockout.

:::keypoints
- **Authentication** = who; **authorization** = what they may touch. Both are needed.
- Store a **salted, slow hash** (argon2id), never the password. Re-hash at login when settings improve.
- A **JWT** is `header.payload.signature`: *signed, not encrypted*. The server needs only the secret to verify it.
- Pin the **algorithm**, require **`exp`** and **`sub`**, and keep **`JWT_SECRET`** long, random and private.
- Make login errors **vague** and **timing equal**; rate limit by IP; hash in a worker thread.
- Protect routes with **one dependency**, and **test the whole route table** so none is forgotten.
- Filter **every query by owner**; answer `404`, not `403`, for other people's data.
- JWT trade-off: no revocation before expiry.
:::

:::exercise Why not 403?
A user asks for `GET /conversations/abc123`, which belongs to someone else. Streambot answers `404 Conversation not found`. What would be wrong with `403 Forbidden`?
:::

:::solution Show the answer
`403` tells the caller "this exists, but you may not see it". An attacker could try thousands of ids and learn which ones are real, or confirm that a particular person has a conversation. Answering `404` for both "missing" and "someone else's" reveals nothing. It is the same reason login says "Incorrect email or password" instead of saying which of the two was wrong.
:::

```quiz
? Why is a JWT payload safe to be readable by the user?
- Because it is encrypted with the secret.
+ Because it is only signed: reading it is harmless, and changing it breaks the signature.
- Because the browser hides it.
> Never put anything private in a token; its protection is integrity, not secrecy.

? A token arrives whose header says "alg": "none". What does the verifier do?
- Accepts it, since no signature is needed.
+ Rejects it: the allowed algorithm list is fixed on the server, never taken from the token.
- Asks the user to log in again.
> Trusting the token's own claim about how to verify it is a classic vulnerability.

? Why does login still hash the password when the email is unknown?
- To save the attempt in a log.
+ So the response takes the same time, and timing cannot reveal which emails have accounts.
- Because the database needs the hash.
> The error text AND the timing must be identical for both cases.

? What is the main weakness of a stateless JWT?
- It cannot carry a user id.
+ It cannot be revoked before it expires; a stolen token works until `exp`.
- It is too large to send.
> That is why production systems add short lifetimes, refresh tokens and server-side revocation.
```

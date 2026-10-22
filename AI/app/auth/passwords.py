"""Password hashing with argon2id.

Passwords are never stored. We store a HASH: the output of a deliberately slow, memory-hungry
one-way function, with a random salt built in. Checking a login means hashing the attempt the
same way and comparing. If the database leaks, the attacker gets hashes that are very
expensive to reverse, not passwords.

Why argon2id and not SHA-256 or MD5: those are built to be FAST, which is exactly what a
password-cracking program wants. argon2id is built to be slow and to need lots of memory, so
each guess costs an attacker real time and hardware (it won the Password Hashing Competition
and is the current OWASP recommendation).
"""
from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

_hasher = PasswordHasher()  # sensible defaults: argon2id, 64 MiB, 3 passes


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """True only if `password` matches. Never raises on a bad or malformed hash."""
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    """True if the hash was made with weaker settings than today's defaults (upgrade it at login)."""
    return _hasher.check_needs_rehash(password_hash)


# A real hash of a random string, used to spend the SAME time when the email is unknown, so
# response time does not reveal which emails have accounts.
DUMMY_HASH = _hasher.hash("not-a-real-password-just-for-timing")

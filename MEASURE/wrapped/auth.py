"""Signed access tokens. A token names exactly one (user, year); the API never takes a user id from the URL.

This is the "magic link" a product would email: `https://app/#t=<token>`. HMAC-SHA256 over a
small claim string, stdlib only.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class Claims:
    user_id: int
    year: int
    expires_at: int


def _sign(secret: str, body: str) -> str:
    return hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()


def mint(secret: str, user_id: int, year: int, ttl_seconds: int = 90 * 86400, now: float | None = None) -> str:
    expires_at = int((now if now is not None else time.time()) + ttl_seconds)
    body = base64.urlsafe_b64encode(f"{user_id}:{year}:{expires_at}".encode()).decode().rstrip("=")
    return f"{body}.{_sign(secret, body)}"


def verify(secret: str, token: str, now: float | None = None) -> Claims | None:
    """Return the claims, or None for anything malformed, forged or expired."""
    body, _, sig = token.partition(".")
    if not body or not sig or len(token) > 512:
        return None
    if not hmac.compare_digest(_sign(secret, body), sig):
        return None
    try:
        user_id, year, expires_at = (int(p) for p in base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).decode().split(":"))
    except ValueError:  # covers bad base64, bad utf-8 and wrong arity
        return None
    if expires_at < (now if now is not None else time.time()):
        return None
    return Claims(user_id, year, expires_at)


def is_admin(expected: str, presented: str) -> bool:
    return hmac.compare_digest(expected.encode(), presented.encode())

"""Dependency-free password hashing + signed tokens (stdlib only).

Password hashing: PBKDF2-HMAC-SHA256. Tokens: a compact HMAC-SHA256-signed
`<payload>.<sig>` (JWT-ish, but no external lib). Good enough for a board-farm
auth foundation; swap in a real IdP/JWT lib later without touching callers.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Optional

_PBKDF2_ITERS = 200_000


def hash_password(password: str, *, iterations: int = _PBKDF2_ITERS) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return "$".join(
        ["pbkdf2_sha256", str(iterations), _b64e(salt), _b64e(dk)]
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt_b64, dk_b64 = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        salt, expected = _b64d(salt_b64), _b64d(dk_b64)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, int(iters))
        return hmac.compare_digest(dk, expected)
    except Exception:
        return False


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def issue_token(payload: dict, secret: str, *, ttl_seconds: int = 8 * 3600) -> str:
    body = {**payload, "exp": int(time.time()) + ttl_seconds}
    raw = _b64e(json.dumps(body, separators=(",", ":"), sort_keys=True).encode())
    sig = _b64e(hmac.new(secret.encode(), raw.encode(), hashlib.sha256).digest())
    return f"{raw}.{sig}"


def verify_token(token: str, secret: str) -> Optional[dict]:
    try:
        raw, sig = token.split(".")
        expected = _b64e(hmac.new(secret.encode(), raw.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expected):
            return None
        body = json.loads(_b64d(raw))
        if int(body.get("exp", 0)) < int(time.time()):
            return None
        return body
    except Exception:
        return None

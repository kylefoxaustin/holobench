# SPDX-License-Identifier: Apache-2.0
"""Auth core tests: passwords, tokens, user store, open-mode service."""

import time

from holobench.auth import (
    AuthService,
    UserStore,
    hash_password,
    issue_token,
    verify_password,
    verify_token,
)
from holobench.auth.crypto import _b64e  # noqa: PLC2701


def test_password_roundtrip():
    h = hash_password("s3cret")
    assert h.startswith("pbkdf2_sha256$")
    assert verify_password("s3cret", h)
    assert not verify_password("wrong", h)


def test_token_roundtrip_and_tamper():
    t = issue_token({"sub": "alice", "role": "admin"}, "secret")
    body = verify_token(t, "secret")
    assert body and body["sub"] == "alice" and body["role"] == "admin"
    assert verify_token(t, "other-secret") is None          # wrong key
    assert verify_token(t + "x", "secret") is None           # tampered sig


def test_token_expiry():
    t = issue_token({"sub": "a"}, "secret", ttl_seconds=-1)
    assert verify_token(t, "secret") is None


def test_user_store(tmp_path):
    store = UserStore(tmp_path / "users.yaml")
    assert not store.configured                              # empty -> open mode
    store.add("alice", "pw", role="admin")
    assert store.configured
    assert store.authenticate("alice", "pw").is_admin
    assert store.authenticate("alice", "nope") is None
    assert store.get("alice").role == "admin"
    assert [u.username for u in store.list()] == ["alice"]
    # persisted across reloads
    assert UserStore(tmp_path / "users.yaml").authenticate("alice", "pw")
    assert store.remove("alice") and not store.configured


def test_auth_service_open_mode_then_enforced(tmp_path):
    svc = AuthService(store=UserStore(tmp_path / "u.yaml"), secret="k")
    assert not svc.enabled
    assert svc.resolve(None).is_admin                        # open mode -> admin
    svc.store.add("bob", "pw")
    assert svc.enabled
    assert svc.resolve(None) is None                         # now requires a token
    tok = svc.login("bob", "pw")
    assert tok and svc.resolve(tok).username == "bob"
    assert svc.login("bob", "bad") is None

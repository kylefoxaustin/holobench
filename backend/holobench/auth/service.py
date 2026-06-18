# SPDX-License-Identifier: GPL-2.0-or-later
"""AuthService: the single object the API talks to for auth.

Holds the user store + signing secret. When the store is unconfigured (no users)
it reports `enabled == False` and hands out a synthetic admin so Holobench keeps
working with no login (local/dev). Configure a user -> auth is enforced.
"""

from __future__ import annotations

import logging
import os
import secrets
from typing import Optional

from .crypto import issue_token, verify_token
from .store import User, UserStore

log = logging.getLogger("holobench.auth")

# Synthetic identity used in open mode (no users configured).
OPEN_MODE_USER = User(username="local", role="admin")


class AuthService:
    def __init__(
        self, store: Optional[UserStore] = None, secret: Optional[str] = None
    ) -> None:
        self.store = store or UserStore()
        env_secret = secret or os.environ.get("HOLOBENCH_SECRET")
        if env_secret:
            self.secret = env_secret
        elif self.store.configured:
            # Enforced auth, no env secret: persist an auto-generated key next to
            # the user store (0600) so logins survive a restart. A single explicit
            # HOLOBENCH_SECRET is still preferred for multi-worker/multi-host
            # (shared key); this removes the silent-logout footgun for a single
            # enforced instance.
            self.secret = self._load_or_create_persistent_secret()
        else:
            # Open mode (no users) — tokens are unused; an ephemeral key is fine.
            self.secret = secrets.token_hex(32)

    def _load_or_create_persistent_secret(self) -> str:
        path = self.store.path.parent / "secret"
        try:
            if path.exists():
                existing = path.read_text().strip()
                if existing:
                    return existing
            path.parent.mkdir(parents=True, exist_ok=True)
            s = secrets.token_hex(32)
            path.write_text(s)
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
            log.info("generated a persistent signing key at %s (override with HOLOBENCH_SECRET)", path)
            return s
        except OSError:
            log.warning(
                "could not persist a signing key (%s); using an ephemeral one — "
                "logins won't survive a restart. Set HOLOBENCH_SECRET.", path
            )
            return secrets.token_hex(32)

    @property
    def enabled(self) -> bool:
        return self.store.configured

    def login(self, username: str, password: str) -> Optional[str]:
        user = self.store.authenticate(username, password)
        if user is None:
            return None
        # Token lifetime: default 7 days so a long (multi-hour) build plus overnight
        # idle never logs the operator out mid-flow — the old hardcoded 8h TTL did
        # exactly that. Override with HOLOBENCH_TOKEN_TTL_HOURS (e.g. "0.5" in a
        # shared/hardened deployment). Followup: sliding renewal so an *active*
        # session (the UI polls every few seconds) never expires regardless of TTL.
        try:
            ttl = max(60, int(float(os.environ.get("HOLOBENCH_TOKEN_TTL_HOURS", "168")) * 3600))
        except (TypeError, ValueError):
            ttl = 168 * 3600
        return issue_token(
            {"sub": user.username, "role": user.role}, self.secret, ttl_seconds=ttl
        )

    def user_from_token(self, token: Optional[str]) -> Optional[User]:
        if not token:
            return None
        body = verify_token(token, self.secret)
        if not body:
            return None
        username = body.get("sub")
        # Re-resolve against the store so role changes / removals take effect.
        user = self.store.get(username) if username else None
        return user

    def resolve(self, token: Optional[str]) -> Optional[User]:
        """The current user for a request token, or the open-mode admin, or None
        (None == enabled-and-unauthenticated -> caller should 401)."""
        if not self.enabled:
            return OPEN_MODE_USER
        return self.user_from_token(token)

# SPDX-License-Identifier: Apache-2.0
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
        else:
            # Ephemeral per-process secret: fine for a single dev instance, but
            # tokens won't survive a restart / multiple workers. Set
            # HOLOBENCH_SECRET for a real deployment.
            self.secret = secrets.token_hex(32)
            if self.store.configured:
                log.warning(
                    "HOLOBENCH_SECRET not set — using an ephemeral signing key; "
                    "logins won't survive a restart. Set HOLOBENCH_SECRET in prod."
                )

    @property
    def enabled(self) -> bool:
        return self.store.configured

    def login(self, username: str, password: str) -> Optional[str]:
        user = self.store.authenticate(username, password)
        if user is None:
            return None
        return issue_token({"sub": user.username, "role": user.role}, self.secret)

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

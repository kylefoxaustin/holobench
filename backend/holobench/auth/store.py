"""User store backed by a YAML file.

Format (holobench-users.yaml):

    users:
      alice: { password_hash: "pbkdf2_sha256$...", role: admin }
      bob:   { password_hash: "pbkdf2_sha256$...", role: user }

If the file is absent / empty, the store is "unconfigured" -> Holobench runs in
OPEN mode (no login required, every request is a synthetic admin), preserving
the frictionless local experience. Create a user to switch on enforced auth.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

from .crypto import hash_password, verify_password

_REPO_ROOT = Path(__file__).resolve().parents[3]


def default_users_path() -> Path:
    env = os.environ.get("HOLOBENCH_USERS")
    return Path(env) if env else _REPO_ROOT / "data" / "users.yaml"


@dataclass
class User:
    username: str
    role: str = "user"  # "user" | "admin"

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


class UserStore:
    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path else default_users_path()
        self._users: dict[str, dict] = {}
        self.load()

    def load(self) -> None:
        try:
            raw = yaml.safe_load(self.path.read_text()) or {}
            self._users = dict(raw.get("users") or {})
        except FileNotFoundError:
            self._users = {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(yaml.safe_dump({"users": self._users}, sort_keys=True))
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    @property
    def configured(self) -> bool:
        """True once at least one user exists -> enforce auth."""
        return bool(self._users)

    def add(self, username: str, password: str, role: str = "user") -> User:
        if role not in ("user", "admin"):
            raise ValueError("role must be 'user' or 'admin'")
        self._users[username] = {
            "password_hash": hash_password(password),
            "role": role,
        }
        self.save()
        return User(username, role)

    def remove(self, username: str) -> bool:
        existed = username in self._users
        self._users.pop(username, None)
        if existed:
            self.save()
        return existed

    def get(self, username: str) -> Optional[User]:
        rec = self._users.get(username)
        return User(username, rec.get("role", "user")) if rec else None

    def authenticate(self, username: str, password: str) -> Optional[User]:
        rec = self._users.get(username)
        if rec and verify_password(password, rec.get("password_hash", "")):
            return User(username, rec.get("role", "user"))
        return None

    def list(self) -> list[User]:
        return [User(u, r.get("role", "user")) for u, r in sorted(self._users.items())]

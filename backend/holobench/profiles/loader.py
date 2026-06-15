# SPDX-License-Identifier: GPL-2.0-or-later
"""Load and validate board profiles from YAML.

Profiles live in the repo's top-level ``profiles/`` directory. The loader
reads, validates against the pydantic models, and enforces that a profile's
``id`` matches its filename so lookups by id are unambiguous.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import ValidationError

from .models import Profile

# Repo root is three levels up from this file:
# backend/holobench/profiles/loader.py -> backend/holobench/profiles ->
# backend/holobench -> backend -> <repo root>
_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_PROFILE_DIR = _REPO_ROOT / "profiles"
DEFAULT_ASSET_ROOT = _REPO_ROOT / "assets"


def default_asset_dir(profile_id: str) -> Path | None:
    """Convention: boot artifacts for a board live in assets/<id>/.

    HOLOBENCH_ASSET_ROOT overrides the asset root (used by the container image).
    """
    root = os.environ.get("HOLOBENCH_ASSET_ROOT")
    base = Path(root) if root else DEFAULT_ASSET_ROOT
    d = base / profile_id
    return d if d.is_dir() else None


class ProfileError(Exception):
    """Raised when a profile cannot be found or fails validation."""


def _profile_dir(profile_dir: Path | str | None) -> Path:
    return Path(profile_dir) if profile_dir else DEFAULT_PROFILE_DIR


def load_profile_file(path: Path | str) -> Profile:
    """Load and validate a single profile YAML file."""
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text())
    except FileNotFoundError as exc:
        raise ProfileError(f"Profile file not found: {path}") from exc
    except yaml.YAMLError as exc:
        raise ProfileError(f"Profile {path} is not valid YAML: {exc}") from exc

    if not isinstance(raw, dict):
        raise ProfileError(f"Profile {path} must be a YAML mapping")

    try:
        profile = Profile.model_validate(raw)
    except ValidationError as exc:
        raise ProfileError(f"Profile {path} failed validation:\n{exc}") from exc

    expected_id = path.stem
    if profile.id != expected_id:
        raise ProfileError(
            f"Profile id '{profile.id}' does not match filename '{expected_id}' "
            f"({path}). Rename the file or fix the id so lookups are unambiguous."
        )
    return profile


def load_profile(profile_id: str, profile_dir: Path | str | None = None) -> Profile:
    """Load a profile by id (the YAML filename without extension)."""
    path = _profile_dir(profile_dir) / f"{profile_id}.yaml"
    if not path.exists():
        available = ", ".join(list_profiles(profile_dir)) or "(none)"
        raise ProfileError(
            f"No profile '{profile_id}'. Available: {available}"
        )
    return load_profile_file(path)


def list_profiles(profile_dir: Path | str | None = None) -> list[str]:
    """List available profile ids (filenames, not yet validated)."""
    directory = _profile_dir(profile_dir)
    if not directory.is_dir():
        return []
    return sorted(p.stem for p in directory.glob("*.yaml"))

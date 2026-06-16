# SPDX-License-Identifier: GPL-2.0-or-later
"""Load + validate lab (topology) specs from labs/<id>.yaml — mirrors the profile
loader. Labs are centralized in this repo, just like profiles."""
from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from ..profiles.loader import _REPO_ROOT, load_profile
from .models import Lab, LabError

DEFAULT_LAB_DIR = _REPO_ROOT / "labs"


def _lab_dir(lab_dir: Path | str | None) -> Path:
    return Path(lab_dir) if lab_dir else DEFAULT_LAB_DIR


def load_lab_file(path: Path | str, *, validate_profiles: bool = True) -> Lab:
    """Load + validate one lab YAML. If validate_profiles, also confirm every
    node's profile id resolves (fail fast with a clear error, not at launch)."""
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text())
    except FileNotFoundError as exc:
        raise LabError(f"Lab file not found: {path}") from exc
    except yaml.YAMLError as exc:
        raise LabError(f"Lab {path} is not valid YAML: {exc}") from exc

    if not isinstance(raw, dict):
        raise LabError(f"Lab {path} must be a YAML mapping")

    try:
        lab = Lab.model_validate(raw)
    except ValidationError as exc:
        raise LabError(f"Lab {path} failed validation:\n{exc}") from exc

    if lab.id != path.stem:
        raise LabError(
            f"Lab id '{lab.id}' does not match filename '{path.stem}' ({path})."
        )

    if validate_profiles:
        for node in lab.nodes:
            try:
                load_profile(node.profile)
            except Exception as exc:
                raise LabError(
                    f"Lab '{lab.id}' node '{node.name}' references profile "
                    f"'{node.profile}' which does not load: {exc}"
                ) from exc
    return lab


def load_lab(lab_id: str, lab_dir: Path | str | None = None) -> Lab:
    path = _lab_dir(lab_dir) / f"{lab_id}.yaml"
    if not path.exists():
        available = ", ".join(list_labs(lab_dir)) or "(none)"
        raise LabError(f"No lab '{lab_id}'. Available: {available}")
    return load_lab_file(path)


def list_labs(lab_dir: Path | str | None = None) -> list[str]:
    directory = _lab_dir(lab_dir)
    if not directory.is_dir():
        return []
    return sorted(p.stem for p in directory.glob("*.yaml"))

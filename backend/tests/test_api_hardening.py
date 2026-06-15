# SPDX-License-Identifier: GPL-2.0-or-later
"""Upload-quota + admin-gate logic (module-level helpers; no live server)."""

import importlib
import types

import pytest

from holobench.auth import User

A = importlib.import_module("holobench.api.app")  # submodule shadowed by the app instance


def test_dir_bytes_and_upload_budget(tmp_path, monkeypatch):
    share = tmp_path / "share"; frames = tmp_path / "frames"
    share.mkdir(); frames.mkdir()
    (share / "a.bin").write_bytes(b"x" * 1000)
    (frames / "f.raw").write_bytes(b"y" * 2000)
    sess = types.SimpleNamespace(share_dir=share, camera_frames_dir=frames)

    assert A._dir_bytes(share) == 1000
    assert A._dir_bytes(None) == 0

    monkeypatch.setattr(A, "_UPLOAD_QUOTA_MB", 0)
    assert A._upload_budget(sess) is None                       # unlimited

    monkeypatch.setattr(A, "_UPLOAD_QUOTA_MB", 1)               # 1 MiB quota
    assert A._upload_budget(sess) == 1024 * 1024 - 3000         # used 3000 B
    (share / "big.bin").write_bytes(b"z" * (1024 * 1024))
    assert A._upload_budget(sess) == 0                          # exhausted


def test_require_admin():
    def req(role):
        return types.SimpleNamespace(state=types.SimpleNamespace(user=User("u", role)))
    assert A._require_admin(req("admin")).is_admin
    with pytest.raises(Exception):
        A._require_admin(req("user"))

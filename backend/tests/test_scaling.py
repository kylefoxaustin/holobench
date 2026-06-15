# SPDX-License-Identifier: GPL-2.0-or-later
"""Scaling knobs: launch admission control + lazy serial tap (logic only)."""

import asyncio

from holobench.profiles import load_profile
from holobench.session.manager import Session, SessionManager


def test_admission_sem_from_env(monkeypatch):
    async def mk():
        return SessionManager()
    monkeypatch.delenv("HOLOBENCH_MAX_CONCURRENT_LAUNCHES", raising=False)
    monkeypatch.delenv("HOLOBENCH_LAUNCH_STAGGER_S", raising=False)
    assert asyncio.run(mk())._launch_sem is None              # default: unlimited
    monkeypatch.setenv("HOLOBENCH_MAX_CONCURRENT_LAUNCHES", "3")
    monkeypatch.setenv("HOLOBENCH_LAUNCH_STAGGER_S", "2.5")
    m = asyncio.run(mk())
    assert m._launch_sem is not None and m._launch_stagger == 2.5


def test_lazy_serial_flag(tmp_path, monkeypatch):
    monkeypatch.delenv("HOLOBENCH_LAZY_SERIAL", raising=False)
    assert Session(load_profile("imx91-evk"), base_dir=tmp_path)._lazy_serial is False
    monkeypatch.setenv("HOLOBENCH_LAZY_SERIAL", "1")
    assert Session(load_profile("imx91-evk"), base_dir=tmp_path)._lazy_serial is True


def test_resolve_chardev(tmp_path):
    s = Session(load_profile("imx91-evk"), base_dir=tmp_path)
    assert s._resolve_chardev(None) == s.profile.default_serial.chardev
    assert s._resolve_chardev("console0") == "console0"


def test_release_tap_noop_when_untapped(tmp_path):
    s = Session(load_profile("imx91-evk"), base_dir=tmp_path)
    asyncio.run(s.release_tap("console0"))   # no consumer/tap -> no-op, no error
    assert "console0" not in s._tap_refs

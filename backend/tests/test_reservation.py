# SPDX-License-Identifier: GPL-2.0-or-later
"""Reservation logic incl. infinite (no-expiry) reservations."""

from pathlib import Path

from holobench.profiles import load_profile
from holobench.session.manager import Session


def _sess(tmp_path, minutes):
    return Session(load_profile("imx91-evk"), base_dir=tmp_path, minutes=minutes)


def test_finite_reservation(tmp_path):
    s = _sess(tmp_path, 60)
    assert not s.infinite
    assert s.expires_at is not None
    assert 0 < s.remaining_seconds <= 60 * 60
    assert s.expired is False


def test_infinite_reservation(tmp_path):
    s = _sess(tmp_path, 0)                      # <=0 -> infinite
    assert s.infinite
    assert s.expires_at is None
    assert s.remaining_seconds is None
    assert s.expired is False                   # never expires
    assert s.extend(30) is None                 # stays infinite (profile permitting)


def test_profile_default_used_when_minutes_none(tmp_path):
    s = _sess(tmp_path, None)
    p = load_profile("imx91-evk")
    if p.reservation.default_minutes <= 0:
        assert s.infinite
    else:
        assert not s.infinite

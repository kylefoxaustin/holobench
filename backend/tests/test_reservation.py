# SPDX-License-Identifier: GPL-2.0-or-later
"""Reservation logic. Timers were REMOVED (commit 7cb09c4, "no timers on the
reservation system" per Kyle): EVERY session is now infinite (no expiry),
regardless of the requested `minutes` or the profile default. `minutes` is kept
for API back-compat but no longer creates a finite slot."""

from holobench.profiles import load_profile
from holobench.session.manager import Session


def _sess(tmp_path, minutes):
    return Session(load_profile("imx91-evk"), base_dir=tmp_path, minutes=minutes)


def test_every_session_is_infinite(tmp_path):
    # A finite request (60), an explicit infinite (0), and the default (None) all
    # yield a no-expiry reservation now that timers are gone.
    for minutes in (60, 0, None):
        s = _sess(tmp_path, minutes)
        assert s.infinite, f"minutes={minutes} should be infinite (timers removed)"
        assert s.expires_at is None
        assert s.remaining_seconds is None
        assert s.expired is False            # never expires


def test_extend_is_noop_on_infinite(tmp_path):
    s = _sess(tmp_path, 0)
    assert s.extend(30) is None              # stays infinite
    assert s.infinite

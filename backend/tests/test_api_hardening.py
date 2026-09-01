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


# ═══════════════════════════════════════════════════════════════════════════════════════
# "ATTACHABLE" MUST MEAN THE PANEL DTB IS THERE, NOT THAT THE PROFILE NAMED ONE.
#
# ⚠️ Found 2026-09-01 while 95emulator was asking which profile to target for the
# camera→LCD path. The API reported attachable = bool(profile.display.attach_dtb) — a
# check that a STRING WAS SET. attach_dtb is deliberately NOT in setup.required_artifacts
# (it is not boot-critical; a panel-less board boots fine and faithfully), so nothing
# anywhere demanded the file exist. A profile naming a dtb the operator had not
# provisioned still offered a confident "Attach LCD" that reboots the board into failure.
#
# ⭐ AND THE PART THAT MAKES IT WORSE THAN A MISSING FILE: the UI already explains a dark
# panel as "Bare EVK — no panel attached … faithful to real hardware. correct, not a bug."
# That sentence is TRUE for a board with no panel and FALSE for a board whose panel dtb
# was never generated — and without this distinction both render identically. A setup gap
# would wear the costume of a hardware fact, which is the most expensive kind of quiet.
# ═══════════════════════════════════════════════════════════════════════════════════════

def test_attachable_is_measured_not_declared():
    from holobench.api.app import _attach_dtb_path
    from holobench.profiles.loader import load_profile

    # a board whose panel dtb IS provisioned
    p = load_profile("imx95-evk-sd")
    assert p.display.attach_dtb, "this profile is supposed to declare a panel"
    resolved = _attach_dtb_path(p)
    if resolved is None:
        pytest.skip("panel dtb not provisioned on this box — cannot test the positive path")
    assert resolved.is_file()

    # ⭐ THE PLANT: declared, not provisioned. Must degrade, not offer a broken button.
    p.display.attach_dtb = "imx95-19x19-evk-lcd-cam.dtb"   # real name, deliberately absent
    assert _attach_dtb_path(p) is None, (
        "a declared-but-missing panel dtb must not resolve — otherwise the UI offers an "
        "Attach LCD that reboots into failure")


def test_no_panel_and_unprovisioned_panel_are_different_states():
    """A board with no panel is a HARDWARE FACT. A board whose panel dtb was never
    generated is a SETUP GAP. They must not produce the same payload, or the gap inherits
    the fact's explanation ('correct, not a bug') and stops being findable."""
    from holobench.api.app import _attach_dtb_path
    from holobench.profiles.loader import load_profile

    bare = load_profile("imx95-evk")                # declares no panel at all
    assert bare.display.attach_dtb is None
    assert _attach_dtb_path(bare) is None

    gap = load_profile("imx95-evk-sd")
    gap.display.attach_dtb = "definitely-not-generated.dtb"
    assert _attach_dtb_path(gap) is None            # both unattachable…

    # …but only one of them DECLARED a panel, and that is the bit the UI needs to tell
    # "this board has none" from "yours is missing".
    assert bool(bare.display.attach_dtb) is False
    assert bool(gap.display.attach_dtb) is True


# ═══════════════════════════════════════════════════════════════════════════════════════
# QEMU'S WARNINGS MUST REACH THE OPERATOR, NOT JUST THE DISK.
#
# holobench merges QEMU's stderr into qemu.log (stderr=STDOUT at launch), so unlike a
# harness that redirects to /dev/null we never lost these — 95emulator found exactly that
# hole in their own run.sh on 2026-09-01. But we had the OTHER half of the same defect:
# captured, and exposed nowhere. A warning in a file with no endpoint is not meaningfully
# louder than one that was thrown away.
#
# ⭐ THE CASE: ask the modelled ISI for host frames at the wrong geometry and it falls back
# to a synthetic gradient. Capture stays live, the panel shows a plausible moving image,
# and every downstream check passes. One warning line is all that separates "your camera
# works" from "you are looking at a test pattern".
#
# ⚠️ PROVENANCE OF THE SAMPLE BELOW, stated because it is weaker than I would like: the
# fallback line is quoted from 95emulator's message. It is NOT in their working tree and
# NOT at tag imx95-v2.5.0 — that fix is newer than the tag and lives in a worktree this box
# cannot see, so I could not check it against the emitter. That is precisely why the
# detector matches the GENERIC `warning:` shape rather than hardcoding their string: a
# pattern pinned to text I cannot verify would rot the moment their wording changed.
# ═══════════════════════════════════════════════════════════════════════════════════════

def _fake_session_with_log(tmp_path, text: str):
    class _S:
        pass
    s = _S()
    p = tmp_path / "qemu.log"
    p.write_text(text)
    s.log_path = p
    return s


def test_qemu_warnings_are_extracted_from_the_log(tmp_path):
    from holobench.api.app import _qemu_warnings

    log = (
        "char device redirected to /dev/pts/7 (label console)\n"
        # quoted from 95emulator, 2026-09-01 — see provenance note above
        "qemu-system-aarch64: warning: imx95.isi: host frame source '/tmp/wrong_size.raw' "
        "unusable (need 1843200 bytes per frame, got 2000) - FALLING BACK TO THE SYNTHETIC "
        "GRADIENT.\n"
        "[    0.000000] Booting Linux on physical CPU 0x0000000000\n"
    )
    got = _qemu_warnings(_fake_session_with_log(tmp_path, log))
    assert len(got) == 1, f"expected exactly the warning line, got {got}"
    assert "SYNTHETIC GRADIENT" in got[0]


def test_ordinary_boot_output_is_not_reported_as_a_warning(tmp_path):
    """⭐ THE NEGATIVE HALF, which is what stops this becoming noise. A pane that cries
    wolf on every boot is one an operator learns to ignore, and then the real fallback
    warning arrives into a habit of not looking — the same end state as /dev/null, reached
    by a different road."""
    from holobench.api.app import _qemu_warnings

    log = (
        "[    0.123456] imx95-isi 4ad00000.isi: Adding to iommu group 3\n"
        "[    0.234567] ov5640 1-003c: probe done\n"
        "root@imx95evk:~# dmesg | grep -i error\n"          # the WORD error, not a QEMU line
        "Welcome to Linux\n"
    )
    assert _qemu_warnings(_fake_session_with_log(tmp_path, log)) == []


def test_qemu_warnings_dedupe_and_stay_bounded(tmp_path):
    """A device that warns once per frame would otherwise flood the payload on every poll."""
    from holobench.api.app import _qemu_warnings

    log = "qemu-system-aarch64: warning: imx95.isi: fallback\n" * 500
    got = _qemu_warnings(_fake_session_with_log(tmp_path, log))
    assert got == ["qemu-system-aarch64: warning: imx95.isi: fallback"]


def test_missing_or_unreadable_log_is_empty_not_an_exception(tmp_path):
    """The session view is built on every poll; a missing log must not 500 the API."""
    from holobench.api.app import _qemu_warnings

    class _NoLog:
        pass
    assert _qemu_warnings(_NoLog()) == []

    class _Gone:
        log_path = tmp_path / "does-not-exist.log"
    assert _qemu_warnings(_Gone()) == []

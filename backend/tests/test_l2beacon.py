# SPDX-License-Identifier: GPL-2.0-or-later
"""Tests for tools/l2beacon.py — the REAL board's half of the emulated-meets-real lab.

WHY THIS FILE IS NOT OPTIONAL, AND WHY IT TESTS THE REJECTIONS HARDEST

`l2beacon` runs on physical silicon at the far end of a physical cable, and its
console is the lab's LOAD-BEARING ORACLE. The emulated i.MX 95 sits on a macvtap,
where guest and host frames can be locally switched — so the QEMU side's own PASS
is compatible with nothing ever leaving the NIC. The only evidence that a frame
crossed a cable is this program saying it received one.

An oracle in that position has exactly one dangerous failure mode, and it is not
"it missed a frame". It is **accepting something the rest of the segment would
reject**. 95emulator's own source says it plainly (enet-lab3.c:528):

    "A RECEIVER THAT IS MORE PERMISSIVE THAN THE SEGMENT COUNTS PEERS THAT
     EVERYONE ELSE IS REJECTING — and then it is OUR green that is the lie,
     because ours is the only one that came back."

So most of what follows is not "does a good frame pass". It is **does a bad frame
fail** — one mutation per structural gate, because a gate with no failing test is
a gate nobody has shown to work. Every `assert_rejects` here is a mutation the
lab's proof script would have scored as a PASS in its first version.
"""
from __future__ import annotations

import importlib.util
import re
import subprocess
import struct
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
BEACON_PY = REPO / "tools" / "l2beacon.py"
# The emulator repo is the SOURCE OF TRUTH for the wire contract (CLAUDE.md §7).
# holobench transcribes; it never edits. This path is read-only and its absence
# skips the drift test rather than failing it — a dev box without the sibling
# checkout is not a broken repo.
ENET_LAB3_C = Path.home() / "Documents/GitHub/95emulator/tests/enet-lab3/enet-lab3.c"

# ⚠️ SKIP ONLY WHEN THE CHECKOUT IS ABSENT — NOT WHEN THE FILE MOVED.
# 95emulator switched to an upstream branch (imx95-v2-clean) on 2026-08-25 and
# enet-lab3.c vanished from the worktree. These pins SKIPPED. Six of them, silently.
#   ⭐ A DRIFT CHECK THAT GOES QUIET ON DRIFT IS NOT A DRIFT CHECK. That is exactly
#   93emulator's quiet-vs-red distinction, in my own suite, one day after banking it.
# The original call was right — a dev box without the sibling checkout is not a
# broken repo. But "the repo is not here" and "the repo is here and the file is
# gone" are DIFFERENT FACTS, and only the first is a reason to say nothing.
ENET_LAB3_REPO = Path.home() / "Documents/GitHub/95emulator"
# ⭐ PIN THE COMMIT, NOT THE CHECKOUT — holobench's own rule, which these tests
# were violating. They read the WORKTREE, so when 95emulator switched to their
# upstream branch (imx95-v2-clean) on 2026-08-25 the file vanished and six pins
# went SILENT. A worktree is whatever someone last checked out; a commit is a
# fact. We verified the contract at d10d314a, so that is what we compare against —
# and a branch switch can no longer make a drift check disappear.
PINNED_COMMIT = "d10d314a4b354f512dcc74325cd08b16ec65b92c"
PINNED_PATH = "tests/enet-lab3/enet-lab3.c"


def _emulator_source() -> str:
    """The pinned contract source, read from the COMMIT. '' when unavailable."""
    if not ENET_LAB3_REPO.is_dir():
        return ""
    try:
        out = subprocess.run(
            ["git", "-C", str(ENET_LAB3_REPO), "show",
             f"{PINNED_COMMIT}:{PINNED_PATH}"],
            capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout if out.returncode == 0 else ""


def _require_emulator_source() -> str:
    """Skip only when the checkout is ABSENT; fail loudly when the pinned commit
    is present-but-unreachable, because that is drift and not absence."""
    if not ENET_LAB3_REPO.is_dir():
        pytest.skip("95emulator checkout not present on this box")
    src = _emulator_source()
    if not src:
        pytest.fail(
            f"{ENET_LAB3_REPO} exists but {PINNED_PATH} is not readable at pinned "
            f"commit {PINNED_COMMIT[:12]} — the commit we VERIFIED the wire contract "
            f"against is gone (gc'd? re-cloned?). This is drift, not absence, and it "
            f"must not pass as a skip.")
    return src



def _load():
    spec = importlib.util.spec_from_file_location("l2beacon", BEACON_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


lb = _load()

FRDM_MAC = bytes.fromhex("00049f0afc6d")   # the real i.MX 95 FRDM, NXP OUI
MY_ET = 0x88B7                              # the emulated i.MX 95 (us, when receiving)
FRDM_ET = 0x88B9                            # the real FRDM
ORIN_ET = 0x88BA                            # the real Jetson AGX Orin
WANTED = {FRDM_ET, ORIN_ET}


def good(et=FRDM_ET, seq=1, incarn=0xDEADBEEF) -> bytearray:
    return bytearray(lb.build_frame(FRDM_MAC, et, seq, incarn))


def assert_rejects(frame, why_contains: str):
    """A mutated frame must come back CORRUPT — and for the stated reason."""
    verdict, et, seq, incarn, why = lb.classify(bytes(frame), MY_ET, WANTED)
    assert verdict == lb.V_CORRUPT, (
        "a frame mutated to break %r was ACCEPTED as %s — this oracle is more "
        "permissive than the segment it reports on" % (why_contains, verdict))
    assert why_contains in why, "rejected, but for the wrong reason: %r" % why


# ── the frame we build must be exactly what the segment expects ──────────────

def test_frame_is_exactly_64_bytes():
    # Not ">= 64". enet-lab3.c:541 is `n != FRAME_LEN`, and rt1180 spent 90
    # minutes shipping a 1000-byte frame with a valid 64-byte prefix that every
    # enforcing node on the segment threw away.
    assert len(lb.build_frame(FRDM_MAC, FRDM_ET, 1, 0xABCD1234)) == 64


def test_frame_layout_is_byte_exact():
    f = lb.build_frame(FRDM_MAC, FRDM_ET, 7, 0xDEADBEEF)
    assert f[0:6] == b"\xff" * 6                                  # broadcast dst
    assert f[6:12] == FRDM_MAC
    assert struct.unpack_from("!H", f, 12)[0] == FRDM_ET
    assert struct.unpack_from("!I", f, lb.MAGIC_OFF)[0] == lb.BEACON_MAGIC
    assert struct.unpack_from("!H", f, lb.SELF_ET_OFF)[0] == FRDM_ET
    assert struct.unpack_from("!I", f, lb.SEQ_OFF)[0] == 7
    assert struct.unpack_from("!I", f, lb.INCARN_OFF)[0] == 0xDEADBEEF
    assert set(f[lb.FILL_OFF:64]) == {lb.FILL_BYTE}


def test_self_ethertype_echo_matches_header():
    """The body must agree with the header, or the frame accuses itself."""
    f = lb.build_frame(FRDM_MAC, ORIN_ET, 1, 0x11223344)
    assert struct.unpack_from("!H", f, 12)[0] == struct.unpack_from("!H", f, lb.SELF_ET_OFF)[0]


def test_incarnation_is_never_zero_or_the_legacy_sentinel():
    """0 is indistinguishable from an uninitialised field; 0x5A5A5A5A is the v1
    sentinel that costs a required-peer gate its green (enet-lab3.c:594)."""
    vals = {lb._fresh_incarnation() for _ in range(20000)}
    assert 0 not in vals
    assert lb.INCARN_LEGACY not in vals
    assert len(vals) > 19000, "nonce is not actually random"


# ── the oracle accepts what it should ───────────────────────────────────────

def test_good_frame_is_a_sighting():
    verdict, et, seq, incarn, why = lb.classify(bytes(good()), MY_ET, WANTED)
    assert verdict == lb.V_OK
    assert et == FRDM_ET and seq == 1 and incarn == 0xDEADBEEF


# ── the oracle rejects what it must: one mutation per gate ──────────────────

def test_rejects_short_frame():
    assert_rejects(good()[:60], "SHORT")


def test_rejects_long_frame_with_a_valid_64_byte_prefix():
    """rt1180's actual bug. A valid prefix is not a valid frame."""
    assert_rejects(good() + bytearray(936), "SHORT")


def test_rejects_bad_magic():
    f = good()
    struct.pack_into("!I", f, lb.MAGIC_OFF, 0xDEADBEEF)
    assert_rejects(f, "MAGIC")


def test_rejects_frame_that_contradicts_itself():
    """Header says 0x88B9, body says 0x88BA — the signature of a stale or
    clobbered buffer, which is exactly what a DMA writeback bug produces."""
    f = good()
    struct.pack_into("!H", f, lb.SELF_ET_OFF, ORIN_ET)
    assert_rejects(f, "contradicts itself")


def test_rejects_wrong_fill_pattern():
    f = good()
    f[40] = 0x00
    assert_rejects(f, "PATTERN")


def test_rejects_the_staging_probe_frame():
    """⭐ THE REGRESSION THAT MOTIVATED THIS WHOLE FILE.

    /tmp/l2probe.py — the probe the lab was staged with, and which genuinely
    proved the fabric — builds `dst + src + ethertype + body.ljust(46, 0x00)`.
    That is 60 bytes with no magic, no self-echo and a 0x00 fill. If anyone
    reuses it as the board-side node, the segment goes red and the emulator
    gets the blame. Pinned here so that can never happen silently.
    """
    stale = bytearray(b"\xff" * 6 + FRDM_MAC + struct.pack("!H", FRDM_ET)
                      + b"REAL-95-TO-SKIPPY".ljust(46, b"\x00"))
    assert len(stale) == 60
    assert_rejects(stale, "SHORT")


# ── ignoring is not condemning, and self is not a peer ──────────────────────

def test_own_ethertype_is_self_ignored_not_counted():
    """rt1180's monitor once matched its own banner and shouted PASS twelve
    times at an empty wire."""
    verdict, _, _, _, _ = lb.classify(bytes(good(et=MY_ET)), MY_ET, WANTED)
    assert verdict == lb.V_SELF


def test_stranger_is_ignored_not_condemned():
    """Traffic we did not ask for is not a fault. An out-of-block ethertype and
    an unrequested in-block one are both FOREIGN, never CORRUPT."""
    for et in (0x0800, 0x88C1, 0x88B5):     # IP, out-of-block, unrequested peer
        verdict, _, _, _, _ = lb.classify(bytes(good(et=et)), MY_ET, WANTED)
        assert verdict == lb.V_FOREIGN, "0x%04x should be FOREIGN" % et


def test_out_of_block_ethertype_never_reaches_the_structural_gates():
    """95emulator's CATCH 1: 0x88C1/0x88C2 (the ethertypes the fabric proof
    actually used) are outside 0x88B5..0x88BF, so a gate on them would be
    structurally unsatisfiable — and `slot = et - BEACON_ET_LO` would index out
    of bounds as well. A malformed out-of-block frame must still be FOREIGN,
    not CORRUPT: we do not get to condemn traffic we never asked for."""
    junk = bytearray(b"\xff" * 6 + FRDM_MAC + struct.pack("!H", 0x88C1) + b"\x00" * 10)
    verdict, _, _, _, _ = lb.classify(bytes(junk), MY_ET, WANTED)
    assert verdict == lb.V_FOREIGN


# ── freshness: legacy is counted but cannot satisfy a gate ──────────────────

def test_legacy_body_is_counted_but_is_not_a_gate_satisfying_sighting():
    """enet-lab3.c:594 — 'freshness UNVERIFIABLE ... segment stays red'. This is
    the single most likely way the lab goes red for a reason that has nothing to
    do with the wire, so it gets its own test."""
    verdict, et, seq, incarn, why = lb.classify(
        bytes(good(incarn=lb.INCARN_LEGACY)), MY_ET, WANTED)
    assert verdict == lb.V_LEGACY
    assert verdict != lb.V_OK, "a legacy peer must not satisfy a required-peer gate"
    assert verdict != lb.V_CORRUPT, "a legacy peer must not be condemned either"


# ── the contract must not drift out from under us ───────────────────────────

def test_transcription_matches_the_emulator_source():
    """⭐ THE PIN. The emulator repo owns this contract; we only transcribe it.

    If 95emulator changes a byte offset, this test fails HERE — loudly, in
    holobench's own suite — instead of the lab going mysteriously red on a wire
    at 4am and the model getting blamed for it. A path is not an artifact: pin
    the value, and refuse to proceed on drift.
    """
    _SRC = _require_emulator_source()
    src = _SRC

    def cdef(name):
        m = re.search(r"^#define\s+%s\s+(0x[0-9A-Fa-f]+u?|\d+)" % name, src, re.M)
        assert m, "constant %s vanished from enet-lab3.c — the contract moved" % name
        return int(m.group(1).rstrip("uU"), 0)

    for name in ("FRAME_LEN", "BEACON_MAGIC", "MAGIC_OFF", "SELF_ET_OFF", "SEQ_OFF",
                 "INCARN_OFF", "FILL_OFF", "FILL_BYTE", "INCARN_LEGACY",
                 "BEACON_ET_LO", "BEACON_ET_HI"):
        assert cdef(name) == getattr(lb, name), (
            "%s DRIFTED: enet-lab3.c says %#x, l2beacon.py says %#x. The wire "
            "contract moved and holobench's transcription did not follow."
            % (name, cdef(name), getattr(lb, name)))


def test_the_lab_ethertypes_are_inside_the_block():
    """The FRDM and Orin ethertypes must be ones an enet-lab3 node can actually
    see. This is 95emulator's CATCH 1 as an executable assertion."""
    _SRC = _require_emulator_source()
    for name, et in (("real FRDM", FRDM_ET), ("real Orin", ORIN_ET),
                     ("emulated i.MX 95", MY_ET)):
        assert lb.is_beacon_et(et), (
            "%s uses 0x%04x, outside 0x%04x..0x%04x — every enet-lab3 node would "
            "silently ignore it" % (name, et, lb.BEACON_ET_LO, lb.BEACON_ET_HI))
    assert len({MY_ET, FRDM_ET, ORIN_ET}) == 3, "nodes must not share an ethertype"


# ── the fifth rung: does the NEGATIVE CONTROL itself detect a broken oracle? ──
#
# qualcomm's contribution (2026-08-25), from four instances in a completely
# different domain — a builder carrying a SyntaxError read as success, a self-test
# passing against a regex that could never match, a checker reporting "0 ungated,
# 0 gated" because its path resolver was wrong:
#
#     A CHECK THAT DID NOT RUN LOOKS EXACTLY LIKE A CHECK THAT PASSED.
#
# Their point lands above this lab's whole ladder: quiet < red < red-that-SHOWS <
# red-that-NAMES all assume THE CHECK RAN. The rung above is a check with a
# negative control — plant the fault and watch it fire — because that is the only
# rung that survives the harness itself being broken.
#
# ⭐ APPLIED HERE IT IS UNCOMFORTABLE. prove-oracle-bites.sh proves the BOARD's
# oracle can refuse. Nothing has ever proved that the CONTROL can detect a board
# whose oracle CANNOT refuse. Its four phases would report "controls_held=4/4" if
# they were measuring a receiver that accepts everything — provided that receiver
# also happened to stay quiet. So: cripple the oracle deliberately and assert the
# control notices.

def _crippled_classify(buf, my_et, wanted):
    """An oracle with its body gates removed — accepts any in-block ethertype.

    This is the failure the negative control exists to catch, and until it was
    planted, nobody had seen the control react to it.
    """
    if len(buf) < 14:
        return (lb.V_FOREIGN, None, None, None, "runt")
    et = struct.unpack("!H", buf[12:14])[0]
    if et == my_et:
        return (lb.V_SELF, et, None, None, None)
    if not lb.is_beacon_et(et) or et not in wanted:
        return (lb.V_FOREIGN, et, None, None, None)
    return (lb.V_OK, et, 1, 0xABCD1234, None)      # <- accepts ANY body


def test_the_negative_control_detects_an_oracle_that_cannot_refuse():
    """Plant the fault the control exists to catch, and confirm it fires.

    The control's phases are: ARMED must be accepted; WRONG-ET, CORRUPT and
    LEGACY must be REFUSED. Against a healthy oracle, exactly one is accepted.
    Against a crippled one, the body-based controls stop refusing — and that
    difference is what makes `controls_held=4/4` a measurement rather than a
    formality.
    """
    phases = [
        ("ARMED",    FRDM_ET, {},                                    True),
        ("WRONG-ET", 0x88BE,  {},                                    False),
        ("CORRUPT",  FRDM_ET, {"magic": True},                       False),
        ("LEGACY",   FRDM_ET, {"incarn": lb.INCARN_LEGACY},          False),
    ]

    def frame(et, mut):
        f = bytearray(lb.build_frame(FRDM_MAC, et, 7,
                                     mut.get("incarn", 0xA5A5A5A5)))
        if mut.get("magic"):
            struct.pack_into("!I", f, lb.MAGIC_OFF, 0xDEADBEEF)
        return bytes(f)

    healthy = {n: lb.classify(frame(et, m), MY_ET, {FRDM_ET}) [0] == lb.V_OK
               for n, et, m, _ in phases}
    crippled = {n: _crippled_classify(frame(et, m), MY_ET, {FRDM_ET})[0] == lb.V_OK
                for n, et, m, _ in phases}

    # A healthy oracle behaves exactly as the control's design requires.
    for name, _, _, expect_accept in phases:
        assert healthy[name] == expect_accept, (
            f"healthy oracle got {name} wrong — the control's premise is broken")

    # ⭐ THE ACTUAL ASSERTION: the crippled oracle must differ, on the body gates.
    broke = [n for n, _, _, expect in phases if crippled[n] != expect]
    assert broke, (
        "PLANTED A CRIPPLED ORACLE AND THE CONTROL SAW NO DIFFERENCE. "
        "controls_held=4/4 would then be a formality, not a measurement — the "
        "control cannot distinguish a receiver that refuses from one that accepts "
        "everything, and every green it has ever produced is void.")
    assert "CORRUPT" in broke and "LEGACY" in broke, (
        f"the control noticed {broke}, but the BODY gates (CORRUPT/LEGACY) are the "
        f"ones that separate 'I saw a frame' from 'I saw an ethertype' — if those "
        f"do not flip, the control is not testing what it claims to test")
    # WRONG-ET is ethertype-level, so a body-crippled oracle still refuses it.
    # Stated so the asymmetry is deliberate rather than an unnoticed gap.
    assert "WRONG-ET" not in broke

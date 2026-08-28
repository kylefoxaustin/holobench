# SPDX-License-Identifier: GPL-2.0-or-later
"""Pin every emitter token holobench's scorers grep for.

⭐ WHY THIS FILE EXISTS — 95emulator found it in their tree and holobench had the
identical hole. When they softened enet-lab3's FAIL message, their own NEG-test
was asserting on the OLD string verbatim:

    grep -aq 'ENET-LAB3 FAIL: deadline, missing peers: 0x88b6'

Change the emitter and that grep stops matching, so the negative test falls
through to its INCONCLUSIVE branch and SILENTLY STOPS TESTING. The suite still
prints green and nothing says the assertion died.

    A CONTROL THAT QUIETLY BECOMES A NO-OP IS WORSE THAN NO CONTROL,
    because the green it produces is indistinguishable from a real one.

holobench's scorers grep SEVEN such tokens across two files and had NOTHING that
would notice if an emitter renamed one. The scorer would not crash — it would
score every leg INCONCLUSIVE, forever, and look like a quiet lab.

⭐ THE GENERAL FORM, which is the fleet's now: WHEN YOU FIX AN EMITTER'S TEXT, THE
  THINGS THAT ASSERT ON THAT TEXT ARE PART OF THE CHANGE. "I changed the emitter"
  and "I changed everything that reads the emitter" are different claims.

And a method note holobench earned the hard way: when asked "who consumes this
string?", it grepped 91/93/mcx/rt1180 and reported a blast radius of one file —
having EXCLUDED 95emulator, the very tree whose file was changing, because it was
looking for OTHER consumers. The emitter's own repo is the most likely consumer of
its own text. Search the tree you are changing FIRST.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
L2BEACON = REPO / "tools" / "l2beacon.py"
SCORE_RS = REPO / "tools" / "score-real-silicon.py"
RUN_LAB3 = REPO / "tools" / "run-enet-lab3.py"
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



def _consts(path: Path) -> dict[str, str]:
    """NAME = "literal" pairs a scorer greps for."""
    return dict(re.findall(r'^([A-Z_]+)\s*=\s*"([^"]+)"', path.read_text(), re.M))


# ── tokens produced by OUR OWN emitter ──────────────────────────────────────

@pytest.mark.parametrize("name", ["BOARD_PASS", "BOARD_UP", "BOARD_CORRUPT"])
def test_board_tokens_are_actually_emitted_by_l2beacon(name):
    """Each L2BEACON token the scorer greps must exist in l2beacon.py.

    These are OURS on both ends, so a rename is entirely within our control —
    which is exactly why it would be easy to do and never notice.
    """
    token = _consts(SCORE_RS)[name]
    assert token in L2BEACON.read_text(), (
        f"score-real-silicon.py greps {token!r} but tools/l2beacon.py no longer "
        f"emits it. The scorer would not crash — it would score every leg "
        f"INCONCLUSIVE forever and look like a quiet lab.")


def test_the_up_token_gates_participation_and_must_keep_its_colon():
    """BOARD_UP is the proof-of-participation gate; its exact form is load-bearing.

    'Every participant must prove it participated' is implemented as a substring
    match on this token. A trailing-colon change would silently disable the one
    check that separates INCONCLUSIVE from FAIL — the distinction four false
    verdicts in this lab came from missing.
    """
    up = _consts(SCORE_RS)["BOARD_UP"]
    assert up.endswith(":"), "BOARD_UP must match the emitted prefix exactly"
    assert f'print("{up}' in L2BEACON.read_text() or up in L2BEACON.read_text()


# ── tokens produced by an emitter WE DO NOT OWN ─────────────────────────────

@pytest.mark.parametrize("src,name", [(SCORE_RS, "GUEST_PASS"),
                                      (SCORE_RS, "GUEST_CORRUPT"),
                                      (RUN_LAB3, "PASS_TOKEN"),
                                      (RUN_LAB3, "CORRUPT_TOKEN")])
def test_guest_tokens_still_exist_in_the_emulator_source(src, name):
    """⭐ THE CROSS-REPO PIN. 95emulator owns these strings; we only grep them.

    If they rename one, this fails HERE, loudly, in holobench's own suite —
    instead of every future run scoring INCONCLUSIVE while the lab looks quiet
    and nobody can say when it stopped asserting.
    """
    _SRC = _require_emulator_source()
    token = _consts(src)[name]
    assert token in _SRC, (
        f"{src.name} greps {token!r} but 95emulator's enet-lab3.c no longer emits "
        f"it. The emitter moved and our scorer did not follow — see this file's "
        f"header for why that fails silently rather than loudly.")


def test_no_scorer_token_is_left_undeclared():
    """Every token constant is covered above. A new one added without a test here
    reintroduces exactly the hole this file closes."""
    covered = {"BOARD_PASS", "BOARD_UP", "BOARD_CORRUPT", "GUEST_PASS", "GUEST_CORRUPT"}
    declared = {k for k, v in _consts(SCORE_RS).items()
                if v.startswith(("ENET-LAB3", "L2BEACON"))}
    assert declared == covered, (
        f"score-real-silicon.py declares token(s) with no pin here: "
        f"{sorted(declared - covered)}. Add them, or the next rename dies quietly.")


def test_guest_corroboration_is_never_reported_as_a_per_leg_count():
    """⭐ A POOLED NUMBER MUST NOT WEAR A PER-LEG LABEL — CHECKED AT *EVERY* SITE.

    The guest runs one enet-lab3 instance per leg onto ONE console, and its PASS
    lines carry no ethertype — so its total cannot be attributed per leg. The
    scorer once printed "saw 0x88b9 (673 PASS lines)" under BOTH legs, inviting a
    reader to sum them (1346) or read each leg as corroborated 673 times.

    ⚠️ THIS TEST WAS ITSELF INERT UNTIL 2026-08-27. It used src.index(...) — the
    FIRST occurrence only — and the first occurrence is the CORRECTED one. So it
    passed forever while any number of bare re-assertions accumulated below it.
    Proven by planting exactly that: a second, unqualified "saw {want} ({count}
    PASS lines)" lower in the same file, and the suite stayed green.

    ⭐ THAT IS qualcomm's PROXIMITY-GUARD INVERSION IN A DIFFERENT MECHANISM. Theirs
    forgave anything within ±400 chars of a retraction — so the file documenting a
    correction was the file most able to absorb its re-assertion. Mine forgave
    everything after the first match. Both guards are strongest exactly where the
    claim is most likely to come back, and neither is revealed by RUNNING it — only
    by planting the superseded form and checking the guard still fires.
    """
    src = SCORE_RS.read_text()

    # ⚠️ SCOPE TO THE CLAIM-UNIT, NOT TO A WINDOW. qualcomm's guard forgave anything
    # within +/-400 chars of a retraction, which made the file DOCUMENTING a
    # correction the file most able to absorb its re-assertion. Widening a window to
    # fix an over-fire would rebuild that hole here. So: extract each notes.append(...)
    # call by PAREN BALANCE — the statement is the unit the claim lives in — and
    # require every one that corroborates to mark its count POOLED.
    calls, k = [], 0
    while True:
        k = src.find("notes.append(", k)
        if k < 0:
            break
        j, depth = k + len("notes.append("), 1
        while j < len(src) and depth:
            depth += (src[j] == "(") - (src[j] == ")")
            j += 1
        calls.append(src[k:j])
        k = j

    corroborating = [c for c in calls if "guest corroborates" in c]
    assert corroborating, "the guest-corroboration note vanished — the claim is unguarded"
    for c in corroborating:
        assert "POOLED" in c or "pooled" in c, (
            "an UNQUALIFIED guest-corroboration note exists:\n"
            f"    {c[:120]}...\n"
            "A per-leg ethertype beside an unqualified total reads as a per-leg count. "
            "EVERY site must mark it POOLED, not just the first — the first is the one "
            "that got corrected.")


def test_no_test_file_lives_outside_the_path_the_gate_actually_runs():
    """⭐ INVERT THE DEFAULT: a test the gate does not collect must be LOUD, not silent.

    qualcomm's finding (2026-08-27): their gate matched each checker's last line
    against an ALLOWLIST OF NOUNS, so 7 of 25 checkers were invisible — including
    the one guarding shipped artifacts, and two live failures in the checker
    reserved for defects that had already shipped. Their generalisation:

        AN ALLOWLIST MAKES SILENCE THE DEFAULT FOR ANYTHING NEW.

    A checker added tomorrow is mute until someone edits a pattern in another file,
    and nothing tells you. They proved it on themselves within the hour: the checker
    they wrote to DETECT inert wiring was itself invisible to the gate, by that
    mechanism, on the day they wrote it.

    My equivalent is narrower but real: the pre-commit hook runs `pytest tests/`
    from backend/, so a test file placed anywhere else is collected by nobody and
    says nothing. This makes that condition fail loudly instead.
    """
    root = Path(__file__).resolve().parents[2]
    collected_dir = (root / "backend" / "tests").resolve()
    strays = []
    for p in root.rglob("test_*.py"):
        if ".venv" in p.parts or ".git" in p.parts:
            continue
        if collected_dir not in p.resolve().parents:
            strays.append(p.relative_to(root))
    assert not strays, (
        f"test file(s) outside the path the gate runs: {strays}. The pre-commit hook "
        f"runs `pytest tests/` from backend/, so these are collected by nobody and "
        f"fail silently forever. Move them under backend/tests/ or widen the hook — "
        f"but do not leave them where the default is silence.")


def test_the_commit_gate_is_actually_wired():
    """⭐ MAKE THE UNARMED STATE ANNOUNCE ITSELF THE FIRST TIME ANYONE LOOKS.

    qualcomm's move (2026-08-27), adopted: a FRESH CLONE has no installed hooks, so
    the repo's stated guarantee — "the suite runs on every commit" — is FALSE until
    someone runs .githooks/install.sh, and nothing says so. No local hook can fix
    that, because on a fresh clone no local hook exists to complain.

    What CAN fix it is the thing a newcomer runs anyway: the suite itself. So this
    fails, loudly, naming the install command. It converts "silently unarmed
    forever" into "unarmed until the first time anyone runs the tests".

    ⚠️ IT IS A PARTIAL CLOSE AND I WILL NOT CLAIM MORE. Nothing forces that first
    run. A clone where nobody runs pytest and nobody commits is still unarmed and
    still silent. Only CI closes it properly, and there is none here.

    ⚠️ NO ESCAPE HATCH, DELIBERATELY. An env-var bypass would reintroduce exactly
    the hole — the degraded state would go quiet again for anyone who set it once.
    The fix is five seconds: bash .githooks/install.sh
    """
    import subprocess
    root = Path(__file__).resolve().parents[2]

    hooks_path = subprocess.run(
        ["git", "-C", str(root), "config", "--get", "core.hooksPath"],
        capture_output=True, text=True).stdout.strip()
    assert hooks_path, (
        "core.hooksPath is UNSET — this repo's commit gate is not wired, so the suite "
        "runs only when someone remembers to type it. Fix: bash .githooks/install.sh")

    installed = (root / hooks_path).resolve()
    for hook in ("pre-commit", "pre-push"):
        p = installed / hook
        assert p.is_file() and os.access(p, os.X_OK), (
            f"{hook} missing or not executable at {installed} — the gate is not armed. "
            f"pre-push also DELEGATES TO THE FLEET PUSH GATE, so a missing one removes "
            f"that too. Fix: bash .githooks/install.sh")


def _scorer_mod():
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "scorer", Path(__file__).resolve().parents[2] / "tools" / "score-real-silicon.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# The beacon's designed HONEST NEGATIVE: frames arrived from a required peer, but never
# all of them in one window. l2beacon.py:418 prints this, and it carries `rx=`.
_NOPASS_LOG = (
    "L2BEACON UP: et=0x88b7\n"
    "L2BEACON STATS: rx_peer=9 rx_self_ignored=0 rx_foreign_ignored=3 corrupt=0 passes=0 tx=40\n"
    "L2BEACON NO-PASS: required peers were never all seen in one window "
    "(0x88b7 rx=9). This is a reportable negative, not an error.\n"
)


def test_rx_from_reads_the_whole_log_not_just_PASS_lines():
    """⚠️ THE DOCSTRING USED TO CLAIM PASS-LINES-ONLY. It scans the whole log, and
    l2beacon emits rx= from TWO sites — the PASS line AND the NO-PASS line. This test
    exists so that claim cannot quietly come back: it pins what the function actually
    measures, which is what the call site's OR-guard depends on."""
    m = _scorer_mod()
    assert m._rx_from(_NOPASS_LOG) == 9, "rx= on a NO-PASS line must still be seen"
    assert _NOPASS_LOG.count(m.BOARD_PASS) == 0, "…and this log has NO PASS lines at all"


def test_or_guard_second_half_is_not_redundant():
    """⭐ qualcomm's test, applied to my shipped scorer: for any guard with an OR,
    disable the OTHER branch and re-plant. If the failure is still caught, the branch you
    disabled may be dead.

    Here it came back the other way — the second half is LOAD-BEARING and the first
    cannot see this case at all. An honest negative (rx=9, passes=0) is a FAIL only
    because of `or board_passes <= 0`. Drop that half and the scorer reports a false PASS
    on a run where the required peers were never all seen.

    What made it look droppable was a WRONG COMMENT, not a wrong test: the old docstring
    implied rx > 0 guaranteed a PASS line. Redundancy hides deadness; an inaccurate
    comment manufactures the APPEARANCE of redundancy over something alive."""
    m = _scorer_mod()
    rx = m._rx_from(_NOPASS_LOG)
    passes = _NOPASS_LOG.count(m.BOARD_PASS)

    assert not (rx <= 0), "first half does NOT fire on the honest negative"
    assert passes <= 0, "second half DOES fire on it"
    assert (rx <= 0 or passes <= 0), "the guard as written catches it"

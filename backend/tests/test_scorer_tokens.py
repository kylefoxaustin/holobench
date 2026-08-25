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

#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Runner + scorer for labs/imx95-real-silicon.yaml — the lab with silicon on the far end.

`holobench lab launch` brings the lab up and holds it. It does not SCORE it, and
the existing scorer (tools/run-enet-lab3.py) cannot: it is hard-wired to
LAB_ID = "mcx-rt1180-95-l2", and it reads every node's output through
`session.console_log()` — which a silicon node does not have, because holobench
never launched it. Without this file the end-to-end run produces a console dump
and no verdict, and somebody reads it by eye. Reading by eye is how "I saw the
token somewhere in the output" becomes a PASS.

────────────────────────────────────────────────────────────────────────────────
⭐ WHERE THE VERDICT COMES FROM, AND WHY IT IS NOT THE GUEST

The emulated node sits on a macvtap. Two endpoints on one lower device can be
LOCALLY SWITCHED, so the guest's own PASS is compatible with no frame ever
reaching the NIC. Its console is useful evidence and it is NOT the assertion.

    THE LOAD-BEARING ASSERTION IS THE REAL BOARD'S OWN LOG, fetched off the board
    over ssh, saying it RECEIVED 0x88B7 — because that board is on the far side of
    a physical cable and local switching cannot reach it.

So this scorer grades each leg on the SILICON side first, and treats the guest's
console as corroboration. A leg where only the guest is happy is not a pass; it is
a leg where the one witness that could have refuted it never spoke.

────────────────────────────────────────────────────────────────────────────────
THE FOUR RULES, inherited and paid for
  1. PREFLIGHT REFUSES A VERDICT. A setup that cannot work must never reach the
     scoring code. (claude-connect's v1 printed "2 passed, 0 failed" while all
     three steps had failed — it tested for the ABSENCE of a failure string, and a
     step that never ran emitted none.)
  2. PASS REQUIRES A POSITIVE INTEGER PARSED FROM THE ARTIFACT THAT CARRIES THE
     ASSERTION. Never `! grep failure`, and never a field that only exists once a
     run has ENDED — a 40/40 success was once scored FAIL because the parser read
     `rx_peer=` (STATS-only) from a log that was still being written.
  3. EVERY PARTICIPANT MUST PROVE IT PARTICIPATED. A peer that never started is
     INCONCLUSIVE, never FAIL. This lab has produced that error four separate
     times; it is the single most durable lesson in it.
  4. INCONCLUSIVE IS ITS OWN VERDICT. A killed or unreachable run is not a caught
     bug (mcxn947).

AND THE EVIDENCE OUTLIVES THE RUN. Teardown deletes session work dirs, taking the
consoles with them. Everything is copied out BEFORE teardown, because a scorer
that keeps only its verdict has destroyed the thing that could overturn it.
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from holobench.labs import load_lab                       # noqa: E402
from holobench.labs.coordinator import LabCoordinator     # noqa: E402
from holobench.session.manager import SessionManager      # noqa: E402

LAB_ID = "imx95-real-silicon"
HOLD_S = 75.0
GUEST_ET = 0x88B7

# The guest speaks enet-lab3's tokens; the real boards speak l2beacon's.
GUEST_PASS = "ENET-LAB3 PASS"
GUEST_CORRUPT = "ENET-LAB3 CORRUPT"
BOARD_PASS = "L2BEACON PASS"
BOARD_UP = "L2BEACON UP:"
BOARD_CORRUPT = "L2BEACON CORRUPT"

EVIDENCE = Path(__file__).resolve().parent.parent / "scratchpad-consoles" / "real-silicon"
L2BEACON_SRC = Path(__file__).resolve().parent / "l2beacon.py"
ENET_LAB3_C = Path.home() / "Documents/GitHub/95emulator/tests/enet-lab3/enet-lab3.c"


def _emitted_strings(py_src: str) -> list[str]:
    """Every string literal that actually reaches a print() call. NOT the whole file.

    ⚠️ WHY THIS IS NOT `tok in source` (fixed 2026-08-27, proven by planting). The old
    check asked whether the token appeared ANYWHERE in tools/l2beacon.py — and that file
    documents its own output format in its module docstring, worked example and all:

        L2BEACON PASS #<n>: saw all required peers (0x88b7 rx=1234)

    So the emitter's PASS line was renamed to "L2BEACON VERIFIED", the docstring was left
    alone, and the guard reported NO PROBLEMS. A guard whose stated job is "refuse a
    verdict rather than produce a quiet one" was satisfied by the DOCUMENTATION of the
    thing it was checking. My own vantage rule, turned on me: a grep hit is not a finding
    until you read what it hit.

    qualcomm, same day: a guard's detection pattern should be tested against a real
    sample of what it detects — a regex is a claim about a string FORMAT, and format
    claims rot exactly like numeric ones.
    """
    import ast
    out: list[str] = []
    try:
        tree = ast.parse(py_src)
    except SyntaxError:
        return out
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id == "print":
            for sub in ast.walk(node):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                    out.append(sub.value)
    return out


def _c_string_literals(c_src: str) -> list[str]:
    """Every double-quoted literal in a C source, with // and /* */ comments stripped
    first — same reason as above: a token surviving only in a comment is not emitted."""
    import re as _re
    no_block = _re.sub(r"/\*.*?\*/", " ", c_src, flags=_re.S)
    no_line = _re.sub(r"//[^\n]*", " ", no_block)
    return _re.findall(r'"((?:[^"\\]|\\.)*)"', no_line)


def _verify_tokens_still_exist() -> list[str]:
    """⭐ FAIL LOUD, NOT QUIET — 93emulator's distinction, applied at RUN time.

    Their token consumer ASSERTS (`die "both nodes must reach PASS"`), so a rename
    makes it go RED. This scorer GRADES, so a rename makes it go QUIET: every leg
    scores INCONCLUSIVE forever and the lab merely looks like nothing happened.
    A suite that goes green is at least suspicious to a careful reader; a lab that
    goes quiet reads as an uneventful night.

    backend/tests/test_scorer_tokens.py pins these at CI time, but that is not
    enough here: its cross-repo checks SKIP when the emulator checkout is absent,
    and the emitter can move between a test run and a lab run. So the scorer
    re-checks its own assumptions at the moment it is about to depend on them, and
    REFUSES A VERDICT rather than producing a quiet one.

    Returns a list of problems (empty = fine).
    """
    problems: list[str] = []
    ours = "\n".join(_emitted_strings(L2BEACON_SRC.read_text())) if L2BEACON_SRC.is_file() else ""
    for name, tok in (("BOARD_PASS", BOARD_PASS), ("BOARD_UP", BOARD_UP),
                      ("BOARD_CORRUPT", BOARD_CORRUPT)):
        if not ours:
            problems.append("tools/l2beacon.py is missing — cannot verify board tokens")
            break
        if tok not in ours:
            problems.append(
                f"{name}={tok!r} is no longer PRINTED by tools/l2beacon.py (it may "
                f"still appear in a docstring or comment there — that does not emit it). This "
                f"scorer would grade every leg INCONCLUSIVE and look like a quiet lab.")
    if ENET_LAB3_C.is_file():
        guest = "\n".join(_c_string_literals(ENET_LAB3_C.read_text()))
        for name, tok in (("GUEST_PASS", GUEST_PASS), ("GUEST_CORRUPT", GUEST_CORRUPT)):
            if tok not in guest:
                problems.append(
                    f"{name}={tok!r} is no longer emitted by 95emulator's enet-lab3.c. "
                    f"The emitter moved and this scorer did not follow.")
    return problems


def _ssh(host: str, cmd: str, timeout: float = 10.0) -> tuple[int, str]:
    """ssh to a real board AS THE INVOKING USER, never as root.

    ⚠️ THE SIXTH SUDO-IDENTITY BUG IN THIS LAB, and it aborted a whole run:
        frdm95: cannot reach root@10.0.1.181 — Host key verification failed
        orin:   cannot reach kyle@10.0.1.124 — Host key verification failed
    The lab is launched under sudo (macvtap needs privilege), so this scorer runs
    as root — and ROOT'S known_hosts has never seen these boards. Kyle's has. The
    boards were reachable the whole time; the identity was wrong.

    ⭐ SUDO CHANGES WHO YOU ARE, AND EVERY USER-SCOPED IDENTITY MOVES WITH IT.
    Previously: root's ssh KEYS, root's ssh CONFIG, root's $HOME, os.getuid()==0
    chowning a device away from QEMU, sudo -n demanding passwordless when already
    root — and now root's KNOWN_HOSTS. Six faces, one cause.

    The preflight caught this and refused a verdict, which is the system working:
    it did not score a lab whose peers it could not reach.
    """
    argv = ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={int(timeout)}", host, cmd]
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user and os.geteuid() == 0:
        argv = ["sudo", "-u", sudo_user, "-H"] + argv
    try:
        p = subprocess.run(
            argv,
            capture_output=True, text=True, timeout=timeout + 5)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 255, "ssh timed out"


def _rx_from(log: str) -> int:
    """Largest rx= anywhere in a board log. ⚠️ NOT "in its PASS lines".

    This docstring used to claim it parsed the PASS line specifically. IT DOES NOT —
    the regex scans the WHOLE log, and l2beacon emits `rx=` from TWO places:
        l2beacon.py:402  L2BEACON PASS #N: ... (0x88b7 rx=9)
        l2beacon.py:418  L2BEACON NO-PASS: ... (0x88b7 rx=9)   <- printed when passes==0
    So a run that received frames from one required peer but never all of them in one
    window — the beacon's designed HONEST NEGATIVE — yields rx > 0 with ZERO passes.

    It does correctly skip the STATS line, but by accident of spelling rather than by
    scoping: STATS says `rx_peer=`, and "rx=" is not a substring of "rx_peer=". A
    rename there would silently change what this function measures.

    ⭐ THE REASON THIS MATTERS IS AT THE CALL SITE, NOT HERE. The old wording implied
    rx > 0 guarantees a PASS line exists, which makes the `or board_passes <= 0` half
    of RULE 2 look like redundant belt-and-braces. It is not redundant; it is the only
    half that catches the honest negative. A comment that makes a live guard look dead
    is a deletion waiting to happen.
    """
    vals = [int(m) for m in re.findall(r"[( ]rx=(\d+)", log)]
    return max(vals) if vals else 0


def _evidence_tail(log: str, n: int = 12) -> list[str]:
    """The raw log beside the verdict — 93emulator's refinement.

    ⭐ THE HONESTY LADDER, built across this thread:
        quiet < red < red-that-SHOWS-the-evidence < red-that-NAMES-the-cause

    A verdict here is computed from COUNTS I derived using TOKENS I CHOSE. If that
    parsing is wrong in any way — a renamed token, a truncated log, a partial ssh
    read — then `rx=0` is wrong, and a FAIL saying "the real board did not receive"
    would confidently accuse THE WIRE when the bug is in my grep. That is 95's
    "red is not automatically honest": a red naming the wrong cause sends the next
    person to the cable while the fault sits in the harness.

    Naming a cause is the top of the ladder but it is also the easiest thing to get
    WRONG, because it requires me to be right about why. Showing the evidence is
    cheaper and more robust: I dump what I actually have, and a reader who sees
    `rx=0` next to visible PASS lines self-corrects without me. So every non-PASS
    verdict carries the raw log, not just my summary of it.
    """
    lines = [l for l in log.strip().splitlines() if l.strip()]
    if not lines:
        return ["      (board log is EMPTY — nothing was captured at all)"]
    head = ["      ── raw board log (the evidence, not my summary of it) ──"]
    if len(lines) > n:
        head.append(f"      … {len(lines) - n} earlier line(s) elided; full copy in scratchpad-consoles/")
    return head + [f"      | {l[:150]}" for l in lines[-n:]]


def score_leg(node, board_log: str, guest_console: str) -> tuple[str, list[str]]:
    """Grade one leg. Returns (PASS|FAIL|INCONCLUSIVE, notes)."""
    notes: list[str] = []

    # RULE 3, first and hardest. If the board's beacon never came up, everything
    # downstream is a statement about THIS BUG, not about the wire.
    if BOARD_UP not in board_log:
        tail = board_log.strip().splitlines()[-1][:140] if board_log.strip() else "(empty log)"
        notes.append(f"the board's beacon NEVER STARTED — {tail}")
        notes.append("nothing was listening, so silence here is not evidence about the wire")
        notes.extend(_evidence_tail(board_log))
        return ("INCONCLUSIVE", notes)

    board_passes = board_log.count(BOARD_PASS)
    board_rx = _rx_from(board_log)
    board_corrupt = board_log.count(BOARD_CORRUPT)
    notes.append(f"board: PASS={board_passes} rx={board_rx} CORRUPT={board_corrupt}")

    # RULE 2: a positive integer from the artifact that carries the assertion.
    #
    # ⚠️ DO NOT "SIMPLIFY" THIS TO ONE TEST. The two halves catch DIFFERENT failures and
    # neither subsumes the other — verified 2026-08-27 by qualcomm's test (disable one
    # branch of an OR and re-plant; if the failure is still caught, the branch you
    # disabled may be dead):
    #     board_rx <= 0      alone  → catches a silent wire, MISSES the honest negative
    #     board_passes <= 0  alone  → catches the honest negative
    # A real L2BEACON NO-PASS log (rx=9, passes=0) scores FAIL with both halves and
    # NOT-CAUGHT — a false PASS — with the second removed. Pinned by
    # test_or_guard_second_half_is_not_redundant.
    if board_rx <= 0 or board_passes <= 0:
        notes.append(f"⭐ the REAL BOARD did not receive 0x{GUEST_ET:04x} — this is a "
                     f"genuine negative and the load-bearing one")
        notes.append("   …IF my parse is right. rx/PASS are counts I derived with tokens "
                     "I chose, so read the log below before believing the wire is at fault:")
        notes.extend(_evidence_tail(board_log))
        return ("FAIL", notes)

    if board_corrupt:
        notes.append(f"⚠️ the board CONDEMNED {board_corrupt} frame(s) — the emulated "
                     f"NIC put something malformed on a real wire. Worth 95emulator's eyes.")

    # The guest's side is corroboration, never the verdict.
    want = f"0x{node.ethertype:04x}"
    guest_saw = GUEST_PASS in guest_console
    if guest_saw:
        # ⚠️ THE GUEST'S PASS COUNT IS POOLED AND CANNOT BE SPLIT PER LEG.
        # The guest runs one enet-lab3 instance per leg onto ONE console, and its
        # PASS lines carry no ethertype:
        #     ENET-LAB3 PASS: t=... peers=1/1 validated=1 beat=1 loss=0 ...
        # So a total is all this log can support. The earlier form printed
        # "saw 0x88b9 (673 PASS lines)" under BOTH legs — a per-leg ethertype
        # welded to a pooled count, which invites a reader to sum them (1346) or
        # to believe each leg was independently corroborated 673 times. Neither is
        # in the log. ⭐ 673 was REAL and TRUE and attached to a claim it does not
        # measure — the instrument-pointed-at-the-wrong-quantity class, which is
        # the one that survives review because nothing looks wrong.
        # (qualcomm found this on the third re-derivation of the same bundle.)
        notes.append(f"guest corroborates: reached PASS on this leg's peer {want}; "
                     f"the guest's {guest_console.count(GUEST_PASS)} PASS lines are a "
                     f"POOLED TOTAL across BOTH legs and cannot be attributed per-leg "
                     f"(its PASS line carries no ethertype). Do not sum across legs.")
    else:
        notes.append(f"⚠️ guest did NOT report seeing {want}. The crossing is proven "
                     f"ONE WAY (board received the guest); the reverse is not shown.")
    if GUEST_CORRUPT in guest_console:
        notes.append(f"⚠️ guest logged {guest_console.count(GUEST_CORRUPT)} CORRUPT — "
                     f"the real board's frames arrived malformed to the model.")

    if not guest_saw:
        # One-way is a real result, but the reader must be able to see WHY the
        # guest was silent rather than take my word that it was.
        notes.extend(_evidence_tail(guest_console, 8))
    return ("PASS" if guest_saw else "INCONCLUSIVE", notes)


class _Tee:
    """Write to the real stream AND a transcript file."""

    def __init__(self, stream, fh):
        self._s, self._f = stream, fh

    def write(self, data):
        self._s.write(data)
        self._f.write(data)
        self._f.flush()
        return len(data)

    def flush(self):
        self._s.flush()
        self._f.flush()

    def isatty(self):
        return self._s.isatty()


def _install_transcript() -> Path:
    """Tee stdout AND stderr to a file under scratchpad-consoles/runs/.

    ⚠️ ADDED 2026-09-02, MONTHS LATE. Kyle asked for exactly this in August — "please have
    the script output to a file that you can read yourself" — and I added it to
    prove-macvtap-guest.sh and prove-oracle-bites.sh and NOT to this one, which is the
    script that produces the actual verdict. A lesson applied to two files out of three is
    not a lesson applied, and the file it was missing from was the one that matters.

    Installed BEFORE the preflight, because an ABORTED run is exactly the one you want a
    record of. Chowned to the invoking user: this runs under sudo, and a transcript only
    root can read is one I cannot read.
    """
    d = Path(__file__).resolve().parents[1] / "scratchpad-consoles" / "runs"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"real-silicon-{datetime.now():%Y%m%d-%H%M%S}.log"
    fh = open(path, "w", buffering=1)
    sys.stdout = _Tee(sys.stdout, fh)
    sys.stderr = _Tee(sys.stderr, fh)
    uid = os.environ.get("SUDO_UID")
    gid = os.environ.get("SUDO_GID")
    if uid and gid:
        try:
            os.chown(path, int(uid), int(gid))
            os.chown(d, int(uid), int(gid))
        except OSError:
            pass
    return path


async def main() -> int:
    _tpath = _install_transcript()
    print(f"📝 transcript: {_tpath}")
    lab = load_lab(LAB_ID)
    silicon = lab.silicon_nodes
    emulated = [n for n in lab.nodes if n.kind == "emulated"]

    print("═" * 74)
    print(f" {lab.display_name}")
    print("═" * 74)

    # ── RULE 1: PREFLIGHT THAT REFUSES A VERDICT ────────────────────────────
    # The scorer's own assumptions are checked FIRST — before the boards, before
    # the wire. A grader whose tokens have moved cannot produce a verdict about
    # anything else.
    problems = _verify_tokens_still_exist()
    if not silicon:
        problems.append("no silicon node — this lab cannot make its claim without one")
    for n in silicon:
        rc, out = _ssh(n.host, "echo ok")
        if rc != 0 or "ok" not in out:
            problems.append(f"{n.name}: cannot reach {n.host} over ssh — {out.strip()[:100]}")
    if not (Path(__file__).resolve().parent / "l2beacon.py").is_file():
        problems.append("tools/l2beacon.py missing — nothing to stage on the boards")
    if problems:
        print("\n🛑 ABORTED — NO VERDICT GIVEN:")
        for p in problems:
            print(f"   · {p}")
        print("\n   A setup that cannot work must never reach the scoring code.")
        return 2

    EVIDENCE.mkdir(parents=True, exist_ok=True)
    mgr = SessionManager()
    coord = LabCoordinator(mgr)
    running = await coord.launch(lab, auto_ip=False, on_event=lambda m: print(m))

    print(f"\n  holding {HOLD_S:.0f}s for the segment to exercise …")
    await asyncio.sleep(HOLD_S)

    # ── COLLECT EVIDENCE BEFORE TEARDOWN DESTROYS IT ────────────────────────
    guest_console = ""
    for n in emulated:
        sid = running.node_sessions.get(n.name)
        if not sid:
            continue
        clog = mgr.get(sid).console_log()
        if clog and Path(clog).is_file():
            shutil.copy(clog, EVIDENCE / f"{n.name}.console.log")
            guest_console = Path(clog).read_text(errors="replace")

    board_logs: dict[str, str] = {}
    for n in silicon:
        rc, out = _ssh(n.host, f"cat /tmp/holobench-{n.name}.log 2>/dev/null")
        board_logs[n.name] = out
        (EVIDENCE / f"{n.name}.board.log").write_text(out)

    await coord.stop(lab.id)
    print(f"\n  evidence preserved to {EVIDENCE}")

    # ── SCORE ───────────────────────────────────────────────────────────────
    print("\n" + "═" * 74)
    print(" PER-LEG VERDICT — graded on the SILICON side; the guest corroborates")
    print("═" * 74)
    verdicts = {}
    for n in silicon:
        v, notes = score_leg(n, board_logs.get(n.name, ""), guest_console)
        verdicts[n.name] = v
        icon = {"PASS": "✅", "FAIL": "❌", "INCONCLUSIVE": "⚠️ "}[v]
        print(f"\n{icon} {n.name:8} ({n.iface} @ {n.host}, et=0x{n.ethertype:04x}) — {v}")
        for note in notes:
            print(f"     {note}")

    npass = sum(1 for v in verdicts.values() if v == "PASS")
    nfail = sum(1 for v in verdicts.values() if v == "FAIL")
    ninc = sum(1 for v in verdicts.values() if v == "INCONCLUSIVE")

    print("\n" + "═" * 74)
    print(f" VERDICT   pass={npass}  fail={nfail}  inconclusive={ninc}  (of {len(silicon)} legs)")
    if npass == len(silicon):
        print(" ⭐⭐ THE MODEL IS INDISTINGUISHABLE FROM THE SILICON, TO THE SILICON —")
        print("    on every leg, asserted by hardware on the far side of a cable.")
    elif nfail:
        print(" ❌ A LEG GENUINELY FAILED — a real board did not receive the model's")
        print("    frames. That is a finding about the wire or the emulated NIC, and")
        print("    it is the kind worth taking to 95emulator with the console attached.")
    else:
        print(" ⚠️  INCONCLUSIVE — a killed or unstarted run is not a caught bug.")
    print("\n ⚠️  A leg is scored on the REAL BOARD's log. The guest sits on a macvtap")
    print("    where frames can be locally switched, so its own PASS is compatible")
    print("    with nothing leaving the NIC. Only the far end can refute that.")
    print("═" * 74)
    return 0 if npass == len(silicon) else (1 if nfail else 2)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

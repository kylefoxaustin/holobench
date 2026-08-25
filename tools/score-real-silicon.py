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
import re
import shutil
import subprocess
import sys
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
    ours = L2BEACON_SRC.read_text() if L2BEACON_SRC.is_file() else ""
    for name, tok in (("BOARD_PASS", BOARD_PASS), ("BOARD_UP", BOARD_UP),
                      ("BOARD_CORRUPT", BOARD_CORRUPT)):
        if not ours:
            problems.append("tools/l2beacon.py is missing — cannot verify board tokens")
            break
        if tok not in ours:
            problems.append(
                f"{name}={tok!r} is no longer emitted by tools/l2beacon.py. This "
                f"scorer would grade every leg INCONCLUSIVE and look like a quiet lab.")
    if ENET_LAB3_C.is_file():
        guest = ENET_LAB3_C.read_text()
        for name, tok in (("GUEST_PASS", GUEST_PASS), ("GUEST_CORRUPT", GUEST_CORRUPT)):
            if tok not in guest:
                problems.append(
                    f"{name}={tok!r} is no longer emitted by 95emulator's enet-lab3.c. "
                    f"The emitter moved and this scorer did not follow.")
    return problems


def _ssh(host: str, cmd: str, timeout: float = 10.0) -> tuple[int, str]:
    try:
        p = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={int(timeout)}", host, cmd],
            capture_output=True, text=True, timeout=timeout + 5)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 255, "ssh timed out"


def _rx_from(log: str) -> int:
    """Largest rx= seen in a board's PASS lines.

    Parses the PASS line — the artifact that CARRIES the assertion — and not the
    STATS line, which only exists once the beacon has exited. That distinction
    already cost this lab one false FAIL on a 40/40 run.
    """
    vals = [int(m) for m in re.findall(r"[( ]rx=(\d+)", log)]
    return max(vals) if vals else 0


def score_leg(node, board_log: str, guest_console: str) -> tuple[str, list[str]]:
    """Grade one leg. Returns (PASS|FAIL|INCONCLUSIVE, notes)."""
    notes: list[str] = []

    # RULE 3, first and hardest. If the board's beacon never came up, everything
    # downstream is a statement about THIS BUG, not about the wire.
    if BOARD_UP not in board_log:
        tail = board_log.strip().splitlines()[-1][:140] if board_log.strip() else "(empty log)"
        notes.append(f"the board's beacon NEVER STARTED — {tail}")
        notes.append("nothing was listening, so silence here is not evidence about the wire")
        return ("INCONCLUSIVE", notes)

    board_passes = board_log.count(BOARD_PASS)
    board_rx = _rx_from(board_log)
    board_corrupt = board_log.count(BOARD_CORRUPT)
    notes.append(f"board: PASS={board_passes} rx={board_rx} CORRUPT={board_corrupt}")

    # RULE 2: a positive integer from the artifact that carries the assertion.
    if board_rx <= 0 or board_passes <= 0:
        notes.append(f"⭐ the REAL BOARD did not receive 0x{GUEST_ET:04x} — this is a "
                     f"genuine negative and the load-bearing one")
        return ("FAIL", notes)

    if board_corrupt:
        notes.append(f"⚠️ the board CONDEMNED {board_corrupt} frame(s) — the emulated "
                     f"NIC put something malformed on a real wire. Worth 95emulator's eyes.")

    # The guest's side is corroboration, never the verdict.
    want = f"0x{node.ethertype:04x}"
    guest_saw = GUEST_PASS in guest_console
    if guest_saw:
        notes.append(f"guest corroborates: saw {want} ({guest_console.count(GUEST_PASS)} PASS lines)")
    else:
        notes.append(f"⚠️ guest did NOT report seeing {want}. The crossing is proven "
                     f"ONE WAY (board received the guest); the reverse is not shown.")
    if GUEST_CORRUPT in guest_console:
        notes.append(f"⚠️ guest logged {guest_console.count(GUEST_CORRUPT)} CORRUPT — "
                     f"the real board's frames arrived malformed to the model.")

    return ("PASS" if guest_saw else "INCONCLUSIVE", notes)


async def main() -> int:
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

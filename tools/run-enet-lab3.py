#!/usr/bin/env python3
"""Live runner + scorer for labs/mcx-rt1180-95-l2.yaml — the 3-node raw-L2 segment.

WHY THIS EXISTS AND `holobench lab launch` DOES NOT SUFFICE. Launching the lab proves the
wiring. It does not SCORE it, and the scoring is the whole argument:

  * **The heartbeat, not the verdict.** A re-arming node re-earns PASS forever, so its PASS
    line is not a one-shot verdict — it is a HEARTBEAT WITH THE WIRE IN THE LOOP. When a peer
    departs, a survivor that re-arms goes SILENT (it can no longer see both peers). When the
    peer returns, it RESUMES. **THE GAP IS THE DEPARTURE AND THE RESUME IS THE RECOVERY**, and
    that is the only assertion in this lab that survives the departure at all — every PASS
    token is otherwise earned before it.
  * **A TOTAL CANNOT SEE A GAP.** rt1180 wrote this scorer's first version by counting beats:
    20,587, green, meaningless. The count was never the signal — THE SPACING WAS. So we
    timestamp every beat as it arrives and assert on the INTERVALS.
  * **Grep the TOKEN, not the substring.** rt1180's monitor once matched its own `need 0x88b7`
    BANNER and shouted PASS twelve times at an empty wire.
  * **A timeout is INCONCLUSIVE, never a FAILURE** (mcxn947). A killed run is not a caught bug.

WHAT IT CANNOT SEE, STATED PLAINLY. The nodes' PASS assertion keys on ETHERTYPE, so a frame
whose BODY is garbage has the same ethertype as a good one. rt1180 measured 88 frames DMA'd
to guest address 0 in exactly this lab's late-arrival slot, and this scorer would have called
that run green. `ENET-LAB3 CORRUPT` is treated as a hard fail *the moment the node owners add
a checkable payload* (asked 2026-07-13); until then, ABSENCE OF `CORRUPT` IS NOT EVIDENCE OF
INTEGRITY — it is evidence that nobody is looking.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from holobench.labs.coordinator import LabCoordinator          # noqa: E402
from holobench.labs.loader import load_lab                      # noqa: E402
from holobench.session.manager import SessionManager            # noqa: E402

LAB_ID = "mcx-rt1180-95-l2"

# THE FLEET CONTRACT — a mandated PREFIX, and nothing more. Four nodes emit THREE different
# formats and share this prefix; for two days they shared it BY LUCK, because nobody had ever
# agreed on it:
#     mcx      "ENET-LAB3 PASS: saw BOTH peers on the segment"
#     rt1180   "ENET-LAB3 PASS #7: saw BOTH peers on the segment"     <- the '#' is rt1180's ALONE
#     imx95    "ENET-LAB3 PASS: saw BOTH peers on the segment (...)"
#     imx91    "ENET-LAB3 PASS: t=12.030s peers=2/2 beat=217"         <- a different tail entirely
#
# rt1180 found that its own README documented a token its binary never printed — correct — and
# then prescribed `ENET-LAB3 PASS #` as the fleet token, which matches ONE of the four. Adopting
# it would have scored mcx, imx95 and imx91 red on a segment where all three were beating. It
# generalised from its ARTIFACT to the CONTRACT: the same move its README made when it drifted
# from its ELF.
#
# ⭐ A TOKEN THAT HAPPENS TO MATCH IS NOT A TOKEN YOU AGREED ON — and the difference is invisible
#   right up until someone "fixes" it. Match the agreed prefix. Never a banner.
PASS_TOKEN = "ENET-LAB3 PASS"
CORRUPT_TOKEN = "ENET-LAB3 CORRUPT"

# PHASE GATE. A receiver that enforces a field its senders do not yet emit CONDEMNS THE HONEST —
# and here the false positive is byte-identical to the true positive ("magic=0" is exactly what a
# frame DMA'd to guest address 0 looks like: a buffer that was never written). So CORRUPT is only
# a HARD FAIL once every node emits the body. Until then it is reported and NOT scored, because a
# red we cannot trust is worse than no red: it gets the check deleted by the people it protects.
#   EMIT status 2026-07-13: mcx ✅ · rt1180 ⏳ · imx95 ⏳ · imx91 ⏳
ENFORCE_CORRUPT = False
POLL_S = 0.25
MARGIN_S = 90
# A beat is "live" if we have seen one within this long. Generous: the bare-metal nodes beat
# every few hundred ms, the Linux node far slower, and we are timing an 18s+ window.
BEAT_TIMEOUT_S = 20.0


class Beats:
    """Timestamps every PASS/CORRUPT line as it lands, per node. We stamp on ARRIVAL rather
    than trusting a guest clock — this is an OBSERVER measurement and is honest about it. It
    is sound here only because the interval we assert on (tens of seconds) dwarfs host
    scheduling jitter; a finer timing claim would need `-icount`."""

    def __init__(self) -> None:
        self.beats: dict[str, list[float]] = {}
        self.corrupt: dict[str, list[str]] = {}
        self._seen: dict[str, int] = {}

    def poll(self, node: str, log: Path | None, t: float) -> None:
        if not log or not log.exists():
            return
        try:
            text = log.read_text(errors="replace")
        except OSError:
            return
        lines = text.splitlines()
        start = self._seen.get(node, 0)
        for ln in lines[start:]:
            if PASS_TOKEN in ln:
                self.beats.setdefault(node, []).append(t)
            elif CORRUPT_TOKEN in ln:
                self.corrupt.setdefault(node, []).append(ln.strip())
        self._seen[node] = len(lines)

    def gap_around(self, node: str, lo: float, hi: float) -> tuple[float, float] | None:
        """The beat gap that BRACKETS [lo, hi] — last beat before `lo`, first beat after `hi`."""
        bs = self.beats.get(node) or []
        before = [b for b in bs if b <= lo]
        after = [b for b in bs if b >= hi]
        if not before or not after:
            return None
        return (max(before), min(after))


async def main() -> int:
    lab = load_lab(LAB_ID)
    mgr = SessionManager()
    coord = LabCoordinator(mgr)
    beats = Beats()

    departing = [n for n in lab.nodes if n.stop_at is not None]
    if not departing:
        print("this lab has no departure — nothing to assert across", file=sys.stderr)
        return 2
    dep = departing[0]
    survivors = [n.name for n in lab.nodes if n.name != dep.name]

    print(f"=== {lab.display_name}")
    print(f"=== horizon {lab.horizon_s:.0f}s + {MARGIN_S}s margin")
    print(f"=== departure: {dep.name} leaves t+{dep.stop_at:.0f}"
          + (f", REJOINS t+{dep.rejoin_at:.0f}" if dep.rejoin_at else " (never returns)"))
    print()

    running = await coord.launch(lab, on_event=lambda m: print(m, flush=True))
    t0 = running.t0
    loop = asyncio.get_running_loop()

    async def watch() -> None:
        while True:
            t = loop.time() - t0
            for n in lab.nodes:
                sid = running.node_sessions.get(n.name)
                if not sid:
                    continue
                try:
                    beats.poll(n.name, mgr.get(sid).console_log(), t)
                except Exception:
                    pass
            await asyncio.sleep(POLL_S)

    w = asyncio.create_task(watch())
    deadline = lab.horizon_s + MARGIN_S
    print(f"\nobserving to t+{deadline:.0f}s (timestamping every heartbeat) ...\n", flush=True)
    await asyncio.sleep(max(0.0, deadline - (loop.time() - t0)))
    w.cancel()

    print("=" * 78)
    print("TIMELINE (measured, not requested)")
    print("=" * 78)
    for n in sorted(lab.nodes, key=lambda n: n.start_at):
        a = running.node_arrivals.get(n.name)
        line = f"  {n.name:7} arrive t+{a:6.1f}s" if a is not None else f"  {n.name:7} NEVER ARRIVED"
        if n.name in running.node_departures:
            line += f" · DEPART t+{running.node_departures[n.name]:6.1f}s (coordinator-issued)"
        if n.name in running.node_rejoins:
            line += f" · REJOIN t+{running.node_rejoins[n.name]:6.1f}s"
        nb = len(beats.beats.get(n.name, []))
        line += f"   [{nb} heartbeats]"
        print(line)

    fails: list[str] = []
    inconclusive: list[str] = []

    print("\n" + "=" * 78)
    print(f"1) EVERY NODE PASSED — literal {PASS_TOKEN!r} in its OWN console")
    print("=" * 78)
    for n in lab.nodes:
        nb = len(beats.beats.get(n.name, []))
        print(f"  {n.name:7} {'✅' if nb else '❌'} {nb} heartbeat(s)")
        if not nb:
            fails.append(f"{n.name} never printed {PASS_TOKEN}")

    print("\n" + "=" * 78)
    print(f"2) NO CORRUPTION — literal {CORRUPT_TOKEN!r} anywhere")
    print("=" * 78)
    total_corrupt = sum(len(v) for v in beats.corrupt.values())
    if total_corrupt:
        for node, lines in beats.corrupt.items():
            for ln in lines[:5]:
                print(f"  {'❌' if ENFORCE_CORRUPT else '⚠️ '} {node}: {ln}")
        if ENFORCE_CORRUPT:
            fails.append(f"{total_corrupt} corrupt frame report(s)")
        else:
            print("  ⚠️  REPORTED, NOT SCORED — phase 1: not every node emits the body yet, so a")
            print("      CORRUPT here may just be an honest peer that has not upgraded. And that")
            print("      false positive is BYTE-IDENTICAL to the true one (magic=0 is exactly what")
            print("      a frame DMA'd to guest address 0 looks like). A red we cannot trust is")
            print("      worse than no red: it gets the check deleted by the people it protects.")
            inconclusive.append(f"{total_corrupt} CORRUPT report(s) — unscoreable until all nodes emit")
    else:
        print("  (none reported)")
        print("  ⚠️  BUT: the nodes' PASS keys on ETHERTYPE. A frame whose BODY is garbage has")
        print("      the same ethertype as a good one. rt1180 measured 88 frames DMA'd to guest")
        print("      address 0 in this lab's late-arrival slot and this check saw nothing.")
        print("      ABSENCE OF 'CORRUPT' IS NOT EVIDENCE OF INTEGRITY UNTIL THE NODES CARRY A")
        print("      CHECKABLE PAYLOAD — it is evidence that nobody is looking.")

    print("\n" + "=" * 78)
    print("3) ⭐ THE DEPARTURE WAS SURVIVED — the survivors' heartbeat GAP brackets it,")
    print("      and RESUMES on the rejoin. This is the ONLY assertion that outlives the")
    print("      departure; every PASS token is otherwise earned before it.")
    print("=" * 78)
    d_at = running.node_departures.get(dep.name)
    r_at = running.node_rejoins.get(dep.name)
    if d_at is None:
        inconclusive.append("the scheduled departure never fired")
        print("  ⚠️  INCONCLUSIVE: the departure never fired")
    elif r_at is None:
        inconclusive.append(f"{dep.name} never rejoined — recovery cannot be asserted")
        print(f"  ⚠️  INCONCLUSIVE: {dep.name} never came back; a stopped heartbeat proves the")
        print("      wire NOTICED the loss, never that it SURVIVED it")
    else:
        for s in survivors:
            bs = beats.beats.get(s, [])
            # A LATCHED node — one that prints PASS once and stops caring — has NO ORACLE at
            # departure time: its last beat lands long before the wire ever got interesting.
            # That is a node we CANNOT SEE THROUGH, not a wire that broke.
            #
            # ⭐ AN UNSCOREABLE NODE IS *INCONCLUSIVE*, NEVER A *FAILURE*. (mcxn947's rule: a
            #   killed run is not a caught bug.) Calling it FAIL would conflate "I cannot see"
            #   with "it is broken" — which is the same collapsed oracle this whole lab exists
            #   to avoid, and the first version of this scorer did exactly that.
            latched = not bs or max(bs) < d_at - BEAT_TIMEOUT_S
            if latched:
                n = len(bs)
                print(f"  ⚠️  {s:7} LATCHED — {n} beat(s), last at "
                      f"t+{(max(bs) if bs else 0):.1f}s, long before the departure.")
                print(f"           A latched assertion prints PASS once and shows NOTHING here.")
                print(f"           NOT A FAILURE — an unscoreable node is INCONCLUSIVE. Ask its")
                print(f"           owner to RE-ARM (clear the seen-flags after PASS, like mcx).")
                inconclusive.append(f"{s} is LATCHED — it cannot witness a departure")
                continue
            g = beats.gap_around(s, d_at, r_at)
            if g is None:
                # It WAS beating into the departure and never came back. That is a real signal:
                # the wire lost this node's peer and never gave it back.
                print(f"  ❌ {s:7} was beating up to the departure and NEVER RESUMED "
                      f"(last beat t+{max(bs):.1f}s) — the wire did not recover for it")
                fails.append(f"{s}: heartbeat never resumed after the rejoin")
                continue
            last_before, first_after = g
            gap = first_after - last_before
            window = r_at - d_at
            went_quiet = last_before <= d_at + BEAT_TIMEOUT_S
            came_back = first_after <= r_at + BEAT_TIMEOUT_S
            ok = went_quiet and came_back and gap >= window * 0.5
            mark = "✅" if ok else "❌"
            print(f"  {mark} {s:7} last beat t+{last_before:6.1f}s → first beat t+{first_after:6.1f}s"
                  f"   GAP {gap:5.1f}s  (departure window {window:.0f}s)")
            if not ok:
                fails.append(
                    f"{s}: heartbeat gap {gap:.1f}s does not bracket the "
                    f"{window:.0f}s departure window")
            else:
                print(f"           → went silent when {dep.name} left, RESUMED when it returned:")
                print(f"             the wire absorbed the loss AND recovered.")

    print("\n" + "=" * 78)
    if fails:
        print("RESULT: FAIL")
        for f in fails:
            print(f"   ❌ {f}")
    elif inconclusive:
        print("RESULT: INCONCLUSIVE (a killed or unscoreable run is NOT a caught bug)")
        for i in inconclusive:
            print(f"   ⚠️  {i}")
    else:
        print("RESULT: PASS — all nodes saw both peers on a staggered segment, and the")
        print("        survivors' heartbeat went silent for the departure and RESUMED on the")
        print("        rejoin. The departure was not merely witnessed; it was SURVIVED.")
    print("=" * 78)

    print("\nstopping lab ...")
    await coord.stop(lab.id)
    print("done.")
    return 1 if fails else (2 if inconclusive else 0)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

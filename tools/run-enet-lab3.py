#!/usr/bin/env python3
"""Live runner + scorer for labs/mcx-rt1180-95-l2.yaml — the 4-node raw-L2 segment.

WHY THIS EXISTS AND `holobench lab launch` DOES NOT SUFFICE. Launching the lab proves the
wiring. It does not SCORE it, and the scoring is the whole argument:

  * **The heartbeat, not the verdict.** A re-arming node re-earns PASS forever, so its PASS
    line is not a one-shot verdict — it is a HEARTBEAT WITH THE WIRE IN THE LOOP. When a peer
    departs, a survivor that re-arms goes SILENT. When the peer returns, it RESUMES.
    **THE GAP IS THE DEPARTURE AND THE RESUME IS THE RECOVERY.**
  * **A TOTAL CANNOT SEE A GAP.** The first version of this scorer counted beats: 20,587,
    green, meaningless. The count was never the signal — THE SPACING WAS.
  * **Grep the TOKEN, not the substring.** rt1180's monitor once matched its own `need 0x88b7`
    BANNER and shouted PASS twelve times at an empty wire.
  * **A timeout is INCONCLUSIVE, never a FAILURE** (mcxn947). A killed run is not a caught bug.

THE FOUR THINGS THIS SCORER LEARNED FROM THE FLEET'S DEPARTURE-TEST FAILURES (mcxn947,
2026-07-14 01:03 — six attempts, and NOT ONE failure was in the firmware):

  ① AN OBSERVER THAT CANNOT KEEP UP WITH ITS SUBJECT IS OBSERVING ITS OWN BACKLOG. Their node
    printed every frame, fell behind the wire, and a KILLED peer's stale backlog kept
    refreshing its liveness timer. We poll a file and stamp on arrival — so we assert only on
    intervals that DWARF our own poll period, and we say so.
  ② A LIVENESS TIMEOUT THAT IS TOO SHORT DOES NOT FAIL SAFE — IT MANUFACTURES DEPARTURES THAT
    NEVER HAPPENED. Their 300-spin hold declared a LIVE peer dead 75 times. So we no longer
    hardcode a timeout and hope: we MEASURE each node's own inter-beat spacing and refuse to
    score any node whose normal quiet period is within reach of our timeout (§0).
  ③ NEVER TEST A BINARY YOU DID NOT JUST BUILD. Holobench cannot: it never builds any of them.
    Its version is boot.pin — a hash gate that REFUSES TO LAUNCH on drift (session/manager.py).
  ④ A KILL THAT REACHES THE WRAPPER AND NOT THE PROCESS IS NOT A KILL. Five of their "failed"
    departure tests were scoring a departure that never happened. So we VERIFY THE DEPARTED
    NODE IS ACTUALLY DEAD before we score its departure, and an un-killable peer is
    INCONCLUSIVE, not a failure (§3).

WHAT WE CAN SEE THAT NO SINGLE NODE CAN. mcxn947 noted that on its node a stalled ring and a
departed peer produce the SAME signal (a rejected frame never refreshes a peer's liveness), and
called that the honest answer — from the segment's point of view they ARE the same event.
Correct for a node. NOT correct for the coordinator: **we scheduled the departure, so we know
when it was.** A heartbeat gap that BRACKETS t+stop_at is the departure we ordered. A gap
ANYWHERE ELSE is a wire fault, and nobody but us is in a position to tell them apart (§4).
"""
from __future__ import annotations

import asyncio
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from holobench.labs.coordinator import LabCoordinator          # noqa: E402
from holobench.labs.loader import load_lab                      # noqa: E402
from holobench.profiles.loader import load_profile              # noqa: E402
from holobench.session.manager import (                          # noqa: E402
    SessionManager,
    live_orphan_boards,
)

LAB_ID = "mcx-rt1180-95-l2"

# THE FLEET CONTRACT — a mandated PREFIX, and nothing more. Four nodes emit FOUR different
# formats and share this prefix; for two days they shared it BY LUCK, because nobody had ever
# agreed on it:
#     mcx      "ENET-LAB3 PASS: saw BOTH peers on the segment"
#     rt1180   "ENET-LAB3 PASS #7: saw BOTH peers on the segment"     <- the '#' is rt1180's ALONE
#     imx95    "ENET-LAB3 PASS: saw BOTH peers on the segment (...)"
#     imx91    "ENET-LAB3 PASS: t=12.030s peers=2/2 beat=217"         <- a different tail entirely
#
# rt1180 found its own README documented a token its binary never printed — correct — and then
# prescribed `ENET-LAB3 PASS #` as the FLEET token, which matches ONE of the four. Adopting it
# would have scored three honest nodes red. It generalised from its ARTIFACT to the CONTRACT:
# the same move its README made when it drifted from its ELF.
#
# ⭐ A TOKEN THAT HAPPENS TO MATCH IS NOT A TOKEN YOU AGREED ON — and the difference is invisible
#   right up until someone "fixes" it. Match the agreed PREFIX. Never a banner.
#
# The same bug tried to recur FIVE MINUTES after the contract was ratified: rt1180 introduced
# `ENET-LAB3 PAYLOAD-REPLAY` as a NEW token and asked scorers to grep a new prefix. 91emulator
# caught it. The kind now goes AFTER the ratified token, where the contract says free-form is
# welcome — so this one grep sees replays, garbage, and anything invented next week:
#     ENET-LAB3 CORRUPT: PAYLOAD-REPLAY peer 0x88b5 seq 2 <= last 2 -- a STALE BUFFER
#     ^^^^ ratified ^^^^  ^^^^ the node's own kind ^^^^
PASS_TOKEN = "ENET-LAB3 PASS"
CORRUPT_TOKEN = "ENET-LAB3 CORRUPT"
# THE NODE'S SELF-DECLARED CONTRACT — and the third time in two days this exact bug tried to
# ship. imx91 announces, machine-readably:
#
#   ENET-LAB3 UP: if=eth0 mac=52:54:00:12:34:b8 ethertype=0x88B8 peers=2
#                 body=emit enforce=self-arming(per-peer)
#
# I wrote `UP_TOKEN = "ENET-LAB3 UP"` and grepped all four nodes with it. rt1180's banner is
# `ENET-LAB3 up:` — LOWERCASE. mcx's and imx95's are different again. So there is NO agreed
# declaration line: I had generalised from ONE node's artifact to a fleet contract, which is
# precisely what rt1180 did with `PASS #` and what its README did with the token its ELF never
# printed. Three times, same shape, three different authors.
#
# ⭐ WHAT A NODE HAPPENS TO PRINT IS NOT AN INTERFACE. An interface is something the fleet
#   AGREED to. Until it does, this scorer refuses to pretend: a node with no declared contract
#   is reported as UNDECLARED and its CORRUPT lines are NOT scored — because we cannot tell
#   whether it can distinguish a bad frame from a peer that has not upgraded.
#
# Proposed to the fleet (not assumed): every node prints exactly this at bring-up, and the
# scorer derives the emit-status board from it instead of anyone maintaining one by hand.
UP_TOKEN = "ENET-LAB3 UP:"

POLL_S = 0.25
MARGIN_S = 180          # the 4th node arrives at t+450 and needs time to boot AND be scored

# A beat is "live" if we have seen one within this long. NOT a tuning knob to be nudged until
# the test goes green — §0 MEASURES whether this value is defensible for each node and refuses
# to score the ones it isn't. mcxn947 tuned the equivalent constant from 20000 to 300 and
# manufactured 75 departures that never happened; the number was never the problem, the fact
# that it was a GUESS was the problem.
BEAT_TIMEOUT_S = 20.0
# A node's own normal quiet period must be comfortably INSIDE the timeout, or a routine pause
# in its beacon is indistinguishable from a departure. 3x is the margin we demand.
QUIET_MARGIN = 3.0


class Beats:
    """Timestamps every PASS/CORRUPT/UP line as it lands, per node. We stamp on ARRIVAL rather
    than trusting a guest clock — this is an OBSERVER measurement and is honest about it. It is
    sound here ONLY because the interval we assert on (tens of seconds) dwarfs both host
    scheduling jitter and our own 0.25s poll; a finer timing claim would need `-icount`."""

    def __init__(self) -> None:
        self.beats: dict[str, list[float]] = {}
        self.corrupt: dict[str, list[str]] = {}
        self.banner: dict[str, str] = {}
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
            elif UP_TOKEN in ln and node not in self.banner:
                self.banner[node] = ln.strip()
        self._seen[node] = len(lines)

    def quiet_period(self, node: str, exclude: tuple[float, float] | None) -> float | None:
        """This node's LONGEST normal gap between beats, ignoring the departure window.

        This is the number that decides whether BEAT_TIMEOUT_S is defensible FOR THIS NODE. A
        node that normally goes quiet for 15s cannot be scored against a 20s timeout: its
        ordinary silence and a real departure would be the same observation."""
        bs = self.beats.get(node) or []
        if len(bs) < 2:
            return None
        gaps = []
        for a, b in zip(bs, bs[1:]):
            if exclude and not (b < exclude[0] or a > exclude[1]):
                continue        # this gap straddles the departure — that's the SIGNAL, not noise
            gaps.append(b - a)
        return max(gaps) if gaps else None

    def gap_around(self, node: str, lo: float, hi: float) -> tuple[float, float] | None:
        """The beat gap that BRACKETS [lo, hi] — last beat before `lo`, first beat after `hi`."""
        bs = self.beats.get(node) or []
        before = [b for b in bs if b <= lo]
        after = [b for b in bs if b >= hi]
        if not before or not after:
            return None
        return (max(before), min(after))

    def unscheduled_gaps(self, node: str, window: tuple[float, float],
                         threshold: float) -> list[tuple[float, float]]:
        """Beat gaps longer than `threshold` that do NOT overlap the scheduled departure.

        ⭐ THE ASSERTION ONLY THE COORDINATOR CAN MAKE. A node cannot tell a stalled ring from a
        departed peer — mcxn947 showed both produce the identical signal, and called that
        honest, and it is. But WE ORDERED THE DEPARTURE. We know when it was. So a gap that
        brackets it is the event we asked for, and a gap ANYWHERE ELSE is a wire fault that
        every node on the segment is structurally unable to name."""
        bs = self.beats.get(node) or []
        out = []
        for a, b in zip(bs, bs[1:]):
            if b - a < threshold:
                continue
            if not (b < window[0] or a > window[1]):
                continue        # overlaps the departure: expected
            out.append((a, b))
        return out


def _fmt(v: float) -> str:
    return f"t+{v:.1f}s"


def _peer_et(line: str) -> str:
    """Pull the offending peer's ethertype out of a CORRUPT line.

    Every node spells this differently — `et=0x88b7`, `et 0x88b5`, `peer 0x88b5` — which is
    the same un-agreed-format problem as the PASS token, one layer down. We accept all three
    rather than mandate a fourth, because the FLEET has not agreed on one and inventing one
    here is exactly the move that has now failed four times.
    """
    import re
    m = re.search(r"(?:et=|et |peer )(0x[0-9a-fA-F]{4})", line)
    return m.group(1).lower() if m else "0x????"


async def main() -> int:
    # ── PREFLIGHT: REFUSE A WIRE THAT IS NOT EMPTY ───────────────────────────────────────
    # ⭐ AN ORPHANED PROCESS ON A SHARED BUS IS NOT A LEAK. IT IS A LIAR THAT OUTLIVED THE
    #   RUN THAT CREATED IT — and its testimony is indistinguishable from a peer's.
    #
    # This check exists because the alternative already happened. A killed runner orphaned
    # its QEMU children; the mcast group comes from an empty per-coordinator set, so the
    # next run landed on the SAME WIRE (a guarantee, not a race). That run was a 4-node lab
    # sharing a segment with a GHOST of the previous one, still speaking the OLD protocol.
    # Every node rejected every other node, and this scorer faithfully reported that
    # rt1180's self-ethertype field was broken and imx95 was still emitting ASCII — about
    # two sessions that had fixed exactly those bugs hours earlier.
    #
    # A false red is the expensive kind of wrong. It spends someone else's night on a
    # phantom and teaches them to distrust the one signal that was telling the truth.
    orphans = live_orphan_boards()
    if orphans:
        print("REFUSING TO RUN: a board from an earlier run is STILL ALIVE and it is on "
              "this lab's multicast segment.\n", file=sys.stderr)
        for d in orphans:
            print(f"    {d}", file=sys.stderr)
        print("\nIts frames are indistinguishable from a real peer's, so this run would "
              "score a segment\nit is not actually testing — and would blame the nodes. "
              "Reap them, then re-run.", file=sys.stderr)
        return 3

    lab = load_lab(LAB_ID)
    mgr = SessionManager()
    coord = LabCoordinator(mgr)
    beats = Beats()

    profiles = {n.name: load_profile(n.profile) for n in lab.nodes}

    departing = [n for n in lab.nodes if n.stop_at is not None]
    if not departing:
        print("this lab has no departure — nothing to assert across", file=sys.stderr)
        return 2
    dep = departing[0]
    survivors = [n.name for n in lab.nodes
                 if n.name != dep.name and n.start_at < dep.stop_at]
    joiners = [n for n in lab.nodes if n.start_at > dep.stop_at]

    print(f"=== {lab.display_name}")
    print(f"=== horizon {lab.horizon_s:.0f}s + {MARGIN_S}s margin")
    print(f"=== departure: {dep.name} leaves t+{dep.stop_at:.0f}"
          + (f", REJOINS t+{dep.rejoin_at:.0f}" if dep.rejoin_at else " (never returns)"))
    for j in joiners:
        print(f"=== post-departure joiner: {j.name} arrives t+{j.start_at:.0f} "
              f"(AFTER the departure — the only oracle that cannot be pre-satisfied)")
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

    d_at = running.node_departures.get(dep.name)
    r_at = running.node_rejoins.get(dep.name)
    dep_window = (d_at, r_at) if (d_at is not None and r_at is not None) else None

    print("=" * 78)
    print("TIMELINE (measured, not requested)")
    print("=" * 78)
    for n in sorted(lab.nodes, key=lambda x: x.start_at):
        a = running.node_arrivals.get(n.name)
        line = f"  {n.name:7} arrive {_fmt(a):>10}" if a is not None else f"  {n.name:7} NEVER ARRIVED"
        if n.name in running.node_departures:
            line += f" · DEPART {_fmt(running.node_departures[n.name])} (coordinator-issued)"
        if n.name in running.node_rejoins:
            line += f" · REJOIN {_fmt(running.node_rejoins[n.name])}"
        line += f"   [{len(beats.beats.get(n.name, []))} heartbeats]"
        print(line)

    # ── PERSIST THE BEAT TIMESTAMPS, so a completed run stays DIAGNOSABLE ────────────────
    # ⭐ THE EVIDENCE MUST OUTLIVE THE RUN. On the v2-cutover run the survivor analysis was
    # anomalous — §0 (dense 0.4s beating) disagreed with §4 (no beat before the departure) —
    # and I could not tell a real finding from a scorer bug, because coord.stop() reaps the
    # console logs and the beat timestamps vanished WITH the verdict. A scorer that keeps only
    # its conclusion and not its evidence cannot be audited, and an unauditable RED is worth as
    # little as an unauditable GREEN. So dump every beat, per node, BEFORE we tear anything down.
    dump = Path(__file__).resolve().parent.parent / "scratchpad-beats.jsonl"
    try:
        import json
        with dump.open("w") as fh:
            for n in lab.nodes:
                fh.write(json.dumps({
                    "node": n.name,
                    "arrival": running.node_arrivals.get(n.name),
                    "departure": running.node_departures.get(n.name),
                    "rejoin": running.node_rejoins.get(n.name),
                    "beats": [round(b, 2) for b in beats.beats.get(n.name, [])],
                    "corrupt_sample": beats.corrupt.get(n.name, [])[:20],
                }) + "\n")
        print(f"\n(beat timestamps persisted to {dump} — a completed run stays diagnosable)")
    except Exception as exc:
        print(f"\n(could not persist beats: {exc})")

    fails: list[str] = []
    inconclusive: list[str] = []

    # ── 0) THE INSTRUMENT, BEFORE THE SUBJECT ────────────────────────────────────────────
    # mcxn947 lost SIX departure runs to instrument bugs and not one to firmware. So this
    # scorer audits ITSELF first, and any node it cannot honestly score is named as such
    # BEFORE its result is read — not explained away afterwards.
    print("\n" + "=" * 78)
    print("0) ⭐ THE INSTRUMENT IS AUDITED FIRST — a scorer that cannot see a node must SAY SO")
    print("     before it reports on it, not after. (mcxn947: six failed departure runs, every")
    print("     one an instrument bug, every one of which LOOKED like a model bug.)")
    print("=" * 78)
    unscoreable: set[str] = set()
    for n in lab.nodes:
        q = beats.quiet_period(n.name, dep_window)
        nb = len(beats.beats.get(n.name, []))
        if nb < 2:
            print(f"  ⚠️  {n.name:7} {nb} beat(s) — NOT ENOUGH TO MEASURE ITS OWN SPACING.")
            print(f"           A node with one beat has no interval, and a scorer that asserts")
            print(f"           on intervals cannot see it. Unscoreable, NOT failed.")
            unscoreable.add(n.name)
            continue
        if q is None:
            # ≥2 beats, but EVERY gap between them straddles the departure window — so we
            # have no sample of this node's NORMAL spacing to judge the timeout against.
            # That is not a healthy node and it is not a broken one: it is a node we cannot
            # calibrate against. Unscoreable, never failed.
            #
            # This branch exists because its absence CRASHED the scorer on a completed run
            # and destroyed the verdict — `None` formatted as a float. THE FIFTH INSTRUMENT
            # BUG IN THIS LAB, and once again the subject was fine and the observer was not.
            # A scorer that dies is not a scorer that found nothing; it is a scorer that
            # found nothing OUT.
            print(f"  ⚠️  {n.name:7} {nb} beat(s), but EVERY gap straddles the departure —")
            print(f"           no sample of its NORMAL spacing, so the timeout cannot be")
            print(f"           calibrated for it. Unscoreable, NOT failed.")
            unscoreable.add(n.name)
            inconclusive.append(f"{n.name}: no normal beat-gap to calibrate the timeout against")
            continue
        headroom = BEAT_TIMEOUT_S / q if q else float("inf")
        ok = headroom >= QUIET_MARGIN
        print(f"  {'✅' if ok else '⚠️ '} {n.name:7} longest NORMAL quiet {q:5.1f}s vs "
              f"{BEAT_TIMEOUT_S:.0f}s timeout — {headroom:.1f}x headroom")
        if not ok:
            print(f"           TOO CLOSE. This node's ORDINARY silence is within {QUIET_MARGIN}x of")
            print(f"           the timeout, so a routine pause and a real departure would be the")
            print(f"           SAME OBSERVATION. Scoring it would MANUFACTURE a departure.")
            unscoreable.add(n.name)
            inconclusive.append(
                f"{n.name}: quiet period {q:.1f}s too close to the {BEAT_TIMEOUT_S:.0f}s timeout")

    # ── 1) EVERY NODE PASSED ─────────────────────────────────────────────────────────────
    print("\n" + "=" * 78)
    print(f"1) EVERY NODE PASSED — literal {PASS_TOKEN!r} in its OWN console")
    print("=" * 78)
    for n in lab.nodes:
        nb = len(beats.beats.get(n.name, []))
        print(f"  {n.name:7} {'✅' if nb else '❌'} {nb} heartbeat(s)")
        if not nb:
            fails.append(f"{n.name} never printed {PASS_TOKEN}")

    # ── 2) THE PROFILE'S CLAIM vs THE NODE'S OWN BANNER ──────────────────────────────────
    # The derived emit-board. On 2026-07-13 holobench published a status board saying
    # `imx91 ⏳` (emits no checkable body) while holding open, in the same session, the source
    # file that had emitted it for three hours. 91emulator's diagnosis:
    #   ⭐ A STATUS BOARD THAT IS NOT DERIVED IS A STATUS BOARD THAT DRIFTS.
    # So nothing here is transcribed. The node ANNOUNCES its own contract at boot and we check
    # the profile against it. A profile that OVERCLAIMS is a hard fail: it is the thing that
    # would make us enforce CORRUPT against a node that cannot emit a body.
    print("\n" + "=" * 78)
    print("2) ⭐ THE NODE'S OWN BANNER IS THE SOURCE OF TRUTH — the profile is only a CLAIM,")
    print("      and a claim that disagrees with the board it describes is the bug.")
    print("=" * 78)
    enforcing: set[str] = set()
    undeclared: list[str] = []
    for n in lab.nodes:
        ln = profiles[n.name].lab_node
        banner = beats.banner.get(n.name)

        if banner is None:
            # NOT a failure, and NOT something to paper over with a guess. This node simply
            # does not declare a machine-readable contract, because the fleet never agreed on
            # one. We refuse to infer it from what the node "obviously" does.
            undeclared.append(n.name)
            print(f"  ⚠️  {n.name:7} UNDECLARED — prints no {UP_TOKEN!r} line.")
            if ln:
                print(f"           The PROFILE claims body={ln.emits_checkable_body} "
                      f"self-arm={ln.enforces_on_arm}, and NOTHING CAN CHECK THAT CLAIM. It is")
                print(f"           a transcription, and a transcription nobody verifies is exactly")
                print(f"           the status board that drifts. Not scored, not trusted.")
            print(f"           Its CORRUPT lines will NOT be scored: we cannot tell whether it")
            print(f"           can distinguish a bad frame from a peer that has not upgraded.")
            continue

        says_body = "body=emit" in banner
        says_arm = "self-arming" in banner
        print(f"  ✅ {n.name:7} DECLARES: body={'emit' if says_body else 'NONE'} "
              f"enforce={'self-arming(per-peer)' if says_arm else 'unconditional/none'}")

        if ln is None:
            print(f"           (no profile claim to check it against — the banner IS the fact)")
        else:
            drift = []
            if ln.emits_checkable_body != says_body:
                drift.append(f"emits_checkable_body: profile={ln.emits_checkable_body} "
                             f"board={says_body}")
            if ln.enforces_on_arm != says_arm:
                drift.append(f"enforces_on_arm: profile={ln.enforces_on_arm} board={says_arm}")
            for d in drift:
                # Either direction is a bug. My own stale board UNDERclaimed (said imx91 ⏳ when
                # it had been ✅ for three hours) and that was still the bug.
                print(f"           ❌ THE PROFILE DISAGREES WITH THE BOARD: {d}")
                fails.append(f"{n.name}: profile drifted from the board — {d}")
            if not drift:
                print(f"           profile agrees with the board (pinned artifact + declared "
                      f"contract + live banner: three-way)")
        if says_arm:
            enforcing.add(n.name)

    if undeclared:
        print()
        print(f"  ⚠️  {len(undeclared)} of {len(lab.nodes)} nodes declare no contract: "
              f"{', '.join(undeclared)}")
        print("      This is the honest state of the fleet, not a holobench bug — and it is the")
        print("      reason the emit-status board keeps going stale. ASKED (not assumed): every")
        print("      node prints one line at bring-up —")
        print("         ENET-LAB3 UP: ethertype=0x.... peers=N body=emit|none "
              "enforce=self-arming|unconditional|none")
        print("      Then nobody maintains a board; the board IS the segment, read off the wire.")
        inconclusive.append(
            f"{len(undeclared)} node(s) declare no contract ({', '.join(undeclared)}) — "
            f"their CORRUPT is unscoreable")

    # ── 3) CORRUPTION — enforced PER NODE, because trustworthiness is a FIRMWARE property ──
    print("\n" + "=" * 78)
    print(f"3) NO CORRUPTION — literal {CORRUPT_TOKEN!r}, scored PER NODE")
    print("=" * 78)
    print("   ⭐ THE FLAG DAY IS RETIRED, AND 91emulator RETIRED IT. I had called a phase-gated")
    print("      rollout: nobody enforces the body until everybody emits it, because a receiver")
    print("      that enforces a field its senders do not emit CONDEMNS THE HONEST — and here the")
    print("      false positive is BYTE-IDENTICAL to the true one (`magic=0` is exactly what a")
    print("      frame DMA'd to guest address 0 looks like: a buffer that was never written).")
    print("      91's escape needs no coordination at all:")
    print()
    print("         A PEER THAT HAS EVER EMITTED A VALID BODY CANNOT STOP KNOWING HOW.")
    print()
    print("      Arm the check PER PEER, on first evidence that peer CAN emit. Never emitted →")
    print("      count it, say so once. HAS emitted, now magic=0 → that is a buffer that was")
    print("      never written, and it is caught. Same bytes; benign in one case, a hard fail in")
    print("      the other. ASK THE SENDER, NOT THE FRAME. So enforcement is a property of each")
    print("      node's FIRMWARE, not of the calendar — and it is read off the node's banner.")
    print()
    any_corrupt = False
    for n in lab.nodes:
        lines = beats.corrupt.get(n.name, [])
        if not lines:
            continue
        any_corrupt = True
        armed = n.name in enforcing
        # WHICH PEERS does this node reject, and how often? Printing the first five lines
        # tells you a node is unhappy; it does not tell you WHO it is unhappy WITH — and on a
        # mixed segment that is the entire diagnosis. A histogram by peer ethertype turns
        # "there is corruption" into an INTEROP MATRIX: it names the pair that disagrees.
        by_peer: dict[str, int] = {}
        for ln_ in lines:
            et = _peer_et(ln_)
            by_peer[et] = by_peer.get(et, 0) + 1
        who = ", ".join(f"{et}×{c}" for et, c in sorted(by_peer.items()))
        print(f"  {'❌' if armed else '⚠️ '} {n.name}: REJECTS {who}")
        for ln_ in lines[:2]:
            print(f"           {ln_}")
        if armed:
            fails.append(f"{n.name}: {len(lines)} CORRUPT frame report(s) from a SELF-ARMING node")
            print(f"           SCORED. This node only condemns a peer that has PROVEN it can emit")
            print(f"           a valid body — so this is not an un-upgraded peer. It is a bad frame.")
        else:
            inconclusive.append(f"{n.name}: {len(lines)} CORRUPT report(s) from a node that does "
                                f"NOT self-arm — unscoreable")
            print(f"           NOT SCORED — this node enforces unconditionally, so it cannot tell")
            print(f"           an un-upgraded peer from a corrupt frame. A red we cannot trust is")
            print(f"           worse than no red: it gets the check deleted by the people it protects.")
    if not any_corrupt:
        armed_names = sorted(enforcing)
        if armed_names:
            print(f"  ✅ none reported, and {len(armed_names)} node(s) were ACTUALLY LOOKING: "
                  f"{', '.join(armed_names)}")
            print(f"     These assert the per-sender sequence STRICTLY INCREASES — the only check")
            print(f"     that can see a stale buffer. A dropped frame leaves the descriptor pointing")
            print(f"     at a PREVIOUSLY VALID frame, so magic, pattern and self-consistent ethertype")
            print(f"     ALL PASS. EVERY STALE FRAME IS A GOOD FRAME. The question was never 'is this")
            print(f"     valid' — it was 'is this NEW'. rt1180: 26 replays caught, 0 well-formedness")
            print(f"     failures. 91: 40 and 0. mcxn: 490 and 0.")
        else:
            print("  ⚠️  none reported — AND NOBODY WAS LOOKING. Not one node on this segment")
            print("      self-arms, so ABSENCE OF 'CORRUPT' IS NOT EVIDENCE OF INTEGRITY. It is")
            print("      evidence that nobody is asking.")
            inconclusive.append("no node on the segment enforces the body — integrity unasserted")

    # ── 4) THE DEPARTURE WAS SURVIVED ────────────────────────────────────────────────────
    print("\n" + "=" * 78)
    print("4) ⭐ THE DEPARTURE WAS SURVIVED — the survivors' heartbeat GAP brackets it and")
    print("      RESUMES on the rejoin. This is the ONLY assertion that outlives the departure;")
    print("      every PASS token is otherwise earned before it.")
    print("=" * 78)

    # ④ A KILL THAT REACHES THE WRAPPER AND NOT THE PROCESS IS NOT A KILL. mcxn947 scored five
    # departures that never happened because SIGKILL hit `timeout`, not QEMU. We depart over QMP,
    # which is a different mechanism — and that is EXACTLY why it must still be checked, because
    # a mechanism you trust is a mechanism you stopped verifying. An un-killable peer is
    # INCONCLUSIVE, never a failure.
    dead = None
    if d_at is not None:
        old_sid = None
        hist = running.node_session_history.get(dep.name) or []
        if hist:
            old_sid = hist[0]
        dead = _is_really_gone(mgr, old_sid)
        if dead is True:
            print(f"  ✅ {dep.name} VERIFIED DEAD at the departure (its QEMU process is gone).")
            print(f"     Not assumed: a departure that did not happen would score as a wire that")
            print(f"     never noticed, which is the same green as a wire that survived.")
        elif dead is False:
            print(f"  ⚠️  {dep.name} WAS NOT ACTUALLY GONE — the QMP quit did not take. Everything")
            print(f"     below would be scoring a departure THAT NEVER HAPPENED.")
            inconclusive.append(f"{dep.name} never actually died — the departure is unverified")
        else:
            print(f"  ⚠️  could not verify {dep.name} was dead (no session handle)")

    if d_at is None:
        inconclusive.append("the scheduled departure never fired")
        print("  ⚠️  INCONCLUSIVE: the departure never fired")
    elif r_at is None:
        inconclusive.append(f"{dep.name} never rejoined — recovery cannot be asserted")
        print(f"  ⚠️  INCONCLUSIVE: {dep.name} never came back; a stopped heartbeat proves the")
        print("      wire NOTICED the loss, never that it SURVIVED it")
    elif dead is False:
        pass    # already recorded; scoring a non-departure is worse than not scoring
    else:
        for s in survivors:
            if s in unscoreable:
                print(f"  ⚠️  {s:7} SKIPPED — §0 could not honestly score this node.")
                continue
            bs = beats.beats.get(s, [])
            # A LATCHED node — one that prints PASS once and stops caring — has NO ORACLE at
            # departure time: its last beat lands long before the wire ever got interesting.
            # ⭐ AN UNSCOREABLE NODE IS *INCONCLUSIVE*, NEVER A *FAILURE*. Calling it FAIL would
            #   conflate "I cannot see" with "it is broken" — the same collapsed oracle this lab
            #   exists to avoid, and the first version of this scorer did exactly that. A lab that
            #   scores an unscoreable node RED gets "fixed" by someone silencing the node.
            latched = not bs or max(bs) < d_at - BEAT_TIMEOUT_S
            if latched:
                print(f"  ⚠️  {s:7} LATCHED — {len(bs)} beat(s), last at "
                      f"{_fmt(max(bs) if bs else 0)}, long before the departure.")
                print(f"           A latched assertion prints PASS once and shows NOTHING here.")
                print(f"           NOT A FAILURE — an unscoreable node is INCONCLUSIVE. Ask its")
                print(f"           owner to RE-ARM (clear the seen-flags after PASS, like mcx).")
                inconclusive.append(f"{s} is LATCHED — it cannot witness a departure")
                continue
            g = beats.gap_around(s, d_at, r_at)
            if g is None:
                print(f"  ❌ {s:7} was beating up to the departure and NEVER RESUMED "
                      f"(last beat {_fmt(max(bs))}) — the wire did not recover for it")
                fails.append(f"{s}: heartbeat never resumed after the rejoin")
                continue
            last_before, first_after = g
            gap = first_after - last_before
            window = r_at - d_at
            ok = (last_before <= d_at + BEAT_TIMEOUT_S
                  and first_after <= r_at + BEAT_TIMEOUT_S
                  and gap >= window * 0.5)
            print(f"  {'✅' if ok else '❌'} {s:7} last beat {_fmt(last_before)} → first beat "
                  f"{_fmt(first_after)}   GAP {gap:5.1f}s  (departure window {window:.0f}s)")
            if ok:
                print(f"           → went silent when {dep.name} left, RESUMED when it returned:")
                print(f"             the wire absorbed the loss AND recovered.")
            else:
                fails.append(f"{s}: heartbeat gap {gap:.1f}s does not bracket the "
                             f"{window:.0f}s departure window")

    # ── 5) THE POST-DEPARTURE JOINER ─────────────────────────────────────────────────────
    if joiners:
        print("\n" + "=" * 78)
        print("5) ⭐ A NODE THAT WAS NOT THERE FOR THE DEPARTURE JOINED AFTERWARDS — AND SAW.")
        print("      A survivor only shows the segment still works FOR SOMEONE ALREADY ON IT:")
        print("      its ring is programmed, its peers are known, its descriptors are armed, and")
        print("      a departure re-tests none of that. This node had NONE of it. It is the only")
        print("      oracle in the lab that CANNOT BE PRE-SATISFIED — it did not exist when the")
        print("      wire was whole.")
        print("=" * 78)
        for j in joiners:
            bs = beats.beats.get(j.name, [])
            arrived = running.node_arrivals.get(j.name)
            if not bs:
                print(f"  ❌ {j.name:7} joined at {_fmt(arrived or 0)} (after the t+{dep.stop_at:.0f} "
                      f"departure) and NEVER SAW ITS PEERS.")
                print(f"           The post-departure segment did not carry a new arrival.")
                fails.append(f"{j.name}: joined after the departure and never passed")
                continue
            first = min(bs)
            print(f"  ✅ {j.name:7} joined {_fmt(arrived or 0)} — AFTER the t+{dep.stop_at:.0f} "
                  f"departure — and PASSED at {_fmt(first)} ({len(bs)} beats)")
            print(f"           The segment still works for somebody who was NOT ON IT when the")
            print(f"           member was lost. That is the assertion this lab was missing.")
        print()
        print("  ⚠️  HONEST LIMIT, STATED BEFORE THE GREEN IS READ: this node SEES the survivors.")
        print("      They CANNOT SEE IT — mcx/rt1180/imx95 each compile a fixed peer list of")
        print("      0x88B5/0x88B6/0x88B7 and 0x88B8 is in NONE of them. They are STRUCTURALLY")
        print("      BLIND to it. So this proves the post-departure segment CARRIES a new joiner,")
        print("      NOT that the fleet NOTICED one. The absence of a complaint from a node that")
        print("      CANNOT complain is not evidence. (0x88B8 → three peer lists: asked, not assumed.)")

    # ── 6) NO UNSCHEDULED GAPS — the assertion only the coordinator can make ─────────────
    if dep_window:
        print("\n" + "=" * 78)
        print("6) ⭐ NO GAP WE DID NOT ORDER. mcxn947 found that on a NODE, a stalled ring and a")
        print("      departed peer produce the identical signal — and called that the honest")
        print("      answer, because from the segment's point of view they ARE the same event.")
        print("      Right for a node. Wrong for the COORDINATOR: WE SCHEDULED THE DEPARTURE.")
        print("      A gap bracketing t+{:.0f} is the one we asked for. A gap ANYWHERE ELSE is a"
              .format(dep.stop_at))
        print("      wire fault that no node on this segment is in a position to name.")
        print("=" * 78)
        for n in lab.nodes:
            if n.name in unscoreable:
                continue
            gaps = beats.unscheduled_gaps(n.name, dep_window, BEAT_TIMEOUT_S)
            if not gaps:
                print(f"  ✅ {n.name:7} no unexplained silence")
            for a, b in gaps[:3]:
                print(f"  ❌ {n.name:7} went quiet {_fmt(a)} → {_fmt(b)} ({b - a:.1f}s) — "
                      f"NOTHING WAS SCHEDULED THERE")
                fails.append(f"{n.name}: unscheduled {b - a:.1f}s heartbeat gap at {_fmt(a)}")

    # ── RESULT ───────────────────────────────────────────────────────────────────────────
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
        print("RESULT: PASS")
        print("  · every node saw its peers on a staggered segment;")
        print("  · the survivors' heartbeat went silent for the departure and RESUMED on the")
        print("    rejoin — the departure was not merely witnessed, it was SURVIVED;")
        print("  · a node that was NOT THERE for the departure joined afterwards and saw;")
        print("  · every self-arming node was looking at the frame BODY, and found none corrupt;")
        print("  · and nothing went quiet that we did not ask to go quiet.")
    print("=" * 78)

    print("\nstopping lab ...")
    await coord.stop(lab.id)
    print("done.")
    return 1 if fails else (2 if inconclusive else 0)


def _is_really_gone(mgr: SessionManager, sid: str | None) -> bool | None:
    """Did the departed node's QEMU process ACTUALLY exit? None = cannot tell."""
    if not sid:
        return None
    try:
        sess = mgr.get(sid)
    except Exception:
        return True         # the session is gone from the manager entirely
    proc = getattr(sess, "_proc", None)
    if proc is None:
        return None
    return proc.returncode is not None


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

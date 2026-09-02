#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""l2beacon — the fleet's v2 raw-L2 beacon, for a REAL BOARD.

This is the silicon-side half of the emulated-meets-real lab. It speaks the exact
same wire contract as the fleet's `enet-lab3` firmware, so a physical i.MX 95 FRDM
(or a Jetson AGX Orin, or anything with an AF_PACKET socket and python3) can stand
on a raw-L2 segment as a first-class peer of an emulated board.

    usage:  l2beacon.py <ifname> <my_ethertype> [peer_ethertype ...]
            l2beacon.py --idle-control <ifname> <secs>

────────────────────────────────────────────────────────────────────────────────
WHY THIS FILE EXISTS AT ALL, AND WHY IT IS NOT `l2probe.py`

The lab was staged with a probe (`/tmp/l2probe.py`) that proved the *fabric*: it
sent 14 frames of a custom ethertype from the real FRDM and skippy's host-side
sniffer received all 14. That is a real result and this file does not replace it —
it proved the LAN carries non-IP ethertypes, which is the one fabric property the
lab needs.

But that probe cannot be the lab's board-side node, and the reason is not style.
MEASURED against 95emulator's `tests/enet-lab3/enet-lab3.c` (read, not recalled):

    l2probe sends  b"\\xff"*6 + smac + ethertype + body.ljust(46, b"\\x00")
                   = 6 + 6 + 2 + 46 = 60 BYTES.

    enet-lab3.c:541   if (n != FRAME_LEN) bad = BAD_SHORT;      FRAME_LEN == 64

60 != 64, so every emulated node on the segment CONDEMNS it — not "ignores", not
"counts it as legacy": condemns it as a broken beacon. And it fails three further
gates it never reaches: no BEACON_MAGIC at [14..17], no self-ethertype echo at
[18..19], and a 0x00 fill where the contract says 0x5A. A real board running the
staging probe would appear on the wire, be visibly received, and turn NOTHING
green — and the most likely reading of that in the room, at 4am, is "the model
can't see real silicon."

⭐ THE FAILURE MODE THIS FILE EXISTS TO PREVENT is not a dropped frame. It is a
  RED SEGMENT THAT GETS BLAMED ON THE EMULATOR. The wire would be perfect and the
  verdict would be wrong, and the wrong verdict points at the one thing the whole
  lab is trying to make a claim about.

────────────────────────────────────────────────────────────────────────────────
THE CONTRACT — transcribed from enet-lab3.c, byte offsets and all

  FRAME_LEN     64 EXACTLY (not ">= 64"; a 1000-byte frame with a valid 64-byte
                prefix is REJECTED — rt1180 shipped exactly that for 90 minutes)
  [0..5]        dst MAC   — broadcast ff:ff:ff:ff:ff:ff is fine on a raw segment
  [6..11]       src MAC   — the sending interface's own
  [12..13]      ethertype — MUST be in 0x88B5..0x88BF (BEACON_ET_LO..HI)
  [14..17]      MAGIC     0xB5 0xB6 0xB7 0xC0   (BEACON_MAGIC, big-endian)
  [18..19]      SELF_ET   this node's OWN ethertype, BE — must equal [12..13]
  [20..23]      SEQ       incrementing, big-endian
  [24..27]      INCARN    per-BOOT random nonce, constant for the run
  [28..63]      FILL      0x5A

  ⚠️ INCARN must never be 0 or 0x5A5A5A5A. 0x5A5A5A5A is INCARN_LEGACY — the
  sentinel a v1 node produces by filling those bytes with the pattern. A peer
  sending it is COUNTED as a sighting but declared "freshness UNVERIFIABLE", and
  enet-lab3.c:594 says the rest out loud: it "does NOT satisfy a required-peer
  gate — segment stays red". So the sentinel is not a cosmetic downgrade. It is
  the difference between a green run and a red one, silently.

────────────────────────────────────────────────────────────────────────────────
THE RX RULES ARE COPIED, NOT INVENTED — AND THAT IS THE POINT

Every validation below mirrors enet-lab3's. That is deliberate, and it is the one
design decision in this file worth defending, because the tempting alternative
(a lenient board-side sniffer that just counts ethertypes) is actively dangerous.
enet-lab3.c says why, in its own comment at line 528:

    "A RECEIVER THAT IS MORE PERMISSIVE THAN THE SEGMENT COUNTS PEERS THAT
     EVERYONE ELSE IS REJECTING — and then it is OUR green that is the lie,
     because ours is the only one that came back."

That warning lands with unusual force HERE. This board's console is the lab's
LOAD-BEARING ORACLE: the emulated node sits on a macvtap, where guest and host
frames can be locally switched, so the QEMU side's own PASS proves nothing about
the physical wire. The only evidence that a frame crossed a cable is THIS
program, on the far end of it, saying so. A permissive oracle at exactly that
position would be the most expensive bug in the lab.

So: identical length gate, identical magic gate, identical self-ethertype gate,
identical fill gate, identical incarnation/sequence semantics — including
declining to render a freshness verdict on a legacy peer rather than rendering a
false one.

────────────────────────────────────────────────────────────────────────────────
IT RE-ARMS. IT DOES NOT LATCH.

The fleet audited this across three trees and found two of three oracles latched
(`rt1180: saw_a/saw_b never cleared`, `imx95: passed = 1`), and mcx alone
re-armed. A latched node prints PASS once and is blind forever after — and a
SATISFIED assertion and an ABSENT one print the same thing: nothing. This node
clears its seen-flags after every PASS, so its PASS line is not a verdict but a
HEARTBEAT WITH THE WIRE IN THE LOOP. If the cable is pulled mid-run, the
heartbeat stops, and the stopping is visible.

────────────────────────────────────────────────────────────────────────────────
WHAT IT PRINTS, AND WHY THE SCORER CAN TRUST IT

  L2BEACON UP: if=<n> et=0x88b9 mac=... incarnation=0x...
  L2BEACON PASS #<n>: saw all required peers (0x88b7 rx=1234)
  L2BEACON CORRUPT: <why> ...              <- never counted as a sighting
  L2BEACON LEGACY: peer 0x.... has no incarnation ...
  L2BEACON STATS: rx_peer=<n> rx_self_ignored=<n> rx_foreign_ignored=<n> corrupt=<n>

Every count is an integer the scorer parses and requires to be POSITIVE. It never
scores on the absence of a failure string — that is the bug that made the lab's
own proof script print "paths proven: 2, failed: 0" while all three of its steps
had failed, because it grepped for a line that a never-run step never emitted.
"""
from __future__ import annotations

import os
import random
import socket
import struct
import sys
import time

ETH_P_ALL = 0x0003

# ── the contract (enet-lab3.c:99-121, 187-190) ────────────────────────────────
FRAME_LEN = 64
BEACON_MAGIC = 0xB5B6B7C0
MAGIC_OFF = 14
SELF_ET_OFF = 18
SEQ_OFF = 20
INCARN_OFF = 24
FILL_OFF = 28
FILL_BYTE = 0x5A
INCARN_LEGACY = 0x5A5A5A5A
BEACON_ET_LO = 0x88B5
BEACON_ET_HI = 0x88BF

SEND_EVERY_MS = 200
QUIET_MS = 5000


def is_beacon_et(et: int) -> bool:
    return BEACON_ET_LO <= et <= BEACON_ET_HI


def _mac_bytes(iface: str) -> bytes:
    with open("/sys/class/net/%s/address" % iface) as fh:
        return bytes(int(x, 16) for x in fh.read().strip().split(":"))


def _mac_str(raw: bytes) -> str:
    return ":".join("%02x" % b for b in raw)


def _fresh_incarnation() -> int:
    """A per-BOOT nonce that is neither 0 nor the legacy sentinel.

    enet-lab3.c:368 rejects both on the send side for the same reason we do: 0 is
    indistinguishable from an uninitialised field, and 0x5A5A5A5A is the v1
    sentinel that costs a required-peer gate its green. Drawn from os.urandom so
    two boards booted in the same second cannot collide.
    """
    while True:
        n = struct.unpack("!I", os.urandom(4))[0]
        if n != 0 and n != INCARN_LEGACY:
            return n


def build_frame(src: bytes, my_et: int, seq: int, incarn: int) -> bytes:
    """One v2 beacon, exactly FRAME_LEN bytes. Asserted, not assumed — see below."""
    f = bytearray(FRAME_LEN)
    f[0:6] = b"\xff" * 6                       # dst: broadcast
    f[6:12] = src                              # src: our own
    struct.pack_into("!H", f, 12, my_et)       # ethertype
    struct.pack_into("!I", f, MAGIC_OFF, BEACON_MAGIC)
    struct.pack_into("!H", f, SELF_ET_OFF, my_et)   # the self-echo
    struct.pack_into("!I", f, SEQ_OFF, seq)
    struct.pack_into("!I", f, INCARN_OFF, incarn)
    for i in range(FILL_OFF, FRAME_LEN):
        f[i] = FILL_BYTE
    # A frame that is the wrong length is the single most likely way this whole
    # lab goes red for a non-wire reason, so it is checked here rather than
    # discovered on someone else's console.
    assert len(f) == FRAME_LEN, "frame is %d bytes, contract is %d" % (len(f), FRAME_LEN)
    return bytes(f)


# ── the RX oracle, as a PURE FUNCTION ────────────────────────────────────────
# This is the lab's load-bearing gate: the only thing that can say a frame
# crossed a physical cable. It is deliberately factored out of the socket loop
# so it can be exercised on hand-built frames with no root, no wire and no
# QEMU — because an oracle nobody can test is an oracle nobody has tested, and
# this one sits at exactly the position where a false green would be most
# expensive. `classify` never mutates peer state; the caller owns that.
#
# Verdicts, and they are NOT a severity ladder — they are different KINDS:
#   SELF     our own ethertype. Ignored. (rt1180's monitor once matched its own
#            banner and shouted PASS twelve times at an empty wire.)
#   FOREIGN  a stranger, or an in-block peer we were not asked about.
#            IGNORED, NOT CONDEMNED — traffic we did not ask for is not a fault.
#   CORRUPT  a frame that failed a structural gate. NEVER counted as a sighting.
#   LEGACY   valid body, no incarnation. COUNTED, but cannot satisfy a gate.
#   OK       a sighting.
V_SELF, V_FOREIGN, V_CORRUPT, V_LEGACY, V_OK = (
    "SELF", "FOREIGN", "CORRUPT", "LEGACY", "OK")


def classify(buf: bytes, my_et: int, wanted: set) -> tuple:
    """Return (verdict, ethertype, seq, incarnation, why).

    Mirrors enet-lab3.c's RX path gate-for-gate and IN ITS ORDER. The order is
    not cosmetic: length is checked first because a truncated frame makes every
    later offset a read past the end.
    """
    if len(buf) < 14:
        return (V_FOREIGN, None, None, None, "runt, no ethertype")
    et = struct.unpack("!H", buf[12:14])[0]
    if et == my_et:
        return (V_SELF, et, None, None, None)
    if not is_beacon_et(et) or et not in wanted:
        return (V_FOREIGN, et, None, None, None)

    if len(buf) != FRAME_LEN:
        return (V_CORRUPT, et, None, None,
                "SHORT (len=%d, contract=%d)" % (len(buf), FRAME_LEN))
    magic = struct.unpack_from("!I", buf, MAGIC_OFF)[0]
    if magic != BEACON_MAGIC:
        return (V_CORRUPT, et, None, None,
                "MAGIC (0x%08x != 0x%08x)" % (magic, BEACON_MAGIC))
    self_et = struct.unpack_from("!H", buf, SELF_ET_OFF)[0]
    if self_et != et:
        # The frame contradicts ITSELF — the signature of a stale or clobbered
        # buffer, which is exactly what a DMA writeback bug produces. This gate
        # is why "I saw an ethertype" became "I saw a frame".
        return (V_CORRUPT, et, None, None,
                "SELF_ET (body says 0x%04x, header says 0x%04x — the frame "
                "contradicts itself)" % (self_et, et))
    for i in range(FILL_OFF, FRAME_LEN):
        if buf[i] != FILL_BYTE:
            return (V_CORRUPT, et, None, None,
                    "PATTERN (byte %d = 0x%02x, expected 0x%02x)"
                    % (i, buf[i], FILL_BYTE))

    seq = struct.unpack_from("!I", buf, SEQ_OFF)[0]
    incarn = struct.unpack_from("!I", buf, INCARN_OFF)[0]
    if incarn == INCARN_LEGACY:
        return (V_LEGACY, et, seq, incarn, "v1 body, no incarnation")
    return (V_OK, et, seq, incarn, None)


def idle_control(iface: str, secs: float) -> int:
    """Sniff with nobody sending. This is what turns a green into evidence.

    Both of the lab's proven paths are only meaningful because the same receiver
    saw ZERO with nobody transmitting. A receiver that cannot tell silence from
    traffic has not been shown to be a receiver at all — it may be reporting a
    constant. Returns the count so the caller can require it to be zero.
    """
    s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_ALL))
    s.bind((iface, 0))
    s.settimeout(0.5)
    end = time.time() + secs
    n = 0
    while time.time() < end:
        try:
            f = s.recv(2048)
        except socket.timeout:
            continue
        if len(f) >= 14 and is_beacon_et(struct.unpack("!H", f[12:14])[0]):
            n += 1
    print("L2BEACON IDLE-CONTROL: %d beacon-block frames in %.0fs on %s"
          % (n, secs, iface), flush=True)
    if n == 0:
        print("L2BEACON IDLE-CONTROL PASS: the wire is quiet and this receiver "
              "can tell silence from traffic", flush=True)
    else:
        print("L2BEACON IDLE-CONTROL FAIL: %d frames with nobody sending — a PASS "
              "on this interface is MEANINGLESS until this is explained "
              "(stale node? another lab? loopback?)" % n, flush=True)
    return n


def run(iface: str, my_et: int, peers: list[int], runtime: float | None) -> int:
    if not is_beacon_et(my_et):
        print("L2BEACON ABORTED, NO VERDICT GIVEN: my ethertype 0x%04x is outside "
              "the beacon block 0x%04x..0x%04x. Every enet-lab3 node on the segment "
              "would silently IGNORE it (enet-lab3.c:511) and the peer that needs to "
              "see us could never turn green. Pick an in-block ethertype."
              % (my_et, BEACON_ET_LO, BEACON_ET_HI), flush=True)
        return 2
    for p in peers:
        if not is_beacon_et(p):
            print("L2BEACON ABORTED, NO VERDICT GIVEN: required peer 0x%04x is "
                  "outside the beacon block — this node could never see it, so the "
                  "gate would be structurally unsatisfiable." % p, flush=True)
            return 2

    src = _mac_bytes(iface)
    incarn = _fresh_incarnation()

    tx = socket.socket(socket.AF_PACKET, socket.SOCK_RAW)
    tx.bind((iface, 0))
    rx = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_ALL))
    rx.bind((iface, 0))
    rx.settimeout(SEND_EVERY_MS / 1000.0)

    # ⭐ THE BANNER STATES THE CAP. A corpse is far easier to explain when its intended
    # lifetime is on the record: the run's own log then says how long this process meant to
    # live, so anyone finding it later can tell "still within its cap" from "should have
    # been gone days ago" without guessing. 95emulator's suggestion.
    print("L2BEACON RUNTIME: %s" % ("unlimited (explicit --runtime 0)" if runtime is None
                                    else "%.0fs cap" % runtime), flush=True)
    print("L2BEACON UP: if=%s et=0x%04x mac=%s incarnation=0x%08x need=%s"
          % (iface, my_et, _mac_str(src), incarn,
             ",".join("0x%04x" % p for p in peers) or "(none — broadcast only)"),
          flush=True)

    seq = 0
    passes = 0
    rx_peer = 0
    rx_self = 0
    rx_foreign = 0
    corrupt = 0
    # Per-peer state, mirroring enet-lab3's slot arrays.
    seen: dict[int, bool] = {p: False for p in peers}
    wanted = set(peers)
    count: dict[int, int] = {p: 0 for p in peers}
    last_incarn: dict[int, int] = {}
    last_seq: dict[int, int] = {}
    legacy_warned: dict[int, bool] = {}
    last_tx = 0.0
    started = time.time()

    while True:
        now = time.time()
        if runtime is not None and now - started >= runtime:
            break

        if (now - last_tx) * 1000.0 >= SEND_EVERY_MS:
            seq = (seq + 1) & 0xFFFFFFFF
            tx.send(build_frame(src, my_et, seq, incarn))
            last_tx = now

        try:
            buf = rx.recv(2048)
        except socket.timeout:
            continue

        verdict, et, seq_rx, incarn_rx, why = classify(buf, my_et, wanted)

        if verdict == V_SELF:
            # A monitor that matches its own traffic is the trap rt1180 fell
            # into — its own `need 0x88b7` banner matched its own grep and it
            # shouted PASS twelve times at an EMPTY WIRE.
            rx_self += 1
            continue
        if verdict == V_FOREIGN:
            # IGNORED, NOT CONDEMNED. A stranger on the wire is not a fault.
            rx_foreign += 1
            continue
        if verdict == V_CORRUPT:
            corrupt += 1
            print("L2BEACON CORRUPT: peer 0x%04x %s — NOT counted as a sighting"
                  % (et, why), flush=True)
            continue
        if verdict == V_LEGACY:
            # Counted, but its freshness is unverifiable, so it must NOT satisfy
            # a required-peer gate. Declining to render a verdict beats rendering
            # a false one: the seq rule once fired 8,982 times at an honest peer.
            if not legacy_warned.get(et):
                print("L2BEACON LEGACY: peer 0x%04x has no incarnation (v1 body); "
                      "freshness UNVERIFIABLE, so it does NOT satisfy a "
                      "required-peer gate — this node stays red until that peer "
                      "carries a real incarnation" % et, flush=True)
                legacy_warned[et] = True
            rx_peer += 1
            count[et] += 1
            continue

        prev_i = last_incarn.get(et)
        prev_s = last_seq.get(et)
        if prev_i is not None and prev_i != INCARN_LEGACY and prev_i == incarn_rx \
                and prev_s is not None and seq_rx <= prev_s:
            # SAME incarnation and the sequence did not advance: the peer never
            # restarted, so this can only be our RX path handing us a stale
            # buffer. A NEW incarnation with a reset sequence is a REBOOT and is
            # re-baselined below — condemning that would be condemning a healthy
            # node for being restarted, which is what a board farm DOES.
            corrupt += 1
            print("L2BEACON CORRUPT: peer 0x%04x REPLAY (incarnation 0x%08x "
                  "unchanged, seq %u did not advance past %u) — NOT counted"
                  % (et, incarn_rx, seq_rx, prev_s), flush=True)
            continue

        if prev_i is not None and prev_i != incarn_rx:
            print("L2BEACON REBOOT: peer 0x%04x new incarnation 0x%08x (was "
                  "0x%08x) — re-baselining, not condemning"
                  % (et, incarn_rx, prev_i), flush=True)

        last_incarn[et] = incarn_rx
        last_seq[et] = seq_rx
        rx_peer += 1
        count[et] += 1
        seen[et] = True

        if peers and all(seen.values()):
            passes += 1
            print("L2BEACON PASS #%d: saw all required peers (%s)"
                  % (passes, " ".join("0x%04x rx=%d" % (p, count[p]) for p in peers)),
                  flush=True)
            # RE-ARM. The PASS is a heartbeat with the wire in the loop, not a
            # one-shot verdict — a latched oracle stops looking, and the moment
            # it stops is invisible.
            for p in seen:
                seen[p] = False

    print("L2BEACON STATS: rx_peer=%d rx_self_ignored=%d rx_foreign_ignored=%d "
          "corrupt=%d passes=%d tx=%d"
          % (rx_peer, rx_self, rx_foreign, corrupt, passes, seq), flush=True)
    if peers and passes == 0:
        # An honest negative. A run that saw nothing is a RESULT, and saying so
        # plainly is what keeps the green ones worth anything.
        print("L2BEACON NO-PASS: required peers were never all seen in one window "
              "(%s). This is a reportable negative, not an error."
              % " ".join("0x%04x rx=%d" % (p, count[p]) for p in peers), flush=True)
    return 0 if (not peers or passes > 0) else 1


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[1] == "--idle-control":
        if len(argv) != 4:
            print("usage: %s --idle-control <ifname> <secs>" % argv[0], file=sys.stderr)
            return 2
        return 0 if idle_control(argv[2], float(argv[3])) == 0 else 1

    # ⭐ BOUNDED BY DEFAULT, UNBOUNDED ONLY ON PURPOSE (2026-09-02).
    # This used to default to None — run FOREVER — and one beacon started on 2026-08-25 was
    # still transmitting eight days later. It sat on the Orin's wire through a later run and
    # made that leg's counts unattributable. An eight-day process was not a leak or a wedge;
    # it was the documented behaviour of the default path, which is worse.
    #
    # ⚠️ AND A BARE DEFAULT TTL TRADES ONE SILENT FAILURE FOR ANOTHER (95emulator's caution,
    # which is why this is not just `runtime = 3600`). A cap that expires mid-run makes the
    # node go quiet, and A NODE THAT QUIETLY WENT QUIET LOOKS IDENTICAL TO ONE THAT NEVER
    # STARTED — the exact ambiguity the vantage rule exists to prevent. So the default is
    # generous enough that no real lab reaches it (a lab run is ~2 min), and unbounded stays
    # available as something a person TYPED and can be found in a process list, rather than
    # the default nobody chose.
    DEFAULT_RUNTIME_S = 3600.0
    runtime = DEFAULT_RUNTIME_S
    args = list(argv[1:])
    for i, a in enumerate(args):
        if a == "--runtime":
            v = float(args[i + 1])
            runtime = None if v == 0 else v      # 0 = unlimited, EXPLICITLY
            del args[i:i + 2]
            break

    if len(args) < 2:
        print("usage: %s [--runtime SECS] <ifname> <my_ethertype> [peer_ethertype ...]\n"
              "       (--runtime defaults to %.0fs; 0 means unlimited and must be typed)\n"
              "       %s --idle-control <ifname> <secs>"
              % (argv[0], DEFAULT_RUNTIME_S, argv[0]), file=sys.stderr)
        return 2

    iface = args[0]
    my_et = int(args[1], 0)
    peers = [int(a, 0) for a in args[2:]]
    try:
        return run(iface, my_et, peers, runtime)
    except PermissionError:
        print("L2BEACON ABORTED, NO VERDICT GIVEN: AF_PACKET needs root. Run under "
              "sudo. (Refusing to print a verdict from a run that could not open a "
              "socket — a setup that cannot work must never reach the scoring code.)",
              file=sys.stderr)
        return 2
    except FileNotFoundError:
        print("L2BEACON ABORTED, NO VERDICT GIVEN: no interface '%s' on this host."
              % iface, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nL2BEACON INTERRUPTED — no verdict. A killed run is not a caught bug.",
              flush=True)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))

#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-or-later
#
# prove-leg1-emit.sh — does the EMULATED ENETC on PF 0002:00:08.0 emit conformant
#                      v2 frames? A DELIBERATE PARTIAL. It is not leg1.
#
# ═════════════════════════════════════════════════════════════════════════════
# ⚠️⚠️ READ THIS BEFORE QUOTING ANY RESULT FROM THIS SCRIPT ⚠️⚠️
#
# THIS SCRIPT CANNOT PRODUCE A PASS. Its best possible outcome is
# **LEG1-EMIT-ONLY**, and that is not a weaker way of saying PASS — it is a
# statement about a different, smaller claim.
#
# Leg1 end-to-end is three things joined:
#     (a) the model EMITS conformant frames on PF 0002:00:08.0   <- this script
#     (b) the USB link CARRIES raw non-IP L2 both ways           <- proven, standalone, 39/40
#     (c) the two JOIN: what the model emits is what the Orin receives
#
#   ⭐ (a) AND (b) IS NOT (a-THEN-b). Composing two separately-proven halves is not
#     observing the whole. That substitution — inferring a joined result from its
#     parts — is the same error as inferring leg1 from leg0, one level finer, and
#     this lab exists partly to not make it. 95emulator proposed this partial and
#     said so themselves before saying what it was worth.
#
# So this narrows the unknown from THREE to ONE. It does not close it.
#
# ═════════════════════════════════════════════════════════════════════════════
# THE MECHANISM, AND THE TRAP IT WALKS INTO ON PURPOSE
#
# A SECOND macvtap child on the same lower device, in bridge mode, read by the
# host. The guest beacons on leg1; the sibling receives.
#
# ⚠️ MACVLAN SIBLINGS IN BRIDGE MODE ARE LOCALLY SWITCHED. A frame arriving at the
# sibling proves it left the emulated ENETC and reached the macvlan layer of a real
# NIC. IT DOES NOT PROVE IT WENT OUT THE PHYSICAL CABLE. That is exactly the
# local-switch trap flagged for criterion C — and this script walks into it
# knowingly, because the thing being narrowed is (a), which lives entirely on this
# side of the cable.
#
# ⭐ SO WE ADD A SECOND, INDEPENDENT OBSERVATION the proposal did not have: a
#   capture on the LOWER DEVICE itself, concurrently. We already MEASURED that a
#   guest's transmit direction is visible there (criterion C, 15 frames on
#   enx42b8036560ca), so if the lower device sees these frames too, the claim
#   strengthens from "reached the macvlan layer" to "reached the NIC's transmit
#   path". Still not "electrons on the far end of the cable" — that is (c), and
#   only the Orin can witness it.
#
# WHAT MAKES THIS WORTH RUNNING AT ALL: nobody has ever validated the BODY of a
# frame emitted on PF 0002:00:08.0. 95emulator's smoke test showed both legs come
# up and beacon; that is "it transmits", not "it transmits a conformant v2 frame".
# If the second ENETC PF mangles a body where the first does not, this finds it now
# rather than after a credential lands — and it would be found by a receiver that
# enforces exactly what the segment enforces.
#
#   usage:  sudo bash tools/prove-leg1-emit.sh
#
set -uo pipefail

LOWER="${LOWER:-enx42b8036560ca}"     # the USB gadget link's host NIC
SIBLING="hb-leg1-sib"
GUEST_ET="${GUEST_ET:-0x88B7}"
SECS="${SECS:-25}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_AS="${SUDO_USER:-}"
as_user() { if [ -n "$RUN_AS" ]; then sudo -u "$RUN_AS" -H "$@"; else "$@"; fi; }

say() { echo "$*"; }
abort() { echo; echo "🛑 ABORTED — NO VERDICT GIVEN: $*"; exit 2; }

# Transcript BEFORE the gate: an aborted run is exactly the one you want a
# record of. Fixed in prove-macvtap-guest.sh an hour ago and reintroduced here —
# a lesson applied to one file is not a lesson applied.
RUNLOG_DIR="$REPO/scratchpad-consoles/runs"
as_user mkdir -p "$RUNLOG_DIR" 2>/dev/null || mkdir -p "$RUNLOG_DIR"
RUNLOG="$RUNLOG_DIR/leg1-emit-$(date +%Y%m%d-%H%M%S).log"
as_user touch "$RUNLOG" 2>/dev/null || touch "$RUNLOG"
exec > >(tee -a "$RUNLOG") 2>&1
echo "📝 transcript: $RUNLOG"

[ "$(id -u)" -eq 0 ] || abort "needs root (macvtap creation)."
ip link show "$LOWER" >/dev/null 2>&1 || abort "no lower device '$LOWER'."


echo "══════════════════════════════════════════════════════════════════════"
echo " prove-leg1-emit — DOES PF 0002:00:08.0 EMIT CONFORMANT v2 FRAMES?"
echo "══════════════════════════════════════════════════════════════════════"
echo "  ⚠️  THIS SCRIPT CANNOT PRODUCE A PASS. Best case is LEG1-EMIT-ONLY."
echo "     It narrows leg1's unknown from THREE (emit/carry/join) to ONE (join)."
echo "     Only the real Orin can witness the join, and it is not involved here."
echo

cleanup() {
    echo
    echo "── CLEANUP (Law 2) ──────────────────────────────────────────────────"
    ip link show "$SIBLING" >/dev/null 2>&1 && ip link del "$SIBLING" && echo "  removed $SIBLING"
    pkill -f "score-real-silicon.py" 2>/dev/null
    echo "  CORPSE LIST: $(ip -br link show type macvtap 2>/dev/null | wc -l) macvtap dev(s) present"
    echo
    echo "📝 transcript: $RUNLOG"
}
trap cleanup EXIT INT TERM

# ── the sibling receiver ────────────────────────────────────────────────────
ip link show "$SIBLING" >/dev/null 2>&1 && ip link del "$SIBLING"
ip link add link "$LOWER" name "$SIBLING" type macvtap mode bridge || abort "cannot create sibling macvtap."
ip link set "$SIBLING" up
SIB_IDX="$(cat /sys/class/net/$SIBLING/ifindex)"
SIB_DEV="/dev/tap$SIB_IDX"
[ -c "$SIB_DEV" ] || abort "expected $SIB_DEV, absent."
chown "$(id -u ${RUN_AS:-root})":"$(id -g ${RUN_AS:-root})" "$SIB_DEV" 2>/dev/null || true
say "  sibling receiver: $SIBLING ifindex=$SIB_IDX dev=$SIB_DEV on $LOWER"

# ── idle control BEFORE anything transmits ──────────────────────────────────
say
say "── IDLE CONTROL — sibling sniffing with the lab not yet running ────────"
IDLE="$(as_user "$REPO/.venv/bin/python" - "$REPO" "$SIB_DEV" 3 <<'PYEOF'
import sys, time
sys.path.insert(0, sys.argv[1] + "/tools")
from tapio import Tap
n = 0
with Tap(sys.argv[2]) as t:
    end = time.time() + float(sys.argv[3])
    while time.time() < end:
        f = t.read_frame()
        if f and len(f) >= 14 and 0x88B5 <= int.from_bytes(f[12:14], "big") <= 0x88BF:
            n += 1
print(n)
PYEOF
)"
if [ "${IDLE:-1}" = "0" ]; then
    say "  ✅ 0 beacon frames with the lab down — this receiver can tell silence from traffic."
else
    say "  ⚠️  $IDLE beacon frames with the lab DOWN. Any count below is MEANINGLESS"
    say "     until that is explained (a stale node? another lab on this NIC?)."
fi

# ── launch the lab, capture on the LOWER DEVICE concurrently ────────────────
say
say "── LAUNCHING THE LAB (leg1 beacons on PF 0002:00:08.0) ─────────────────"
if command -v tcpdump >/dev/null 2>&1; then
    timeout $((SECS + 15)) tcpdump -i "$LOWER" -nn -c 50 "ether proto $GUEST_ET" \
        > /tmp/hb-leg1-lower.txt 2>/dev/null &
    LOWERCAP=$!
    say "  (concurrent capture on the LOWER DEVICE $LOWER — the second, independent observation)"
else
    LOWERCAP=""
    say "  ⚠️  no tcpdump; the lower-device observation will be UNAVAILABLE, not negative."
fi

as_user "$REPO/.venv/bin/python" "$REPO/tools/score-real-silicon.py" >/tmp/hb-leg1-lab.txt 2>&1 &
LABPID=$!

# ── read the sibling while the lab runs ─────────────────────────────────────
say
say "── SIBLING RECEIVER — enforcing the SEGMENT's own gates, not a lenient count ──"
as_user "$REPO/.venv/bin/python" - "$REPO" "$SIB_DEV" "$SECS" "$GUEST_ET" <<'PYEOF'
import sys, time, struct
sys.path.insert(0, sys.argv[1] + "/tools")
from tapio import Tap
dev, secs, want = sys.argv[2], float(sys.argv[3]), int(sys.argv[4], 0)
seen = good = 0; first = None; bad = {}
with Tap(dev) as t:
    end = time.time() + secs
    while time.time() < end:
        f = t.read_frame(0.5)
        if not f or len(f) < 14: continue
        if int.from_bytes(f[12:14], "big") != want: continue
        seen += 1
        if first is None: first = ":".join("%02x" % b for b in f[6:12])
        # The SEGMENT's gates. A lenient count here would make this partial worth
        # even less than it already is: "a frame arrived" is not "a conformant
        # frame arrived", and the entire point is the BODY.
        why = None
        if len(f) != 64: why = "len=%d" % len(f)
        elif struct.unpack_from("!I", f, 14)[0] != 0xB5B6B7C0: why = "magic"
        elif struct.unpack_from("!H", f, 18)[0] != want: why = "self-ET echo"
        elif set(f[28:64]) != {0x5A}: why = "fill"
        if why: bad[why] = bad.get(why, 0) + 1
        else: good += 1
print("SIBRESULT %d %d %s %s" % (seen, good, first or "-",
      ",".join("%s:%d" % kv for kv in bad.items()) or "none"))
PYEOF

wait $LABPID 2>/dev/null
[ -n "$LOWERCAP" ] && wait $LOWERCAP 2>/dev/null
LOWERN="$(grep -c . /tmp/hb-leg1-lower.txt 2>/dev/null | head -1)"; LOWERN="${LOWERN:-0}"

say
say "── LOWER-DEVICE OBSERVATION ($LOWER) ───────────────────────────────────"
say "  frames of $GUEST_ET seen at the NIC's tap point: $LOWERN"
say
say "══════════════════════════════════════════════════════════════════════"
say " VERDICT — and it is deliberately NOT on the PASS/FAIL scale"
say "══════════════════════════════════════════════════════════════════════"
say " Read the SIBRESULT line above: seen / conformant / src / rejections."
say
say " ⭐ IF conformant > 0:  LEG1-EMIT-ONLY."
say "    PROVEN:     the emulated ENETC on PF 0002:00:08.0 emits frames whose"
say "                BODY passes every gate the segment enforces (length, magic,"
say "                self-ethertype echo, 0x5A fill) — never before validated."
say "    NOT PROVEN: that any of them left the physical USB cable, and NOT"
say "                PROVEN that the Orin would receive them. Siblings are"
say "                LOCALLY SWITCHED; that is what this measurement is."
say "    The lower-device count above, if > 0, strengthens it to 'reached the"
say "    NIC transmit path' — still short of the far end."
say
say " ⚠️  DO NOT RECORD THIS AS 'leg1 works'. Leg1 end-to-end needs the Orin,"
say "    and the Orin is the only witness that local switching cannot fake."
say "══════════════════════════════════════════════════════════════════════"
exit 0

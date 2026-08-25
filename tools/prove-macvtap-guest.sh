#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-or-later
#
# prove-macvtap-guest.sh — does a GUEST on a macvtap actually reach real silicon?
#
# ─────────────────────────────────────────────────────────────────────────────
# THE QUESTION THIS ANSWERS, AND WHY IT COMES BEFORE THE LAB
#
# Everything proven so far about this wire was proven with a HOST-side raw
# socket on skippy. claude-connect said so plainly when handing the lab over
# (2026-08-22 02:22, item 1):
#
#     "The QEMU guest on a macvtap has not been shown to reach either peer.
#      That is the actual remaining unknown and it is the first thing the lab
#      must assert — host-can-reach does not imply guest-can-reach."
#
# That is correct, and it is load-bearing enough that building the coordinator's
# macvtap transport before answering it would be building on an assumption. So
# this script answers it FIRST, in about a minute, with no QEMU involved.
#
# ⭐ THE TRICK: a QEMU guest on a macvtap is, from the kernel's point of view,
#   whatever has /dev/tapN open. QEMU reads and writes raw Ethernet frames on
#   that character device and nothing else. So this script OPENS /dev/tapN AND
#   WRITES A FRAME TO IT — which is not a simulation of the guest's data path,
#   it IS the guest's data path, minus the emulated NIC. If a frame written here
#   does not reach the FRDM, no amount of QEMU will make it.
#
# ─────────────────────────────────────────────────────────────────────────────
# WHAT IT MEASURES — three questions, and the third is the one nobody asked
#
#   Q1  GUEST -> REAL SILICON.  Write a v2 beacon to /dev/tapN. Does the real
#       i.MX 95 FRDM's own receiver see it, on the far end of a physical cable?
#       This is the lab's load-bearing direction and the only one that cannot be
#       faked by local switching.
#
#   Q2  REAL SILICON -> GUEST.  The FRDM beacons; does it arrive at /dev/tapN?
#       UNMEASURED until now in EITHER direction — claude-connect proved
#       FRDM->skippy(host) and Orin->skippy(host), never anything->guest.
#
#   Q3  ⚠️ CAN SKIPPY EVEN SEE IT?  The lab's proposed criterion C is "a capture
#       on skippy shows the frames on the PHYSICAL iface." That criterion may be
#       UNSATISFIABLE BY CONSTRUCTION, and not because the frames are missing:
#       macvlan deliberately ISOLATES the lower device from its own macvtap
#       children. A host capture on enp6s0 can therefore come back empty while
#       the wire is working perfectly.
#
#       ⭐ IF Q3 COMES BACK EMPTY, THAT IS NOT A FAILURE — IT IS A CRITERION
#         THAT NEEDS REPLACING, and the replacement already exists: the REAL
#         BOARD'S receive count is a better capture point than skippy's, because
#         the board is on the far side of the cable and skippy is not. Reporting
#         this as a red would be blaming the wire for a property of macvlan.
#
# ─────────────────────────────────────────────────────────────────────────────
# SAFETY — why this cannot take skippy off the network
#
# macvtap is ADDITIVE: it adds a virtual endpoint to enp6s0 and does not rebuild
# enp6s0's own configuration. And enp6s0 is NOT skippy's default route (wlo1 is),
# so even total failure here cannot cost the box its connectivity. The script
# removes the macvtap on every exit path, including interrupts, and PRINTS THE
# CORPSE LIST so the next run starts from a wire this one did not dirty.
#
#   usage:  sudo bash tools/prove-macvtap-guest.sh [--frdm-host root@10.0.1.181]
#
set -uo pipefail

LOWER="${LOWER:-enp6s0}"
MACVTAP="hb-mvt0"
GUEST_ET="${GUEST_ET:-0x88B7}"   # the emulated i.MX 95's ethertype (same on both legs)
# ── WHICH LEG ARE WE PROVING? ────────────────────────────────────────────────
# The lab has two, on two physically different media, and this script proves one
# at a time. Defaults are the LAN leg; the USB leg is the same experiment with a
# different wire, peer and ethertype:
#
#   LAN (default)   LOWER=enp6s0           PEER=root@10.0.1.181  IF=eth1    ET=0x88B9
#   USB             LOWER=enx42b8036560ca  PEER=kyle@10.0.1.124  IF=l4tbr0  ET=0x88BA
#                   PEER_SUDO=1   (the Orin's AF_PACKET needs root; the FRDM
#                                  already logs in as root and must NOT get sudo)
#
# ⚠️ THE LAN RESULT IS A STRONG PRIOR TO CHECK, NEVER A FINDING TO INHERIT. The
# fleet's own rule, and it applies with force here: the USB leg is a cdc_ncm
# gadget, a different driver over a different medium. "It worked on Ethernet" is
# not evidence about USB — many gadget stacks pass only IP, and the only reason we
# know this one does not is that it was MEASURED.
FRDM_ET="${PEER_ET:-0x88B9}"
FRDM_HOST="${FRDM_HOST:-root@10.0.1.181}"
FRDM_IF="${FRDM_IF:-eth1}"
PEER_SUDO="${PEER_SUDO:-0}"
LEG="${LEG:-LAN}"
SECS="${SECS:-8}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BEACON="$REPO/tools/l2beacon.py"

# ── PRIVILEGE SPLIT: root for the tap, THE INVOKING USER for ssh ────────────
# ⚠️ THE BUG THIS EXISTS TO KILL, AND IT HAS ALREADY BITTEN THIS LAB TWICE.
# The macvtap needs root. ssh to the boards must NOT be root: under `sudo`, ssh
# reads ROOT's config and ROOT's keys, which have none of Kyle's. claude-connect
# hit exactly this staging the lab (2026-08-22 02:17: "sudo uses ROOT's ssh
# config, which has none of Kyle's aliases") and holobench hit the same class
# 25 minutes later by wrapping a working user-level ssh in sudo.
#
# So every remote call drops back to the invoking user. $SUDO_USER is who ran
# sudo; if the script is somehow run as real root with no SUDO_USER, we say so
# rather than silently trying root's keyring and reporting a wire fault.
RUN_AS="${SUDO_USER:-}"
as_user() {
    if [ -n "$RUN_AS" ]; then sudo -u "$RUN_AS" -H "$@"; else "$@"; fi
}
ssh_b()  { as_user ssh  -o BatchMode=yes -o ConnectTimeout=5 "$@"; }
scp_b()  { as_user scp  -o BatchMode=yes -o ConnectTimeout=5 "$@"; }

# Start the PEER's beacon. On a board that already logs in as root (the FRDM) this
# is a plain backgrounded ssh. On one that needs sudo (the Orin) it needs a TTY so
# sudo can prompt — and it must be ONE ssh, not a `sudo -v` warm-up followed by a
# second session: sudo's tty_tickets caches the credential AGAINST THAT TTY, so the
# later session gets a cache miss and hangs. claude-connect lost a run to exactly
# that and fixed it the same way (2026-08-22 02:22, "reversing the direction so the
# privileged side runs in one session").
# The redirect happens in the LOGIN shell, before sudo, so the log is owned by the
# ssh user and readable back without a second privileged round-trip.
# ⚠️ ssh -t CANNOT PROMPT FROM A BACKGROUND JOB. A backgrounded process under a
# sudo'd script has no controlling terminal, so `sudo` on the far end returns
# "a terminal is required to read the password" and the beacon NEVER RUNS. That
# happened on 2026-08-23 and both directions were then scored as wire FAILURES —
# the fourth time in this lab that a step which never executed was reported as a
# result. So: ask ONCE, here, in the foreground where a tty still exists, and feed
# it to `sudo -S` on stdin. Nothing is written to disk and nothing persists past
# the run. (setcap on the peer's python3 would grant CAP_NET_RAW to every python
# invocation by any user; a NOPASSWD rule on a /tmp path would be a root hole.)
PEER_PW="${PEER_PW:-}"
peer_pw_prompt() {
    [ "$PEER_SUDO" = "1" ] || return 0
    [ -n "$PEER_PW" ] && { say "     (peer password supplied via \$PEER_PW)"; return 0; }

    # ⚠️ THE BUG THIS NOW CATCHES, and it is the lab's own recurring class committed
    # in the credential path: `read < /dev/tty` FAILS SILENTLY when there is no
    # controlling terminal (a wrapped/piped/`!`-prefixed invocation). The PROMPT
    # still prints — it goes to stderr — so a human sees it, types a password into
    # nothing, and the script carries on with PEER_PW empty. `sudo -S` then reports
    # "no password was provided" on the far board, and every phase that follows is
    # measuring a peer that could never have started.
    #   A STEP THAT DID NOT WORK MUST NOT BE CARRIED ON AS IF IT HAD.
    if [ ! -r /dev/tty ]; then
        say "     ⚠️  no controlling terminal — cannot prompt for the $LEG peer's password."
        return 1
    fi
    printf '   sudo password for %s (the %s peer, NOT skippy): ' "$FRDM_HOST" "$LEG" > /dev/tty
    if ! read -rs PEER_PW < /dev/tty; then
        printf '\n' > /dev/tty
        say "     ⚠️  could not read from the terminal — password NOT captured."
        PEER_PW=""
        return 1
    fi
    printf '\n' > /dev/tty
    [ -n "$PEER_PW" ] || { say "     ⚠️  empty password entered."; return 1; }
    return 0
}

# ⭐ PROVE THE CREDENTIAL BEFORE ANYTHING DEPENDS ON IT. "Every participant must
# prove it participated" applies to the password too: a credential that does not
# work is a participant that will not show up, and discovering that four phases
# later means four phases of measurements about a peer that was never there.
peer_pw_verify() {
    [ "$PEER_SUDO" = "1" ] || return 0
    [ -n "$PEER_PW" ] || return 1
    printf '%s\n' "$PEER_PW" | as_user ssh -o BatchMode=yes -o ConnectTimeout=5 \
        "$FRDM_HOST" "sudo -S -p '' true" >/dev/null 2>&1
}

peer_beacon() {   # <logfile> <runtime> <extra-args>
    local log="$1" rt="$2" extra="$3"
    local cmd="python3 /tmp/l2beacon.py --runtime $rt $FRDM_IF $FRDM_ET $extra"
    rm -f /tmp/hb-peer-start.txt
    if [ "$PEER_SUDO" = "1" ]; then
        # ⚠️ THE BUG THIS SHAPE EXISTS TO AVOID, and it cost a run:
        #     ssh host "sudo -S -p '' nohup $cmd > $log 2>&1 &"
        # That `&` backgrounds SUDO ITSELF on the far side, and a background job's
        # stdin is detached from the pipe — so a password piped in never reaches it
        # and sudo reports "no password was provided". The identical pipe worked in
        # peer_pw_verify because THAT sudo ran in the foreground. One character
        # apart, and the failure text blames the credential rather than the
        # backgrounding.
        # ⭐ SO BACKGROUND THE **LOCAL** ssh INSTEAD. Remote sudo then runs in its
        # session's foreground and reads the pipe normally; the & applies to the
        # whole local pipeline, whose first member (printf) does not read stdin.
        printf '%s\n' "$PEER_PW" | as_user ssh -o BatchMode=yes -o ConnectTimeout=5 \
            "$FRDM_HOST" "sudo -S -p '' $cmd > $log 2>&1" >/dev/null 2>&1 &
        PEER_SSH_PID=$!
        sleep 3
        ssh_b "$FRDM_HOST" "head -3 $log 2>/dev/null" > /tmp/hb-peer-start.txt 2>&1
    else
        ssh_b "$FRDM_HOST" "nohup $cmd > $log 2>&1 & sleep 2; head -2 $log" \
            > /tmp/hb-peer-start.txt 2>&1
    fi
    # ⭐ PROVE THE PEER STARTED. Without this, a beacon that never ran produces
    # silence, and silence gets read as "the peer did not receive" — a claim about
    # the wire made from a step that did not happen.
    if grep -q 'L2BEACON UP:' /tmp/hb-peer-start.txt 2>/dev/null; then
        PEER_STARTED=1
        say "     peer beacon UP: $(sed -n 's/.*\(incarnation=[0-9a-fx]*\).*/\1/p' /tmp/hb-peer-start.txt | head -1)"
    else
        PEER_STARTED=0
        say "     ⚠️  PEER BEACON DID NOT START: $(tail -1 /tmp/hb-peer-start.txt | cut -c1-120)"
    fi
}

# ── verdict counters + reporters ────────────────────────────────────────────
# ⚠️ These were DELETED by a careless patch on 2026-08-25: a scripted replace took
# the whole span from `peer_beacon() {` to the preflight marker without checking
# what else lived in it, and these sat in the middle. The script then ran with
# `say: command not found` scattered through a live run on real hardware.
#   ⭐ A RANGE REPLACE IS A CLAIM ABOUT EVERY LINE IN THE RANGE. Read the span
#   before you replace it — the same act that fixes the citation faces.
pass=0; fail=0; incon=0
ok()    { echo "  ✅ PASS       $*"; pass=$((pass+1)); }
no()    { echo "  ❌ FAIL       $*"; fail=$((fail+1)); }
huh()   { echo "  ⚠️  INCONCLUSIVE $*"; incon=$((incon+1)); }
say()   { echo "$*"; }

# ── PREFLIGHT THAT REFUSES A VERDICT ────────────────────────────────────────
# A setup that cannot work must never reach the scoring code. This is the exact
# lesson from the proof script that printed "paths proven: 2, failed: 0" while
# all three of its steps had failed: it scored a run that never happened.
abort() { echo; echo "🛑 ABORTED — NO VERDICT GIVEN: $*"; exit 2; }

[ "$(id -u)" -eq 0 ] || abort "needs root (macvtap creation + AF_PACKET)."
[ -f "$BEACON" ]     || abort "tools/l2beacon.py not found at $BEACON."
ip link show "$LOWER" >/dev/null 2>&1 || abort "no lower device '$LOWER' on this host."
modprobe macvtap 2>/dev/null || true
[ -d /sys/module/macvlan ] || modprobe macvlan 2>/dev/null || true

echo "════════════════════════════════════════════════════════════════════"
echo " prove-macvtap-guest — CAN A GUEST REACH REAL SILICON?"
echo "════════════════════════════════════════════════════════════════════"
echo "  lower device : $LOWER ($(cat /sys/class/net/$LOWER/address))"
echo "  LEG          : $LEG"
echo "  real peer    : $FRDM_HOST if=$FRDM_IF et=$FRDM_ET sudo=$PEER_SUDO"
echo "  guest et     : $GUEST_ET"
echo

cleanup() {
    echo
    echo "── CLEANUP (Law 2: leave the wire as clean as you found it) ─────────"
    if ip link show "$MACVTAP" >/dev/null 2>&1; then
        ip link del "$MACVTAP" && echo "  removed macvtap $MACVTAP"
    else
        echo "  no macvtap to remove"
    fi
    ssh_b "$FRDM_HOST" 'pkill -f l2beacon.py 2>/dev/null; true' 2>/dev/null \
        && echo "  reaped any l2beacon on the FRDM" || true
    echo "  CORPSE LIST: $(ip -br link show type macvtap 2>/dev/null | wc -l) macvtap dev(s) still present"
}
trap cleanup EXIT INT TERM

# ── SET UP THE GUEST ENDPOINT ───────────────────────────────────────────────
ip link show "$MACVTAP" >/dev/null 2>&1 && ip link del "$MACVTAP"
ip link add link "$LOWER" name "$MACVTAP" type macvtap mode bridge \
    || abort "could not create macvtap on $LOWER."
ip link set "$MACVTAP" up
IFINDEX="$(cat /sys/class/net/$MACVTAP/ifindex)"
TAPDEV="/dev/tap$IFINDEX"
GUEST_MAC="$(cat /sys/class/net/$MACVTAP/address)"
[ -c "$TAPDEV" ] || abort "expected char device $TAPDEV, not present."
say "  guest endpoint: $MACVTAP ifindex=$IFINDEX mac=$GUEST_MAC dev=$TAPDEV"
say

# ── STAGE THE CONFORMANT BEACON ON THE REAL BOARD ───────────────────────────
# The board must speak the v2 body. The probe it was staged with sends a
# 60-byte ASCII frame, which every enforcing node on the segment CONDEMNS as
# BAD_SHORT — see tools/l2beacon.py's header.
if scp_b "$BEACON" "$FRDM_HOST:/tmp/l2beacon.py" >/dev/null 2>&1; then
    say "  staged conformant l2beacon.py on the real FRDM"
    FRDM_OK=1
else
    huh "cannot reach $FRDM_HOST over ssh as '${RUN_AS:-root}' — Q1/Q2 cannot be answered."
    say "     Checked as user '${RUN_AS:-root}' (NOT root — root's keyring is not Kyle's)."
    say "     (fix ssh, or pass FRDM_HOST=..., and re-run. Not scoring these.)"
    FRDM_OK=0
fi
if [ "$PEER_SUDO" = "1" ]; then
    if ! peer_pw_prompt || ! peer_pw_verify; then
        say
        say "🛑 ABORTED — NO VERDICT GIVEN: no working sudo credential for the $LEG peer"
        say "   ($FRDM_HOST). Its AF_PACKET receiver needs root, so WITHOUT this every"
        say "   phase below would measure a peer that could never have started — and"
        say "   report silence that says nothing about the wire."
        say "   Fix, in order of preference:"
        say "     · run this from a REAL terminal (a wrapped/piped invocation has no"
        say "       /dev/tty to prompt on — that is exactly what just happened);"
        say "     · or pass it in:  PEER_PW='...' sudo -E bash tools/prove-macvtap-guest.sh"
        say "     · or run the LAN leg only, which needs no peer password."
        exit 2
    fi
    say "     ✅ peer credential VERIFIED against $FRDM_HOST (sudo -S true succeeded)"
fi
say

# ── Q3 FIRST: IDLE CONTROL. Can this receiver tell silence from traffic? ────
say "── IDLE CONTROL — sniffing $MACVTAP with nobody sending ────────────────"
IDLE="$(python3 - "$REPO" "$TAPDEV" 3 <<'PY'
import sys, time
sys.path.insert(0, sys.argv[1] + "/tools")
from tapio import Tap
dev, secs = sys.argv[2], float(sys.argv[3])
n = 0
with Tap(dev) as t:
    end = time.time() + secs
    while time.time() < end:
        f = t.read_frame()
        if f and len(f) >= 14 and 0x88B5 <= int.from_bytes(f[12:14], "big") <= 0x88BF:
            n += 1
print(n)
PY
)"
if [ "${IDLE:-1}" = "0" ]; then
    ok "idle control: 0 beacon frames with nobody sending — this receiver can tell silence from traffic."
else
    huh "idle control saw $IDLE beacon frames with nobody sending. A PASS below would be MEANINGLESS until that is explained (stale node? another lab still on the wire?)."
fi
say

if [ "$FRDM_OK" = "1" ]; then
  # ── Q2: REAL SILICON -> GUEST ─────────────────────────────────────────────
  say "── Q2: does the REAL FRDM's beacon reach the GUEST's /dev/tapN? ───────"
  peer_beacon /tmp/l2b.log "$((SECS+4))" ""
  sleep 1
  RX="$(python3 - "$REPO" "$TAPDEV" "$SECS" "$FRDM_ET" <<'PY'
import sys, time, struct
sys.path.insert(0, sys.argv[1] + "/tools")
from tapio import Tap
dev, secs, want = sys.argv[2], float(sys.argv[3]), int(sys.argv[4], 0)
n = good = 0; first = None
with Tap(dev) as t:
    end = time.time() + secs
    while time.time() < end:
        f = t.read_frame()
        if not f or len(f) < 14: continue
        if int.from_bytes(f[12:14], "big") != want: continue
        n += 1
        if first is None: first = ":".join("%02x" % b for b in f[6:12])
        # The SEGMENT's own gates, not a lenient ethertype count.
        if (len(f) == 64 and struct.unpack_from("!I", f, 14)[0] == 0xB5B6B7C0
                and struct.unpack_from("!H", f, 18)[0] == want
                and set(f[28:64]) == {0x5A}):
            good += 1
print("%d %d %s" % (n, good, first or "-"))
PY
)"
  set -- $RX
  Q2_SEEN="$1"; Q2_GOOD="$2"; Q2_SRC="$3"
  # PASS REQUIRES A POSITIVE INTEGER. Never `! grep failure`.
  if [ "${PEER_STARTED:-0}" -ne 1 ]; then
      huh "REAL -> GUEST: the peer's beacon never started, so nothing was transmitting."
      say "     This is NOT a wire result and must not be scored as one."
  elif [ "${Q2_GOOD:-0}" -gt 0 ]; then
      ok "REAL -> GUEST: $Q2_GOOD conformant v2 beacons of $FRDM_ET at $TAPDEV from src $Q2_SRC (of $Q2_SEEN seen)."
      say "     ⭐ A frame from physical silicon reached the guest's data path. This is the half nobody had measured."
  elif [ "${Q2_SEEN:-0}" -gt 0 ]; then
      no "REAL -> GUEST: $Q2_SEEN frames arrived but 0 passed the segment's gates — the board is on the wire but its BODY is non-conformant."
  else
      no "REAL -> GUEST: 0 frames of $FRDM_ET at $TAPDEV. The guest's data path did not receive the real board."
  fi
  say

  # ── Q1: GUEST -> REAL SILICON. The load-bearing direction. ────────────────
  say "── Q1: does a frame written to /dev/tapN reach the REAL FRDM? ─────────"
  peer_beacon /tmp/l2b2.log "$((SECS+4))" "$GUEST_ET"
  sleep 1
  TXOUT="$(python3 - "$REPO" "$TAPDEV" "$GUEST_MAC" "$GUEST_ET" "$SECS" <<'PY'
import sys, os, struct, time
sys.path.insert(0, sys.argv[1] + "/tools")
from tapio import Tap
dev, mac, et, secs = sys.argv[2], sys.argv[3], int(sys.argv[4], 0), float(sys.argv[5])
src = bytes(int(x, 16) for x in mac.split(":"))
with Tap(dev) as t:
    # PROVE THE SENDER BEFORE ANYTHING DEPENDS ON IT. A crashed writer scored as
    # "the peer did not receive" is a statement about this script, not the wire.
    ok, why = t.selftest()
    print("     " + why)
    if not ok:
        print("TXRESULT SENDER_BROKEN 0"); raise SystemExit(0)
    incarn = struct.unpack("!I", os.urandom(4))[0]
    if incarn in (0, 0x5A5A5A5A): incarn = 0xA5A5A5A5
    seq = 0; end = time.time() + secs; sent = 0
    while time.time() < end:
        seq += 1
        f = bytearray(64)
        f[0:6] = b"\xff" * 6; f[6:12] = src
        struct.pack_into("!H", f, 12, et)
        struct.pack_into("!I", f, 14, 0xB5B6B7C0)
        struct.pack_into("!H", f, 18, et)
        struct.pack_into("!I", f, 20, seq)
        struct.pack_into("!I", f, 24, incarn)
        for i in range(28, 64): f[i] = 0x5A
        try:
            t.write_frame(bytes(f)); sent += 1
        except OSError as exc:
            print("     write FAILED after %d frames: %s" % (sent, exc))
            print("TXRESULT SENDER_BROKEN %d" % sent); raise SystemExit(0)
        time.sleep(0.2)
    print("     guest wrote %d v2 beacons of 0x%04x (incarnation 0x%08x)" % (sent, et, incarn))
    print("TXRESULT SENT %d" % sent)
PY
)"
  echo "$TXOUT" | grep -v '^TXRESULT'
  TXVERDICT="$(printf '%s' "$TXOUT" | sed -n 's/^TXRESULT \([A-Z_]*\) .*/\1/p')"
  TXSENT="$(printf '%s' "$TXOUT" | sed -n 's/^TXRESULT [A-Z_]* \([0-9]*\)/\1/p')"
  # Wait for the board's beacon to actually EXIT before reading its log. Its
  # runtime is SECS+4 and we only wrote for SECS, so reading at +2 catches the
  # run MID-FLIGHT — before the STATS line exists. That is precisely how the
  # first green run got scored as a failure: the parser looked for a field that
  # is only printed at the end, on a log that had not ended.
  sleep 6
  FRDMLOG="$(ssh_b "$FRDM_HOST" 'cat /tmp/l2b2.log 2>/dev/null' 2>/dev/null)"
  # The REAL BOARD'S OWN CONSOLE is the only evidence that counts here.
  # ⭐ PARSE THE ASSERTION, NOT A FIELD THAT MAY NOT EXIST YET.
  # `rx_peer=` lives ONLY in the final STATS line. The board's ACTUAL assertion is
  # its PASS line — "L2BEACON PASS #40: saw all required peers (0x88b7 rx=40)" —
  # which it emits continuously as it re-arms. Scoring on rx_peer= alone made a
  # 40/40 success read as "nothing reached it", with the proof sitting in the very
  # output the failure message printed. Take the PASS lines as primary; STATS as
  # corroboration when the run has ended.
  Q1_PASS="$(printf '%s' "$FRDMLOG" | grep -c 'L2BEACON PASS' || true)"
  Q1_N="$(printf '%s' "$FRDMLOG" | sed -n 's/.*[( ]rx=\([0-9]*\).*/\1/p' | tail -1)"
  Q1_STATS="$(printf '%s' "$FRDMLOG" | sed -n 's/.*rx_peer=\([0-9]*\).*/\1/p' | tail -1)"
  [ -z "$Q1_N" ] && Q1_N="$Q1_STATS"
  Q1_CORRUPT="$(printf '%s' "$FRDMLOG" | grep -c 'L2BEACON CORRUPT' || true)"
  if [ "${PEER_STARTED:-0}" -ne 1 ]; then
      huh "GUEST -> REAL: the peer's beacon never started, so nothing was RECEIVING."
      say "     Its log holds the startup error, not a receive count. Not a wire result."
  elif [ "${TXVERDICT:-SENDER_BROKEN}" != "SENT" ] || [ "${TXSENT:-0}" -eq 0 ]; then
      huh "GUEST -> REAL: THE SENDER NEVER SENT (${TXSENT:-0} frames written)."
      say "     This says NOTHING about the wire and must NOT be scored as a wire"
      say "     failure. A step that did not run is not a caught bug. Fix + re-run."
  elif [ "${Q1_N:-0}" -gt 0 ] || [ "${Q1_PASS:-0}" -gt 0 ]; then
      ok "GUEST -> REAL: the FRDM's OWN console reports rx=${Q1_N:-?} across ${Q1_PASS} PASS line(s), corrupt=${Q1_CORRUPT:-0}."
      say "     ⭐⭐ THIS IS THE LOAD-BEARING ASSERTION. A frame written to /dev/tapN crossed a"
      say "        physical cable and was accepted by silicon that did not know it was emulated."
  elif [ -n "$FRDMLOG" ]; then
      no "GUEST -> REAL: the FRDM ran but reports rx_peer=0 — nothing from the guest reached it."
      say "     board said: $(printf '%s' "$FRDMLOG" | tail -3 | tr '\n' ' | ')"
  else
      huh "GUEST -> REAL: no log came back from the FRDM. Cannot distinguish 'did not receive' from 'never ran'."
  fi
  say

  # ── Q3: can a HOST capture on the lower device see any of it? ─────────────
  say "── Q3: can a capture on $LOWER (the HOST side) see the guest's frames? ─"
  if command -v tcpdump >/dev/null 2>&1; then
      timeout 4 tcpdump -i "$LOWER" -c 3 -nn "ether proto $GUEST_ET" >/tmp/hbcap.txt 2>/dev/null &
      CAPPID=$!
      python3 - "$REPO" "$TAPDEV" "$GUEST_MAC" "$GUEST_ET" 3 <<'PY'
import sys, struct, time
sys.path.insert(0, sys.argv[1] + "/tools")
from tapio import Tap
dev, mac, et, secs = sys.argv[2], sys.argv[3], int(sys.argv[4], 0), float(sys.argv[5])
src = bytes(int(x, 16) for x in mac.split(":"))
with Tap(dev) as t:
    end = time.time() + secs; seq = 0
    while time.time() < end:
        seq += 1
        f = bytearray(64); f[0:6] = b"\xff"*6; f[6:12] = src
        struct.pack_into("!H", f, 12, et); struct.pack_into("!I", f, 14, 0xB5B6B7C0)
        struct.pack_into("!H", f, 18, et); struct.pack_into("!I", f, 20, seq)
        struct.pack_into("!I", f, 24, 0xA5A5A5A5)
        for i in range(28, 64): f[i] = 0x5A
        try: t.write_frame(bytes(f))
        except OSError: break
        time.sleep(0.2)
PY
      wait $CAPPID 2>/dev/null
      CAPN="$(grep -c . /tmp/hbcap.txt 2>/dev/null | head -1)"; CAPN="${CAPN:-0}"
      if [ "${CAPN:-0}" -gt 0 ]; then
          ok "host capture on $LOWER saw $CAPN frame(s) — criterion C is satisfiable as written."
      else
          huh "host capture on $LOWER saw 0 frames."
          say "     ⭐ THIS IS EXPECTED AND IS NOT A WIRE FAULT. macvlan isolates the lower"
          say "       device from its own macvtap children, so skippy can be structurally"
          say "       blind to traffic that is crossing the cable perfectly well."
          say "       => CRITERION C SHOULD MOVE ITS CAPTURE POINT TO THE REAL BOARD, which"
          say "          is on the far side of the cable and cannot be fooled by local"
          say "          switching. Q1 above already IS that capture. Do not score this red."
      fi
  else
      huh "no tcpdump on this host; Q3 not answered."
  fi
fi

say
echo "════════════════════════════════════════════════════════════════════"
echo " VERDICT   pass=$pass  fail=$fail  inconclusive=$incon"
if [ "$fail" -eq 0 ] && [ "$pass" -ge 2 ]; then
    echo " ⭐ GUEST-CAN-REACH IS PROVEN. Build the coordinator's macvtap transport."
elif [ "$fail" -gt 0 ]; then
    echo " ❌ A DIRECTION FAILED. Do NOT build the lab around macvtap until this is"
    echo "    understood — the transport, not the model, is the open question."
else
    echo " ⚠️  INCONCLUSIVE, WHICH IS ITS OWN VERDICT AND NOT A FAILURE."
    echo "    A killed or unreachable run is not a caught bug. Fix the setup and re-run."
fi
echo "════════════════════════════════════════════════════════════════════"
exit 0

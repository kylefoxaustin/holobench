#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-or-later
#
# prove-oracle-bites.sh — CAN THE REAL BOARD'S ORACLE SAY NO?
#
# ═════════════════════════════════════════════════════════════════════════════
# WHY THIS RUNS BEFORE ANYONE TRUSTS THE GREEN
#
# On 2026-08-22 the real i.MX 95 FRDM reported, on its own console:
#
#     L2BEACON PASS #40: saw all required peers (0x88b7 rx=40)
#
# 40 written by an emulated guest, 40 received by physical silicon. That is the
# lab's load-bearing assertion and it is genuinely measured.
#
# ⭐ AND IT IS WORTH NOTHING UNTIL THE SAME ORACLE HAS BEEN SHOWN TO FAIL.
#
# A green run with no failing counterpart proves the WIRE, not the ASSERTION. An
# oracle that says PASS at everything that moves says PASS at 40 frames too, and
# the two are indistinguishable from the outside. This fleet has already paid for
# that lesson twice: rt1180's monitor matched its own banner and shouted PASS
# twelve times at an EMPTY WIRE, and holobench's own scorer called a 40/40 success
# a FAILURE while printing the proof of success three lines below it.
#
#     IF IT CANNOT FAIL, IT HAS NOT BEEN TESTED.
#
# ═════════════════════════════════════════════════════════════════════════════
# ⚠️ WHY WE DO **NOT** DO THE OBVIOUS THING (`ip link set eth1 down`)
#
# The lab spec proposed: "unplug/down the FRDM and re-run — B must FAIL."
# Correct in spirit, DANGEROUS as written on this board:
#
#     the FRDM is reached at root@10.0.1.181 ... ON eth1.
#     eth1 IS THE SSH PATH.
#
# `ip link set eth1 down` over ssh on eth1 kills the connection that would bring
# it back up. The board is then unreachable until somebody physically walks to it.
# A negative control that can brick the asset under test is not a control; it is a
# second failure mode wearing the costume of rigour.
#
# ⭐ SO THE "UNPLUG" HAPPENS ON **OUR** SIDE INSTEAD: destroying the macvtap takes
#   the GUEST off the wire, which is the same experiment from the other end — and
#   it is reversible, needs no board access, and cannot strand anything.
#
# ═════════════════════════════════════════════════════════════════════════════
# THE FIVE PHASES — one ARMED, four that MUST come back red
#
#   ① ARMED       conformant 0x88B7  ->  the board MUST PASS.
#                 Without this the other four prove only that a broken setup is
#                 broken. A control needs something to control AGAINST.
#   ② WRONG-ET    conformant body, ethertype 0x88BE (in-block, nobody's).
#                 MUST NOT PASS. Proves the oracle discriminates rather than
#                 counting anything that arrives.
#   ③ CORRUPT     right ethertype, BROKEN MAGIC. MUST be counted CORRUPT and NOT
#                 as a sighting. ⭐ THE SHARPEST ONE: this is what separates "I saw
#                 an ethertype" from "I saw a FRAME". The 4-node lab was once green
#                 on frames DMA'd from address zero, because a garbage body has the
#                 same ethertype as a good one.
#   ④ LEGACY      right ethertype, valid body, INCARN = 0x5A5A5A5A. MUST report
#                 LEGACY and MUST NOT satisfy the gate (enet-lab3.c:594 —
#                 "freshness UNVERIFIABLE ... segment stays red"). Proves the
#                 freshness gate is real and not decorative.
#   ⑤ UNPLUGGED   macvtap destroyed; guest gone. The board's PASS stream MUST STOP.
#                 ⭐ Only observable because l2beacon RE-ARMS instead of latching:
#                 a latched oracle prints PASS once and is blind forever after, and
#                 a SATISFIED assertion and an ABSENT one print the same thing.
#                 This phase is what makes re-arming worth having.
#
# VERDICT: the oracle is TESTED only if ① is GREEN **and** ②③④⑤ are all RED.
# Any of ②-⑤ coming back green means the oracle passes things it should refuse,
# and every green it has ever produced — including the 40/40 — is void.
#
# ═════════════════════════════════════════════════════════════════════════════
# LAW 2 — this runs on a SHARED FLEET BOARD
#
# imx95-frdm is a shared asset. This script RESERVES it before touching it and
# RELEASES it with a corpse list, because a board left beaconing after a run is
# not litter — it is a peer that outlives its run, and on a shared segment its
# testimony is indistinguishable from a real one.
#
#   usage:  sudo bash tools/prove-oracle-bites.sh
#
set -uo pipefail

LOWER="${LOWER:-enp6s0}"
MACVTAP="hb-nc0"
GUEST_ET="0x88B7"
DECOY_ET="0x88BE"          # in-block, assigned to nobody in the fleet
FRDM_ET="0x88B9"
FRDM_HOST="${FRDM_HOST:-root@10.0.1.181}"
FRDM_IF="${FRDM_IF:-eth1}"
PHASE_S="${PHASE_S:-6}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BEACON="$REPO/tools/l2beacon.py"

RUN_AS="${SUDO_USER:-}"
as_user() { if [ -n "$RUN_AS" ]; then sudo -u "$RUN_AS" -H "$@"; else "$@"; fi; }

# ── TRANSCRIPT + EVIDENCE DIR ───────────────────────────────────────────────────────────
# ⚠️ ADDED 2026-08-27, AFTER THE EVIDENCE FROM THIS SCRIPT'S BEST RUN WAS LOST.
# On 2026-08-25 this control proved the oracle bites: phase ③ planted broken magic and the
# board reported CORRUPT=30. What survived is that COUNT, in a terminal transcript captured
# outside this script. The raw L2BEACON CORRUPT lines went to the BOARD's /tmp, were read
# into a shell variable, counted, and dropped. Board /tmp is gone; so is the only sample of
# what a corrupt line looks like.
#
# ⭐ A COUNT IS A CLAIM ABOUT A SAMPLE; THE SAMPLE IS THE EVIDENCE. Keeping "CORRUPT=30"
# and discarding the thirty lines is the same trade as reporting a measurement without the
# log — and it cost a real capability: BOARD_CORRUPT is now the one scorer pattern with no
# real sample to test against (qualcomm's rule: a detection pattern should be tested
# against a real sample of what it detects).
#
# Two failures, not one, and both are fixed here:
#   1. the script never recorded ITSELF — the 08-25 transcript exists by luck of how it
#      was invoked, and a control whose output depends on the caller is not reproducible;
#   2. it preserved the failure sample onto VOLATILE REMOTE STORAGE, which reads as
#      preservation right up until you go looking.
RUN_DIR="$REPO/scratchpad-consoles/runs/oracle-bites-$(date +%Y%m%d-%H%M%S)"
as_user mkdir -p "$RUN_DIR" 2>/dev/null || mkdir -p "$RUN_DIR"
as_user touch "$RUN_DIR/transcript.log" 2>/dev/null || touch "$RUN_DIR/transcript.log"
exec > >(tee -a "$RUN_DIR/transcript.log") 2>&1
echo "📝 transcript + per-phase board logs: $RUN_DIR"
ssh_b()   { as_user ssh -o BatchMode=yes -o ConnectTimeout=5 "$@"; }
scp_b()   { as_user scp -o BatchMode=yes -o ConnectTimeout=5 "$@"; }

armed_ok=0; controls_held=0; controls_broken=0; incon=0
ok()  { echo "  ✅ $*"; }
no()  { echo "  ❌ $*"; }
huh() { echo "  ⚠️  $*"; incon=$((incon+1)); }
abort(){ echo; echo "🛑 ABORTED — NO VERDICT GIVEN: $*"; exit 2; }

[ "$(id -u)" -eq 0 ] || abort "needs root (macvtap creation)."
[ -f "$BEACON" ]     || abort "tools/l2beacon.py not found."
ip link show "$LOWER" >/dev/null 2>&1 || abort "no lower device '$LOWER'."
modprobe macvtap 2>/dev/null || true

echo "══════════════════════════════════════════════════════════════════════"
echo " prove-oracle-bites — CAN THE REAL BOARD SAY NO?"
echo "  (safe to re-run; each phase keeps its own board-side log)"
echo "══════════════════════════════════════════════════════════════════════"
echo "  board   : $FRDM_HOST if=$FRDM_IF et=$FRDM_ET  (watching $GUEST_ET)"
echo "  wire    : $LOWER"
echo "  ⚠️  the board is NEVER downed — eth1 is its ssh path. The 'unplug'"
echo "     phase destroys OUR macvtap instead. Same experiment, no stranding."
echo

# ── LAW 2: reserve the shared board BEFORE touching it ──────────────────────
# ⚠️ NOT $HOME. Under sudo, $HOME is ROOT'S home (/root), so the lease would be
# written to /root/.claude/... — a directory the fleet's registry never reads. The
# board would look UNRESERVED to every other session, AND the "is someone else
# holding it?" check above would read the wrong file and always find nothing. A
# safety check that silently reads the wrong path is worse than no check: it
# reports "clear" with the same confidence either way.
# This is the THIRD sudo-environment bug in this lab (root's ssh keys, root's ssh
# config, now root's HOME). The pattern: sudo changes WHO YOU ARE, and every
# user-scoped path and credential moves with it.
LEASE="/home/${SUDO_USER:-$(id -un)}/.claude/bus-state/resources/imx95-frdm/lease"
if [ -f "$LEASE" ]; then
    OWNER="$(sed -n 's/^owner=//p' "$LEASE")"
    if [ -n "$OWNER" ] && [ "$OWNER" != "other:holobench" ]; then
        abort "imx95-frdm is held by $OWNER. NOT running on someone else's board — \
a beacon during their run would bias their measurements and they would have no \
way to attribute it."
    fi
fi
as_user mkdir -p "$(dirname "$LEASE")" 2>/dev/null
printf 'owner=other:holobench\nowner_pid=%s\nmode=hard\nacquired_epoch=%s\nexpires_epoch=%s\nlast_active_epoch=%s\nreason=negative control - proving the FRDM oracle can fail\n' \
    "$$" "$(date +%s)" "$(( $(date +%s) + 1800 ))" "$(date +%s)" | as_user tee "$LEASE" >/dev/null
[ -s "$LEASE" ] && grep -q '^owner=other:holobench' "$LEASE" \
    || abort "could not write the lease at $LEASE — REFUSING TO RUN. Law 2 is not \
a formality here: without a registered hold, another session can start a job on \
this board mid-run and neither of us would be able to attribute the interference. \
An unverified reservation is not a reservation."
echo "  🔒 reserved imx95-frdm (hard, 30 min) — Law 2 [verified on disk]"
echo

cleanup() {
    echo
    echo "── CLEANUP (Law 2: leave the wire as clean as you found it) ──────────"
    ip link show "$MACVTAP" >/dev/null 2>&1 && ip link del "$MACVTAP" && echo "  removed macvtap $MACVTAP"
    ssh_b "$FRDM_HOST" 'pkill -f l2beacon.py 2>/dev/null; true' >/dev/null 2>&1 \
        && echo "  reaped l2beacon on the FRDM (no peer outlives its run)" || true
    as_user rm -f "$LEASE" 2>/dev/null && echo "  🔓 released imx95-frdm"
    echo "  CORPSE LIST: $(ip -br link show type macvtap 2>/dev/null | wc -l) macvtap dev(s), \
$(ssh_b "$FRDM_HOST" 'pgrep -c "^python3$" 2>/dev/null || echo 0' 2>/dev/null | tr -d '\n') beacon(s) on the board"
}
trap cleanup EXIT INT TERM

# A stale log from a PREVIOUS run is indistinguishable from this run's evidence.
ssh_b "$FRDM_HOST" 'rm -f /tmp/nc-p*.log /tmp/nc.log /tmp/nc5.log' >/dev/null 2>&1
scp_b "$BEACON" "$FRDM_HOST:/tmp/l2beacon.py" >/dev/null 2>&1 \
    || abort "cannot stage the beacon on $FRDM_HOST over ssh as '${RUN_AS:-root}'."

mk_tap() {
    ip link show "$MACVTAP" >/dev/null 2>&1 && ip link del "$MACVTAP"
    ip link add link "$LOWER" name "$MACVTAP" type macvtap mode bridge || return 1
    ip link set "$MACVTAP" up
    IFINDEX="$(cat /sys/class/net/$MACVTAP/ifindex)"
    TAPDEV="/dev/tap$IFINDEX"
    GUEST_MAC="$(cat /sys/class/net/$MACVTAP/address)"
    chown "$(id -u ${RUN_AS:-root})":"$(id -g ${RUN_AS:-root})" "$TAPDEV" 2>/dev/null || true
    [ -c "$TAPDEV" ]
}

# run_phase <label> <ethertype> <mutation> <expect: PASS|NOPASS>
PHASE_N=0
run_phase() {
    local label="$1" et="$2" mut="$3" expect="$4"
    PHASE_N=$((PHASE_N+1))
    local plog="/tmp/nc-p${PHASE_N}.log"
    echo "── $label ──────────────────────────────────────────────────────────"
    ssh_b "$FRDM_HOST" \
      "nohup python3 /tmp/l2beacon.py --runtime $((PHASE_S+5)) $FRDM_IF $FRDM_ET $GUEST_ET >$plog 2>&1 &" \
      >/dev/null 2>&1
    sleep 1

    local txout
    txout="$(python3 - "$REPO" "$TAPDEV" "$GUEST_MAC" "$et" "$PHASE_S" "$mut" <<'PYEOF'
import sys, os, struct, time
sys.path.insert(0, sys.argv[1] + "/tools")
from tapio import Tap
dev, mac, et, secs, mut = sys.argv[2], sys.argv[3], int(sys.argv[4], 0), float(sys.argv[5]), sys.argv[6]
src = bytes(int(x, 16) for x in mac.split(":"))
with Tap(dev) as t:
    ok, why = t.selftest()
    if not ok:
        print("TXRESULT SENDER_BROKEN 0"); print("     " + why); raise SystemExit(0)
    incarn = 0x5A5A5A5A if mut == "legacy" else (struct.unpack("!I", os.urandom(4))[0] or 0xA5A5A5A5)
    if mut != "legacy" and incarn == 0x5A5A5A5A: incarn = 0xA5A5A5A5
    seq = 0; sent = 0; end = time.time() + secs
    while time.time() < end:
        seq += 1
        f = bytearray(64)
        f[0:6] = b"\xff" * 6; f[6:12] = src
        struct.pack_into("!H", f, 12, et)
        struct.pack_into("!I", f, 14, 0xDEADBEEF if mut == "magic" else 0xB5B6B7C0)
        struct.pack_into("!H", f, 18, et)
        struct.pack_into("!I", f, 20, seq)
        struct.pack_into("!I", f, 24, incarn)
        for i in range(28, 64): f[i] = 0x5A
        try:
            t.write_frame(bytes(f)); sent += 1
        except OSError as exc:
            print("TXRESULT SENDER_BROKEN %d" % sent); raise SystemExit(0)
        time.sleep(0.2)
    print("TXRESULT SENT %d" % sent)
PYEOF
)"
    echo "$txout" | grep -v '^TXRESULT' || true
    local verdict sent
    verdict="$(printf '%s' "$txout" | sed -n 's/^TXRESULT \([A-Z_]*\) .*/\1/p')"
    sent="$(printf '%s' "$txout" | sed -n 's/^TXRESULT [A-Z_]* \([0-9]*\)/\1/p')"

    sleep 6
    local log passn rxn corruptn legacyn
    log="$(ssh_b "$FRDM_HOST" "cat $plog 2>/dev/null" 2>/dev/null)"
    # ⭐ SAVE THE SAMPLE, NOT JUST THE COUNT. $plog lives on the BOARD's /tmp and does not
    # survive a reboot or a tmp sweep — which is exactly how the 08-25 corrupt sample was
    # lost. Copy it beside the transcript before anyone counts anything.
    local keep="$RUN_DIR/phase${PHASE_N}-${label//[^A-Za-z0-9]/_}.board.log"
    printf '%s\n' "$log" > "$keep" 2>/dev/null || true
    echo "     board log kept at $plog (on the board) AND preserved at $keep"
    passn="$(printf '%s' "$log" | grep -c 'L2BEACON PASS' || true)"
    rxn="$(printf '%s' "$log" | sed -n 's/.*[( ]rx=\([0-9]*\).*/\1/p' | tail -1)"
    corruptn="$(printf '%s' "$log" | grep -c 'L2BEACON CORRUPT' || true)"
    legacyn="$(printf '%s' "$log" | grep -c 'L2BEACON LEGACY' || true)"

    # A step that did not run is INCONCLUSIVE, never a result. (Learned the hard way:
    # a crashed writer once got scored as "nothing reached the board".)
    if [ "${verdict:-X}" != "SENT" ] || [ "${sent:-0}" -eq 0 ]; then
        huh "$label: THE SENDER NEVER SENT (${sent:-0} frames). Says nothing about the oracle."
        echo; return
    fi
    echo "     guest sent $sent frames of $et (mutation: ${mut:-none})"
    echo "     board: PASS=$passn  rx=${rxn:-0}  CORRUPT=$corruptn  LEGACY=$legacyn"

    if [ "$expect" = "PASS" ]; then
        if [ "${passn:-0}" -gt 0 ]; then
            ok "$label: board PASSED as required — there IS something to control against."
            armed_ok=1
        else
            no "$label: board did NOT pass. The armed case is broken, so the controls below"
            echo "     prove only that a broken setup is broken. Fix this first."
        fi
    else
        if [ "${passn:-0}" -eq 0 ]; then
            ok "$label: board REFUSED — control held. ⭐ The oracle CAN say no."
            controls_held=$((controls_held+1))
            case "$mut" in
              magic)  [ "${corruptn:-0}" -gt 0 ] \
                        && echo "     and it said WHY: $corruptn CORRUPT — the body gate bit, not just the ethertype." \
                        || echo "     ⚠️ but logged no CORRUPT line — it refused for some OTHER reason. Weaker than it looks." ;;
              legacy) [ "${legacyn:-0}" -gt 0 ] \
                        && echo "     and it said WHY: $legacyn LEGACY — freshness unverifiable, gate stays red." \
                        || echo "     ⚠️ but logged no LEGACY line — refused for some OTHER reason. Weaker than it looks." ;;
            esac
        else
            no "$label: ⭐ BOARD PASSED WHEN IT MUST NOT. THE ORACLE IS BROKEN."
            echo "     It accepts $et/${mut:-none}. Every green this oracle has produced —"
            echo "     INCLUDING THE 40/40 — is void until this is explained."
            controls_broken=$((controls_broken+1))
        fi
    fi
    echo
}

mk_tap || abort "could not create macvtap on $LOWER."
echo "  guest endpoint: $MACVTAP ifindex=$IFINDEX mac=$GUEST_MAC dev=$TAPDEV"
echo

run_phase "① ARMED      conformant $GUEST_ET"                 "$GUEST_ET" ""       PASS
run_phase "② WRONG-ET   conformant body, $DECOY_ET"           "$DECOY_ET" ""       NOPASS
run_phase "③ CORRUPT    $GUEST_ET with BROKEN MAGIC"          "$GUEST_ET" magic    NOPASS
run_phase "④ LEGACY     $GUEST_ET, INCARN=0x5A5A5A5A"         "$GUEST_ET" legacy   NOPASS

# ── ⑤ UNPLUGGED — the guest leaves the wire entirely ────────────────────────
echo "── ⑤ UNPLUGGED   macvtap destroyed, guest off the wire ─────────────────"
ssh_b "$FRDM_HOST" \
  "nohup python3 /tmp/l2beacon.py --runtime 8 $FRDM_IF $FRDM_ET $GUEST_ET >/tmp/nc5.log 2>&1 &" >/dev/null 2>&1
ip link del "$MACVTAP" 2>/dev/null && echo "     macvtap destroyed — nothing of ours is on the wire"
sleep 10
LOG5="$(ssh_b "$FRDM_HOST" 'cat /tmp/nc5.log 2>/dev/null' 2>/dev/null)"
P5="$(printf '%s' "$LOG5" | grep -c 'L2BEACON PASS' || true)"
echo "     board: PASS=$P5"
if [ "${P5:-0}" -eq 0 ]; then
    ok "⑤ UNPLUGGED: board refused — control held."
    echo "     ⭐ Observable ONLY because l2beacon RE-ARMS. A latched oracle would still"
    echo "       be printing the PASS it earned in phase ①, at an empty wire."
    controls_held=$((controls_held+1))
else
    no "⑤ UNPLUGGED: ⭐ BOARD PASSED WITH NO GUEST ON THE WIRE. THE ORACLE IS BROKEN."
    controls_broken=$((controls_broken+1))
fi

echo
echo "══════════════════════════════════════════════════════════════════════"
echo " VERDICT   armed=$armed_ok  controls_held=$controls_held/4  broken=$controls_broken  inconclusive=$incon"
if [ "$armed_ok" -eq 1 ] && [ "$controls_held" -eq 4 ] && [ "$controls_broken" -eq 0 ]; then
    echo " ⭐⭐ THE ORACLE BITES. It passed the real case and REFUSED all four controls."
    echo "    The 40/40 is now a tested assertion, not just a green run."
elif [ "$controls_broken" -gt 0 ]; then
    echo " ❌ THE ORACLE IS BROKEN — it passed something it must refuse."
    echo "    Every green it has produced is VOID until explained. This is the most"
    echo "    valuable possible outcome of this script and the worst possible news."
else
    echo " ⚠️  INCONCLUSIVE — not all controls ran. A killed run is not a caught bug."
fi
echo "══════════════════════════════════════════════════════════════════════"
exit 0

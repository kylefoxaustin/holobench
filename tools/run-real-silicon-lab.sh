#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-or-later
#
# run-real-silicon-lab.sh — the three remaining privileged steps, in one session.
#
# All three need root on skippy, and asking for the password three times invites
# the run to be done in pieces on different days with different state. So they run
# together, in dependency order, and each one's outcome gates the next.
#
#   STEP 1  NEGATIVE CONTROL (LAN).  Can the real board's oracle say NO?
#           Until this passes, the 40/40 crossing proves the WIRE, not the
#           ASSERTION. It runs FIRST because everything downstream leans on it:
#           if the oracle cannot refuse, no later green means anything.
#
#   STEP 2  USB LEG CROSSING.  Same experiment as the LAN leg, different medium.
#           ⚠️ NOT inherited from the LAN result. The fleet's own rule — a
#           sibling's finding is a strong prior to CHECK, never a finding to
#           INHERIT. This is a cdc_ncm gadget with a different driver, and the
#           only reason we know it carries non-IP Ethernet at all is that
#           somebody measured it.
#           ⚠️ The Orin needs its OWN sudo password. You will get a SECOND prompt,
#           from the Orin, partway through. That is expected, not a fault.
#
#   STEP 3  THE FULL LAB UNDER QEMU.  Everything above proved the DATA PATH by
#           writing to /dev/tapN directly — which IS the guest's data path, but
#           the emulated ENETC has never carried these frames. This step boots
#           95emulator's 2-port artifact and puts the model in the loop.
#
# ⭐ WHY THE ORDER IS NOT ARBITRARY: step 3 is the only one that can produce the
#   headline result, and it is the one whose green is easiest to believe without
#   justification. Running the falsification first means that by the time anything
#   impressive appears, the thing that could have refuted it has already been
#   given its chance and did not take it.
#
# ⚠️ DO NOT EDIT ANY SCRIPT THIS RUNS WHILE IT IS RUNNING. bash reads a script
# lazily by byte offset; editing shifts the offsets under the live interpreter and
# it resumes mid-token in a different file. That corrupted a negative-control run
# on 2026-08-22 — phases re-executed against a destroyed macvtap and the verdict
# was computed across two passes. The results looked fine and were not trustworthy.
#
#   usage:  sudo bash tools/run-real-silicon-lab.sh [--skip-usb] [--skip-lab]
#
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_AS="${SUDO_USER:-}"
SKIP_USB=0; SKIP_LAB=0
for a in "$@"; do
    case "$a" in
        --skip-usb) SKIP_USB=1 ;;
        --skip-lab) SKIP_LAB=1 ;;
        *) echo "unknown flag: $a"; exit 2 ;;
    esac
done

[ "$(id -u)" -eq 0 ] || { echo "🛑 needs root (macvtap). Re-run with sudo."; exit 2; }
[ -n "$RUN_AS" ] || echo "⚠️  no \$SUDO_USER — ssh will use ROOT's keys and will probably fail."

hr() { echo; echo "═══════════════════════════════════════════════════════════════════════"; }
step1=SKIPPED; step2=SKIPPED; step3=SKIPPED

hr
echo " STEP 1/3 — NEGATIVE CONTROL (LAN): can the real board's oracle say NO?"
echo "═══════════════════════════════════════════════════════════════════════"
bash "$REPO/tools/prove-oracle-bites.sh"
rc=$?
if [ $rc -eq 0 ]; then step1=RAN; else step1="EXIT=$rc"; fi

hr
if [ "$SKIP_USB" = "1" ]; then
    echo " STEP 2/3 — USB LEG: skipped by flag"
else
    echo " STEP 2/3 — USB LEG CROSSING (emulated i.MX 95 <-> REAL Jetson AGX Orin)"
    echo "═══════════════════════════════════════════════════════════════════════"
    echo " ⚠️  A SECOND PASSWORD PROMPT IS COMING, and it is the ORIN's, not skippy's."
    echo "    The Orin's AF_PACKET receiver needs root there. It is asked for in ONE"
    echo "    ssh session on purpose: sudo's tty_tickets caches a credential against"
    echo "    the tty that entered it, so warming it up in one session and using it in"
    echo "    another gets a cache miss and hangs."
    echo
    LEG=USB \
    LOWER=enx42b8036560ca \
    FRDM_HOST=kyle@10.0.1.124 \
    FRDM_IF=l4tbr0 \
    PEER_ET=0x88BA \
    PEER_SUDO=1 \
        bash "$REPO/tools/prove-macvtap-guest.sh"
    rc=$?
    if [ $rc -eq 0 ]; then step2=RAN; else step2="EXIT=$rc"; fi
fi

hr
if [ "$SKIP_LAB" = "1" ]; then
    echo " STEP 3/3 — FULL LAB: skipped by flag"
else
    echo " STEP 3/3 — THE FULL LAB UNDER QEMU (the model in the loop at last)"
    echo "═══════════════════════════════════════════════════════════════════════"
    echo " Boots 95emulator's 2-port artifact (pin 0c10d5f8..., commit e5671cfa,"
    echo " reproducibility verified by rebuild) with TWO macvtap endpoints:"
    echo "   1st -nic -> PF 0002:00:00.0 -> watches 0x88B9 (real FRDM, LAN)"
    echo "   2nd -nic -> PF 0002:00:08.0 -> watches 0x88BA (real Orin, USB)"
    echo
    # ⚠️ THIS RUNS THE LAB AS ROOT, and that is a real tradeoff worth naming rather
    # than gliding past: creating a macvtap needs privilege, so QEMU inherits it.
    # For a lab on a developer box that is acceptable; for anything shared it is
    # NOT, and Phase 6 (auth + deployment hardening) is where it gets fixed —
    # properly, by pre-creating the endpoints and handing QEMU only the fd, which
    # needs no privilege at all once the device is open and chowned.
    # Recorded here so the shortcut cannot quietly become the design.
    # Asset paths resolve from the repo (not $HOME), and the venv python is called
    # by absolute path, so running as root does not misresolve either.
    "$REPO/.venv/bin/python" -m holobench.cli lab launch imx95-real-silicon \
        --hold 90 --no-auto-ip
    rc=$?
    if [ $rc -eq 0 ]; then step3=RAN; else step3="EXIT=$rc"; fi
fi

hr
echo " SEQUENCE COMPLETE"
echo "   step 1  negative control (LAN) : $step1"
echo "   step 2  USB leg crossing       : $step2"
echo "   step 3  full lab under QEMU    : $step3"
echo
echo " ⚠️  'RAN' means the step executed and printed its own verdict — it is NOT a"
echo "    verdict itself. Read each step's own PASS/FAIL/INCONCLUSIVE above. A"
echo "    wrapper that summarised its children's results would be inventing a"
echo "    judgement it never made, which is the exact failure this lab keeps"
echo "    catching: a step's exit status is not the same as its finding."
echo "═══════════════════════════════════════════════════════════════════════"

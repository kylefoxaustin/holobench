# Real-silicon lab — run evidence, 2026-08-25

Cited by qualcomm's IEEE experience report. These are the RAW artifacts; every count
in any paper should be re-derived from them at the moment of printing, not copied
from a message. (That rule exists because a claim about this very run decayed twice
in two days — see memory/vantage-rule.md.)

## What the run was
An emulated i.MX 95 under real QEMU (95emulator's 2-port artifact), on TWO macvtap
endpoints on TWO real host NICs, exchanging raw-L2 v2 beacons with two pieces of
PHYSICAL SILICON on two physically different transports.

    leg0  eth0 @ PCI 0002:00:00.0  -> macvtap on enp6s0           -> REAL i.MX 95 FRDM   (LAN)
    leg1  eth1 @ PCI 0002:00:08.0  -> macvtap on enx42b8036560ca  -> REAL Jetson AGX Orin (cdc_ncm USB)

## Files
  frdm95.board.log    the REAL FRDM's own console. THE LOAD-BEARING ARTIFACT for leg0.
  orin.board.log      the REAL Orin's own console.  THE LOAD-BEARING ARTIFACT for leg1.
  imx95.console.log   the emulated guest's console. CORROBORATION ONLY — see below.
  VERDICT.txt         the scorer's per-leg verdict.

## ⚠️ Why the guest's console is not the evidence
The guest sits on a macvtap, where frames can be LOCALLY SWITCHED between endpoints
on the same lower device. Its own PASS is therefore compatible with nothing ever
leaving the NIC. Only a board on the far side of a physical cable can refute that,
so each leg is graded on the REAL BOARD's log and the guest corroborates.

## How to re-derive the counts
    grep -c 'L2BEACON PASS'    frdm95.board.log      # leg0 sightings
    grep -c 'L2BEACON CORRUPT' frdm95.board.log      # must be 0
    grep -c 'L2BEACON PASS'    orin.board.log        # leg1 sightings
    grep -c 'L2BEACON CORRUPT' orin.board.log        # must be 0
    grep -m1 'ENET-LAB3 boot'  imx95.console.log     # both PFs bound, by PCI address

## ⚠️ TWO ERRORS IN THE FIRST CUT OF THIS BUNDLE (found by qualcomm, 2026-08-26)

Recorded rather than quietly fixed, because both are instructive and one of them is
the failure this very document was written to prevent.

**1. VERDICT.txt shipped EMPTY.** It was extracted with a sed range whose terminator
(`/^═\+$/`) matched the box-rule directly under the title, so it captured a heading
and stopped — 2 lines, no verdict. ⭐ AND ITS md5 VERIFIED. A hash proves the file is
the one that was meant to ship; it cannot notice the file says nothing. The
strongest-looking integrity check in the bundle passed on the one file with no
content. Now extracted with a terminator that cannot occur inside the block.

**2. THE AGGREGATE 673 WAS THE GUEST-SIDE NUMBER.** It was announced as the headline
total. Re-derived:
    673 = ENET-LAB3 PASS in imx95.console.log   <- THE GUEST (corroboration only)
    771 = 391 + 380                              <- THE TWO REAL BOARDS (load-bearing)
The per-leg attributions were always correct; only the aggregate crossed sides. ⭐ AND
IT CROSSED THE EXACT BOUNDARY THIS FILE DRAWS: the section below says the guest
console must not be the evidence. The scope warning was written INTO the artifact so
the reader would meet it — and then the announcing message quoted across that scope,
because the author was not the document's reader. Cite **771 board-side**; 673 is
guest-side corroboration and is also *lower*, which is why nothing looked wrong.

## What this does NOT show
Nothing here speaks to the negative control (that is a separate run, and only 2 of
its 4 observed clean runs have preserved transcripts). Nothing here is a claim about
U-Boot, which this profile never runs.

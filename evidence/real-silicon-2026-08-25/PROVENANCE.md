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

## What this does NOT show
Nothing here speaks to the negative control (that is a separate run, and only 2 of
its 4 observed clean runs have preserved transcripts). Nothing here is a claim about
U-Boot, which this profile never runs.

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

**3. VERDICT.txt's guest parenthetical is a POOLED total, printed under each leg.**
It reads `guest corroborates: saw 0x88b9 (673 PASS lines)` under frdm95 AND
`saw 0x88ba (673 PASS lines)` under orin — the same 673 both times. Re-derived:
the guest's PASS lines carry NO ethertype
(`ENET-LAB3 PASS: t=... peers=1/1 validated=1 beat=1 loss=0 ...`), so 673 is an
undifferentiated total across both legs and CANNOT be split. ⚠️ DO NOT SUM THEM
(1346 is not a number in this run) and do not read each leg as independently
corroborated 673 times. ⭐ 673 is REAL AND TRUE and attached to a claim it does not
measure — the same class as error 2, one level finer, and again nothing looks wrong
because it is a correct count of something. The scorer is fixed
(tools/score-real-silicon.py) so future runs label it; THIS FILE's VERDICT.txt still
carries the old wording because it is the artifact of a run that already happened
and is not being retro-edited.
Report 673 ONCE, as a pooled guest-side figure. The load-bearing numbers are the
board-side 391 and 380, whose per-leg attribution IS sound.

**4. THE TWO BOARD COUNTS WERE NOT MEASURED OVER EQUAL WINDOWS.** 391 (FRDM) vs
380 (Orin) has been quoted as a straight pair. The lab declares BOTH silicon nodes
at `start_at: 30`, but the coordinator awaited each arrival in turn — and a silicon
arrival deliberately waits 2s to prove its beacon started — so they actually began
at t+30.0 and t+33.1. At 200 ms/beacon that accounts for the direction and most of
the 11-frame gap; ~5 frames (~1s) remain unaccounted and are NOT explained here.
⚠️ I published a cause for that gap ("sudo -n over ssh adds latency") twice before
testing it. Reading the arrival loop refutes it: the gap is serialization plus a
hardcoded sleep in my own code. A DEFAULT EXPLANATION IS NOT A HYPOTHESIS — it
arrives already believed, so it gets written into the record rather than tested.
The coordinator now gathers equal-`start_at` arrivals concurrently, so a FUTURE run
of this lab will not have the offset. THIS run's numbers are unchanged and remain
correct as board-side sighting counts; they are simply not a like-for-like
comparison, and nothing in the result depends on their being one.

## What this does NOT show
Nothing here speaks to the negative control (that is a separate run, and only 2 of
its 4 observed clean runs have preserved transcripts). Nothing here is a claim about
U-Boot, which this profile never runs.

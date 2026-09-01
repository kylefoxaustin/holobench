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

**5. THE GUEST'S `0 CORRUPT` IS A BARE NULL IN THIS CONFIGURATION.** Both boards'
`0 CORRUPT` are backed by a local positive control: prove-oracle-bites.sh plants
broken magic and the board logs CORRUPT=30, so that counter demonstrably fires
here. The GUEST's `ENET-LAB3 CORRUPT: 0` has no such local control — nothing
malformed has ever been sent to the guest in THIS lab, so the zero is supported by
INHERITANCE, not by exercise.
The control does exist upstream: 95emulator's tests/enet-lab3/run.sh (at pinned
commit d10d314a, lines 292-337) has an IMPOSTOR mode emitting 1000-byte frames with
a valid 64-byte prefix and asserts the guest reports `CORRUPT ... wrong length`. Same
source file, so the DETECTOR LOGIC is proven. What is untested is whether it fires
through THIS plumbing — macvtap and the 2-port artifact rather than their mcast
1-port harness.
⭐ Prompted by qualcomm: A NULL BETWEEN TWO POINTS IS NOT A NULL ABOUT THE VARIABLE.
"0 corrupt" earns its meaning only next to a demonstration that the counter can be
made non-zero. Two of the three counters here have that; the third borrows it.
⚠️ UNCLOSED as of 2026-08-27, dated rather than argued away. Closing it needs a
malformed frame written to the guest's macvtap during a live lab run — buildable
from tools/prove-macvtap-guest.sh, not built.

**6. THE QEMU BINARY BEHIND THIS RESULT NO LONGER EXISTS AT ITS PATH (noted 2026-09-01).**
The profile pins md5 `748c91ee7e1746873937f74e4269abb8` for
`95emulator/build/qemu-system-aarch64`. That file was rebuilt in place on 2026-08-30
16:10 and now hashes `4c573eaeecbe0d9bdfbf1586658350f6`. Same path, different build.

⚠️ CORRECTION, same day, to the first version of this note: I originally attributed the new
binary to commit `4721c743b74` (branch `imx95-v2-clean`) because that is what the sibling
checkout had at HEAD. That was an inference from the checkout, not a measurement of the
binary, AND IT IS WRONG. Checked properly:

    binary mtime       2026-08-30 16:10
    checked-out HEAD   4721c743b74, dated 2026-08-25   ← OLDER than the binary
    imx95-v2.5.0 tag   9af44bc7d63, dated 2026-09-01   ← newer, and not checked out

⭐ THE BINARY CORRESPONDS TO NO REF. It is not HEAD and it is not the tag; it is an
artifact of a worktree state that no longer exists. That is materially worse than a stale
pin — a stale pin can be re-earned against a named build, but this one cannot be
regenerated from anything fetchable, so re-pinning to it would bless something
unreproducible. The refusal to re-pin was correct for a weaker reason than the real one.
⭐ THIS IS THE PIN WORKING, NOT FAILING. It is the exact scenario the pin was added for:
95emulator rebuilds often, and without a pin the binary behind a published number can be
replaced with nothing saying so. The lab now REFUSES to launch on drift, which is correct.
⚠️ WHAT IT MEANS FOR THIS BUNDLE: the numbers here remain what was measured — they were
produced by the pinned build, and nothing retroactively changes that. What is no longer
available is REPRODUCTION from the current tree: re-running this lab today requires
rebuilding the pinned commit or re-earning the pin against the new build by re-running
the lab and re-validating. Until someone does that, treat this bundle as a record rather
than a recipe.
⚠️ THE PIN HAS DELIBERATELY NOT BEEN UPDATED. Re-pinning to whatever is on disk would
silently bless a binary nobody validated, which is the only thing a pin exists to prevent.

## What this does NOT show
Nothing here speaks to the negative control (that is a separate run, and only 2 of
its 4 observed clean runs have preserved transcripts). Nothing here is a claim about
U-Boot, which this profile never runs.

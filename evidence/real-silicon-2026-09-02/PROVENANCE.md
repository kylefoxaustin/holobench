# Real-silicon L2 lab — 2026-09-02 — the run that EARNED the pin

`pass=2 fail=0 inconclusive=0`, graded on the REAL BOARDS' own consoles.
Transcript: `scorer-transcript.log` (the scorer writes its own now; it was the one
script of three that did not).

| leg | wire | board's own console | result |
|---|---|---|---|
| frdm95 | LAN, macvtap on `enp6s0` | `PASS=381 rx=381 CORRUPT=0` | ✅ |
| orin | USB cdc_ncm, macvtap on `enx42b8036560ca` | numbering reached `PASS #837`, `CORRUPT=0` | ✅ |

## What this run pinned

Everything it ran with, because a passing run is exactly when a pin is earned:

    qemu-system-aarch64   d929e6a5b42b4e3afdea2683245eb693   imx95-v2.6.0 (2bde22d6644)
    Image                 a6b35df56953424b6adec26b4ba45af0
    imx95-19x19-evk.dtb   2dd6f503b1b3ab4190cab6f3d04587ca
    lab3-2port.cpio.gz    0c10d5f89a0563ebc84c6ba74f14fa71   (already pinned)

Before this, ONLY the initrd was pinned — a changed kernel, dtb or M33 firmware would
have gone unnoticed.

## ⚠️ What it does NOT certify

**1. THE PIN IS FOR THIS LAB, NOT FOR THE BINARY.** This lab is ENETC and raw L2: no ISP,
no camera, no video device in the profile. 95emulator reported the same day that v2.6.0
carries a known ISP defect — `TRIG_CAM0.TRIGGER` is latched rather than self-clearing and
`regmap_update_bits` elides the unchanged write, so the SECOND frame in a session hangs
forever and presents as an intermittent fault. It cannot reach this lab. It is recorded
here so the pin is never read as a general blessing.

**2. 🛑 THE ORIN LEG WAS CONTAMINATED BY MY OWN CORPSE, AND ITS COUNTS MUST NOT BE
CITED.** Found during the post-run corpse check, after this bundle was first written.

    pid 735507 on the Orin, started Tue Aug 25 13:58:01 — EIGHT DAYS before this run
    /usr/bin/python3 /usr/local/sbin/l2beacon.py l4tbr0 0x88ba 0x88b7
    same interface, same ethertype, same watch list as this run's orin node

So during this run there were **TWO** beacons on `l4tbr0` transmitting `0x88ba` and both
watching `0x88b7`. The guest saw `0x88ba` from two sources; the board's PASS lines came
from two processes writing the same log path.

⭐ THAT ALSO UNDERMINES MY FIRST EXPLANATION OF THE GAP BELOW. I attributed the missing
block to a truncated ssh read. A second writer to the same file is at least as good an
explanation and I CANNOT DISTINGUISH THEM from what I have. The gap is real; my stated
cause for it was a guess wearing the clothes of a finding, and it is withdrawn.

⚠️ CONSEQUENCE: **the orin leg's numbers are uninterpretable.** Not wrong — unattributable.
Nothing here can say which beacon produced which line. The leg still shows real hardware on
a real USB wire receiving the emulated board's frames with `CORRUPT=0`, but no count from it
may be quoted.

⭐ **THE frdm95 LEG IS CLEAN AND IS WHAT THE PIN RESTS ON.** Checked: no stale beacon on the
FRDM, `PASS=381 rx=381 CORRUPT=0`, one transmitter, one receiver. That leg alone establishes
the claim the pin needs — an emulated i.MX95 on imx95-v2.6.0 exchanging body-validated raw
L2 frames with real silicon that accepted them. The USB leg was corroboration and is
downgraded to "reached the far end", nothing quantitative.

⚠️ AND IT IS A LAW 2 FAILURE OF MINE, EIGHT DAYS OLD. The August run left that beacon
running and my release said no resources were held. That was false and nothing caught it —
the corpse list I published counted macvtaps on the host and never asked the boards what was
still running on them. A corpse check that only looks where you can see is not a corpse check.

**3. ⚠️ THE ORIN'S COUNT IN THIS BUNDLE IS ALSO INCOMPLETE, AND THAT IS A COLLECTION DEFECT OF
MINE, NOT A WIRE RESULT.** `orin.board.log` holds 770 `L2BEACON PASS` lines, but the
beacon numbers its own passes and they run `#1..#837` with exactly one contiguous block
`#386..#452` (67 lines) absent. The board asserted 837 passes; my collection preserved
770 of them. Contiguous loss points at a truncated read during the ssh fetch, not at
scattered line drops, and not at frames failing on the wire — `CORRUPT=0` and the
numbering is monotonic across the gap.
So: cite **837 as the board's own assertion** and **770 as what this bundle preserves**.
Do not cite 770 as a frame count.

⭐ The verdict is unaffected — a leg passes on `PASS > 0` and `CORRUPT = 0`, both
established many times over — but the *number* would have been wrong, and I would have
published it. Found by checking the beacon's sequence numbers against the line count
rather than trusting either alone.

**4. The August bundle was checked for the same signature and is CLEAN** —
`evidence/real-silicon-2026-08-25/` has zero missing sequence numbers on both boards, so
the previously published 391 / 380 stand exactly as measured.

**5. `m33_image_M2.elf` is NOT hash-pinned.** It is now a real file rather than a symlink
into 95emulator's upstream-prep tree (which rebuilds), so it can no longer drift under a
run — but `boot.pin` only covers `boot.artifacts`, and the M33 firmware is supplied via
`qemu.extra_args`, which has no pin slot. That is a real gap, dated and unclosed rather
than argued away. Its bytes are a non-redistributable NXP BSP artifact; `assets/` is
gitignored and they never leave the host.

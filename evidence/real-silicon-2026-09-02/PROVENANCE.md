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

**2. ⚠️ THE ORIN'S COUNT IN THIS BUNDLE IS INCOMPLETE, AND THAT IS A COLLECTION DEFECT OF
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

**3. The August bundle was checked for the same signature and is CLEAN** —
`evidence/real-silicon-2026-08-25/` has zero missing sequence numbers on both boards, so
the previously published 391 / 380 stand exactly as measured.

**4. `m33_image_M2.elf` is NOT hash-pinned.** It is now a real file rather than a symlink
into 95emulator's upstream-prep tree (which rebuilds), so it can no longer drift under a
run — but `boot.pin` only covers `boot.artifacts`, and the M33 firmware is supplied via
`qemu.extra_args`, which has no pin slot. That is a real gap, dated and unclosed rather
than argued away. Its bytes are a non-redistributable NXP BSP artifact; `assets/` is
gitignored and they never leave the host.

#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""One ARM of the SDCLK_AUTO_GATE experiment: boot imx95-evk-sd and report whether
the uSDHC path enumerates.

    usage:  tools/sd-quirk-arm.py [--binary /path/to/qemu-system-aarch64] [--label X]

WHY THIS IS A TOOL AND NOT A ONE-OFF: an assertion you cannot re-run is a receipt
for one you ran once. 93emulator answered this question on the 93 by removing the
quirk and comparing against a baseline; the 95-side complement needs the same two
arms, run the same way, or the comparison is between a measurement and a memory.

⭐ THE DETECTOR IS THE INTERESTING PART. The obvious probe — grep for "mmcblk0" —
IS WRONG, and it produced a false positive on the first run of this experiment:
the kernel COMMAND LINE contains `root=/dev/mmcblk0p2`, so the token appears at
t=0.000 whether or not any card is ever found. A wait loop keyed on it exits
before the controller has even probed, and reports success.

    A GREP HIT IS NOT A FINDING UNTIL YOU READ WHAT IT HIT.

So ENUM matches only strings the mmc SUBSYSTEM emits — "mmcN: new ... card",
"mmcblkN: mmcM:ADDR", "mmcblkN: pN" — none of which can appear in a cmdline.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
from holobench.profiles.loader import load_profile, default_asset_dir   # noqa: E402
from holobench.session.manager import SessionManager                    # noqa: E402

# Cannot match the kernel cmdline. That is the whole design of these two regexes.
ENUM = re.compile(r"mmc\d+: new .*(SD|MMC) card|mmcblk\d+: mmc\d+:|mmcblk\d+: p\d")
FAIL = re.compile(r"Kernel panic|VFS: Unable to mount|mmc\d+: (error|timeout|Timeout)")
# A short base dir: a UNIX socket path is capped at 108 bytes and QEMU exits
# rather than truncate. Discovered the hard way on a scratchpad path.
BASE = Path("/tmp/hb-sd")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--binary", help="QEMU to test (default: the profile's)")
    ap.add_argument("--label", default="ARM", help="what this arm is (e.g. 'no-quirk')")
    ap.add_argument("--timeout", type=float, default=300.0)
    a = ap.parse_args()

    p = load_profile("imx95-evk-sd")
    if a.binary:
        p.qemu.binary = a.binary
        p.qemu.binary_pin = None      # an experimental arm is explicitly unpinned
    binpath = Path(p.qemu.binary)
    if not binpath.is_file():
        print(f"🛑 ABORTED — NO VERDICT: no QEMU at {binpath}")
        return 2

    print("═" * 70)
    print(f" SD-QUIRK ARM: {a.label}")
    print("═" * 70)
    print(f"  binary     : {binpath}")
    print(f"  binary md5 : {hashlib.md5(binpath.read_bytes()).hexdigest()}")
    print(f"  profile    : imx95-evk-sd   machine: {p.qemu.machine}")

    BASE.mkdir(parents=True, exist_ok=True)
    mgr = SessionManager(base_dir=BASE)
    s = await mgr.launch(p, asset_dir=default_asset_dir(p.id))
    log = Path(s.console_log())
    text = ""
    deadline = asyncio.get_event_loop().time() + a.timeout
    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(2)
        text = log.read_text(errors="replace") if log.is_file() else ""
        if ENUM.search(text) or FAIL.search(text):
            break

    enumerated, failed = bool(ENUM.search(text)), bool(FAIL.search(text))
    ts = (re.findall(r"\[\s*([\d.]+)\]", text) or ["?"])[-1]
    print(f"  console    : {len(text)} bytes, last kernel timestamp {ts}")
    print()
    print("  ── every mmc/sdhci line the guest printed (the evidence, not a summary) ──")
    hits = [l.strip() for l in text.splitlines()
            if re.search(r"mmc|sdhci|usdhc|SDHCI", l) and "command line" not in l]
    for line in hits[:16]:
        print(f"    | {line[:100]}")
    if not hits:
        print("    | (NONE — the uSDHC driver produced no output at all)")
    print()
    if failed:
        print(f"  ❌ {a.label}: the SD path FAILED — see the lines above.")
    elif enumerated:
        print(f"  ✅ {a.label}: the SD path ENUMERATED.")
    else:
        print(f"  ⚠️  {a.label}: INCONCLUSIVE — neither enumeration nor failure within "
              f"{a.timeout:.0f}s. A run that did not reach a verdict is not a result.")
    print("\n  ⚠️  ONE ARM IS NOT AN EXPERIMENT. This is only meaningful next to the "
          "other\n     arm, run the same way, on the same profile and disk.")
    await mgr.destroy(s.id)
    return 0 if enumerated and not failed else (1 if failed else 2)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

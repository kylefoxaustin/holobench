#!/usr/bin/env python3
"""Print each lab node's INVOCATION FINGERPRINT — the value for `qemu.argv_pin`.

⭐ A RUNNER IS PART OF THE ARTIFACT. (rt1180emulator, 2026-07-14.)

`boot.pin` hashes the BINARY. It says nothing about the command line that runs it, and a
lab's result is a function of BOTH. rt1180's board-farm script shipped `-device tmp105`
with no `bus=`; it bound to whichever bus QEMU saw LAST, and when new LPI2C instances were
added, "last" stopped being the one the firmware drove. Two FAILs on aarch64, green on x86,
and the difference was never the architecture — an afternoon nearly spent hunting a phantom
endianness bug in a model that was fine.

    "We test the MODEL on two machines and the HARNESS on one — so the harness is exactly
     where untested code goes to live."

WHY THIS RUNS THE REAL COORDINATOR AND NOT A REIMPLEMENTATION OF IT. A lab node's argv is
not a function of its profile alone: the coordinator supplies the NIC override (segment,
MAC, `model=`), the fabric dtb, and the kernel append. Recomputing that here would mean
maintaining a SECOND copy of the wiring logic — and a pin derived from a second
implementation of the thing it is pinning would be pinning the wrong thing, silently, and
would agree with itself forever. So we drive the ACTUAL LabCoordinator against a manager
that records what it is asked to launch and never starts QEMU.

Usage:  tools/argv-pin.py [lab-id]
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from holobench.labs.coordinator import LabCoordinator          # noqa: E402
from holobench.labs.loader import load_lab                      # noqa: E402
from holobench.profiles.loader import default_asset_dir         # noqa: E402
from holobench.session.manager import Session                   # noqa: E402

LAB_ID = sys.argv[1] if len(sys.argv) > 1 else "mcx-rt1180-95-l2"


class _DryRunManager:
    """Records what the coordinator asks for; builds a real Session; boots nothing."""

    def __init__(self) -> None:
        self.base_dir = None
        self.nodes: dict[str, Session] = {}
        self._n = 0

    async def launch(self, profile, **kw):
        self._n += 1
        kw.pop("owner", None)
        kw.pop("minutes", None)
        sess = Session(
            profile,
            asset_dir=kw.pop("asset_dir", None) or default_asset_dir(profile.id),
            session_id=f"{profile.id}-dryrun",
            **kw,
        )
        self.nodes[profile.id] = sess
        return sess

    async def destroy(self, sid):  # the coordinator tears down on stop
        return None

    def get(self, sid):
        raise KeyError(sid)


async def main() -> int:
    lab = load_lab(LAB_ID)

    # FLATTEN THE TIMELINE. The coordinator honors start_at/stop_at, so a faithful dry run
    # of a staggered lab would sit here for its full 450s horizon doing nothing. An
    # invocation does not depend on WHEN it is issued — the schedule is orthogonal to the
    # command line — so we zero it. (Stated rather than done quietly: this is the one place
    # the dry run deliberately differs from the real launch, and if a future change ever
    # makes argv depend on the schedule, this line becomes a lie and must go.)
    lab = lab.model_copy(deep=True)
    for n in lab.nodes:
        n.start_at, n.stop_at, n.rejoin_at = 0.0, None, None

    mgr = _DryRunManager()
    coord = LabCoordinator(mgr)
    try:
        await coord.launch(lab)
    except Exception as exc:            # a dry run must not pretend it succeeded
        print(f"coordinator wiring failed: {exc}", file=sys.stderr)
        return 1

    print(f"# {lab.id} — invocation fingerprints ({len(mgr.nodes)} nodes)")
    print("#")
    print("# Paste into each profile as:")
    print("#     qemu:")
    print("#       argv_pin: \"<value>\"")
    print("#")
    for node in lab.nodes:
        sess = mgr.nodes.get(node.profile)
        if sess is None:
            print(f"{node.name:8} {node.profile:26} (not wired)")
            continue
        fp = sess.invocation_fingerprint()
        pinned = sess.profile.qemu.argv_pin
        mark = "  " if pinned is None else ("✅" if pinned == fp else "❌ DRIFTED")
        print(f"{node.name:8} {node.profile:26} {fp} {mark}")
        if pinned and pinned != fp:
            print(f"         pinned: {pinned}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

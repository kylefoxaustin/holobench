# SPDX-License-Identifier: GPL-2.0-or-later
"""Macvtap endpoints — how an emulated board gets onto a REAL wire.

Every lab before this one wired its boards with `-nic socket,mcast=`. That is a
genuine L2 broadcast domain and it has found genuine bugs, but it is a
QEMU-INTERNAL socket: no frame on an mcast segment has ever left the host. So no
result on one can say anything about physical hardware, and the strongest claim
available was "our models talk to each other."

This module is the whole difference between that claim and the one the lab is
after: **"the model is indistinguishable from the silicon, TO THE SILICON."**

────────────────────────────────────────────────────────────────────────────────
THE MECHANISM, AND WHY IT IS STILL PRIME-DIRECTIVE CLEAN

    ip link add link enp6s0 name hb-mvt0 type macvtap mode bridge
    ip link set hb-mvt0 up
    -> a character device /dev/tap<ifindex>

QEMU is then handed that ALREADY-OPEN FILE DESCRIPTOR:

    -nic tap,fd=<N>,model=<the board's own NIC>,mac=<pinned>

`-nic tap,fd=` is stock QEMU. No patch, no custom device, no forked binary, and
nothing added to a machine model "for Holobench" — the board still presents its
own modeled NIC and has no idea the backend is a real wire rather than a socket.
That is exactly the boundary CLAUDE.md §2 draws: holobench changes the BACKEND,
never the MODEL.

────────────────────────────────────────────────────────────────────────────────
WHY MACVTAP AND NOT A BRIDGE — this is a safety property, not a preference

claude-connect chose it and the reasoning is worth keeping at the point of use:
macvtap is ADDITIVE. It adds a virtual endpoint to enp6s0 without rebuilding
enp6s0's own configuration. A bridge would re-home the host's address onto a new
device, and a botched attempt would take skippy off the network.

And enp6s0 is **not skippy's default route** (wlo1 is). So even total failure of
everything in this file cannot cost the box its connectivity. That property is
worth preserving in the lab's design rather than being a lucky fact about the
first run — which is why `wire` is a per-segment field a lab must state out loud.

────────────────────────────────────────────────────────────────────────────────
⚠️ THE TWO THINGS A CALLER MUST NOT FORGET

1. **A macvtap can LOCALLY SWITCH.** Two macvtaps on the same lower device talk
   to each other without a frame reaching the NIC. So a segment of nothing but
   emulated nodes can go fully green WITH THE CABLE UNPLUGGED. `Lab` refuses to
   validate such a segment (see models.py) — the fix is a `kind: silicon` member,
   whose console cannot be fooled because it is on the far side of a cable.

2. **macvlan ISOLATES THE LOWER DEVICE from its own children.** A host capture on
   enp6s0 may show NOTHING while the wire is working perfectly. That is a
   property of macvlan, not evidence about the lab. Anything scoring a run must
   treat an empty host-side capture as INCONCLUSIVE, never as a FAIL — the real
   board's own receive count is the better capture point anyway, because skippy
   is on the near side of the cable and the board is not.

────────────────────────────────────────────────────────────────────────────────
ROOT, AND WHY THE FAILURE IS LOUD

Creating a macvtap needs root, and on skippy sudo is password-required. Kyle's
ruling (2026-08-22): the runner shells an explicit, logged sudo step and
**REFUSES TO LAUNCH** if it cannot. It does not fall back to an mcast segment and
it does not carry on quietly — a lab that silently downgrades from a real wire to
an internal socket would still print a green, and that green would be a lie about
the one thing the lab exists to prove.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field


class MacvtapError(Exception):
    """Raised when a real-wire endpoint cannot be created, opened or torn down.

    Always fatal to a launch. There is no degraded mode: an emulated node that
    fails to reach the wire must not run at all, because a lab that scores it
    anyway is scoring a claim it did not test.
    """


def _run(argv: list[str], *, sudo: bool) -> subprocess.CompletedProcess:
    cmd = (["sudo", "-n"] + argv) if sudo else argv
    return subprocess.run(cmd, capture_output=True, text=True)


@dataclass
class MacvtapEndpoint:
    """One emulated node's endpoint on a real host NIC."""
    name: str            # the macvtap link, e.g. "hb-mvt0"
    lower: str           # the host NIC it hangs off, e.g. "enp6s0"
    ifindex: int
    dev: str             # "/dev/tap<ifindex>"
    mac: str
    fd: int = -1

    def open_fd(self) -> int:
        """Open the char device QEMU will be handed.

        The fd is opened HERE, in the coordinator, and passed to the child
        already open — that is what `-nic tap,fd=` means. It must be marked
        inheritable or the exec'd QEMU gets a closed descriptor and fails in a
        way that looks like a NIC bug rather than a plumbing one.
        """
        try:
            self.fd = os.open(self.dev, os.O_RDWR)
        except OSError as exc:
            raise MacvtapError(
                f"cannot open {self.dev} for macvtap '{self.name}': {exc}. "
                f"The link exists but its character device is unreadable — "
                f"usually ownership. REFUSING TO LAUNCH rather than run this "
                f"node on a backend that is not the wire it claims to be."
            ) from exc
        os.set_inheritable(self.fd, True)
        return self.fd

    def close_fd(self) -> None:
        if self.fd >= 0:
            try:
                os.close(self.fd)
            finally:
                self.fd = -1

    def nic_spec(self, *, model: str | None) -> str:
        """The stock `-nic` backend spec for this endpoint.

        The MAC is the macvtap's own. It is pinned deliberately: the real board
        on the far end identifies peers by source address, and an auto-generated
        MAC that does not match the endpoint the kernel actually created would
        make the guest's frames arrive from an address nothing expects.
        """
        if self.fd < 0:
            raise MacvtapError(
                f"macvtap '{self.name}' has no open fd — open_fd() must run "
                f"before its argv is built")
        spec = f"tap,fd={self.fd},mac={self.mac}"
        if model:
            spec += f",model={model}"
        return spec


@dataclass
class MacvtapPool:
    """Creates, tracks and — crucially — REAPS the run's real-wire endpoints.

    LAW 2, AND IT IS NOT CEREMONY HERE. A leaked macvtap is not inert litter: it
    is a live endpoint on a shared physical LAN, with a MAC the next run's peers
    may still have in their FDBs, quietly receiving frames a later lab thinks it
    is sending somewhere else. `reap()` runs on every exit path and RETURNS THE
    CORPSE LIST so the caller can print what it destroyed rather than assert it
    was tidy.
    """
    endpoints: list[MacvtapEndpoint] = field(default_factory=list)
    _counter: int = 0

    # ── preflight: refuse a verdict rather than produce a bad one ────────────
    @staticmethod
    def preflight(wire: str) -> None:
        """Everything that must be true BEFORE anything is created.

        A setup that cannot work must never reach the scoring code. This is the
        lesson from the lab's own proof script, which printed "paths proven: 2,
        failed: 0" while all three of its steps had failed — because it tested
        for the ABSENCE of a failure string, and a step that never ran emitted
        no failure string at all.
        """
        if not shutil.which("ip"):
            raise MacvtapError("no `ip` command on this host — cannot create a macvtap.")
        if not os.path.exists(f"/sys/class/net/{wire}"):
            raise MacvtapError(
                f"no such host NIC '{wire}'. A macvtap segment names the real wire "
                f"it attaches to; this one does not exist on this box.")
        oper = f"/sys/class/net/{wire}/operstate"
        if os.path.exists(oper):
            with open(oper) as fh:
                state = fh.read().strip()
            if state not in ("up", "unknown"):
                raise MacvtapError(
                    f"host NIC '{wire}' is {state}, not up. A real-wire lab on a down "
                    f"NIC would produce a red that says nothing about the model.")
        probe = _run(["ip", "link", "show", wire], sudo=False)
        if probe.returncode != 0:
            raise MacvtapError(f"cannot inspect '{wire}': {probe.stderr.strip()}")
        # The sudo check is LAST and explicit, so its failure message is about
        # privilege rather than being mistaken for a missing interface.
        if _run(["true"], sudo=True).returncode != 0:
            raise MacvtapError(
                "passwordless sudo is not available, and creating a macvtap needs root. "
                "REFUSING TO LAUNCH. Run the lab from a shell where `sudo -n` works, or "
                "pre-create the endpoints. holobench will NOT quietly fall back to an "
                "mcast segment: that would still print a green, and the green would be "
                "a lie about the only thing this lab exists to prove.")

    def create(self, wire: str, *, mode: str = "bridge") -> MacvtapEndpoint:
        """Add one macvtap endpoint on `wire` and return it (fd not yet open)."""
        name = f"hb-mvt{self._counter}"
        self._counter += 1
        # A stale endpoint from a killed run is a live device on a shared LAN.
        # Remove it rather than colliding with it.
        if os.path.exists(f"/sys/class/net/{name}"):
            _run(["ip", "link", "del", name], sudo=True)
        add = _run(["ip", "link", "add", "link", wire, "name", name,
                    "type", "macvtap", "mode", mode], sudo=True)
        if add.returncode != 0:
            raise MacvtapError(
                f"could not create macvtap '{name}' on '{wire}': "
                f"{add.stderr.strip() or add.stdout.strip()}")
        up = _run(["ip", "link", "set", name, "up"], sudo=True)
        if up.returncode != 0:
            _run(["ip", "link", "del", name], sudo=True)
            raise MacvtapError(f"could not bring up macvtap '{name}': {up.stderr.strip()}")
        try:
            with open(f"/sys/class/net/{name}/ifindex") as fh:
                ifindex = int(fh.read().strip())
            with open(f"/sys/class/net/{name}/address") as fh:
                mac = fh.read().strip()
        except OSError as exc:
            _run(["ip", "link", "del", name], sudo=True)
            raise MacvtapError(f"macvtap '{name}' created but unreadable: {exc}") from exc

        dev = f"/dev/tap{ifindex}"
        if not os.path.exists(dev):
            _run(["ip", "link", "del", name], sudo=True)
            raise MacvtapError(
                f"macvtap '{name}' exists but {dev} does not. Without the character "
                f"device there is nothing to hand QEMU.")
        # QEMU runs as the invoking user; the char device is created root-owned.
        _run(["chown", f"{os.getuid()}:{os.getgid()}", dev], sudo=True)

        ep = MacvtapEndpoint(name=name, lower=wire, ifindex=ifindex, dev=dev, mac=mac)
        self.endpoints.append(ep)
        return ep

    def reap(self) -> list[str]:
        """Destroy every endpoint this pool made. Returns the corpse list.

        Returning what was destroyed — rather than logging "cleaned up" — is the
        difference between evidence and a claim. The caller prints this.
        """
        corpses: list[str] = []
        for ep in self.endpoints:
            ep.close_fd()
            if os.path.exists(f"/sys/class/net/{ep.name}"):
                res = _run(["ip", "link", "del", ep.name], sudo=True)
                corpses.append(
                    f"{ep.name} (on {ep.lower}, {ep.dev})"
                    + ("" if res.returncode == 0 else f" — DELETE FAILED: {res.stderr.strip()}"))
        self.endpoints = []
        return corpses

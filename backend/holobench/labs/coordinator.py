# SPDX-License-Identifier: GPL-2.0-or-later
"""Fabric coordinator — launches a whole lab and wires its nodes together.

Each node is just an ordinary Session (one QEMU + QMP + serial), launched through
the existing SessionManager. The coordinator's only extra job is the *fabric*:
for every `eth` link it allocates an isolated multicast group (a virtual L2
switch) and points each member board's modeled NIC at it via a stock
`-nic socket,mcast=...` backend (SessionManager.launch(nic_override=...)), giving
every member a unique MAC. No machine-model changes, no custom devices — exactly
the mechanism proven in the v3.0-α PoC (two i.MX91s pinging over one mcast group).

USB links are wired the same way: each profile's `usb:` block carries that
board's usbredir role (host = stock `-device usb-redir` importer; device =
usbredir exporter/listener), the coordinator owns one unix socket per link, and
the host-side backend reconnects so launch order is forgiving. Validated
end-to-end (2026-07-02): the gateway-lab i.MX93 host enumerates the MCXN947 CDC
gadget at HIGH speed and binds /dev/ttyACM0 (docs/TOPOLOGIES.md §USB).
"""
from __future__ import annotations

import os
from enum import Enum
from pathlib import Path
from typing import Optional

from ..profiles.loader import default_asset_dir, load_profile
from ..session.manager import DEFAULT_BASE_DIR, SessionError, SessionManager
from .models import Lab, LabError

# USB inter-board links: the per-board usbredir roles live in each profile's `usb:`
# block (host = the stock `-device usb-redir` importer/client; device = the usbredir
# exporter/listener), the coordinator owns one short unix socket per link, and the
# args are the real transport confirmed on the bus by the 93<->MCX runs. VALIDATED
# end-to-end through this coordinator (2026-07-02): the gateway-lab i.MX93 host
# enumerates the MCXN947 CDC gadget at HIGH speed and binds /dev/ttyACM0. Launchable
# like eth labs (no longer env-gated). See docs/TOPOLOGIES.md §USB.


def _usb_args(role, cid: str, sock: str) -> list[str]:
    """Raw QEMU args for one usbredir role: a `-chardev`, plus the importer's
    `-device usb-redir` (host end) or the exporter's `-global` (device end),
    with `{id}`/`{path}` filled from the allocated chardev id + link socket."""
    args = ["-chardev", role.chardev.format(id=cid, path=sock)]
    if role.device:
        args += ["-device", role.device.format(id=cid, path=sock)]
    if role.glob:
        args += ["-global", role.glob.format(id=cid, path=sock)]
    return args

# Multicast fabric: one group address per live segment (isolated L2 domains), a
# shared port. 230.0.0.0/24 is an administratively-scoped, unrouted group block —
# traffic stays on the host. Overridable for odd host network policies.
_MCAST_BASE = os.environ.get("HOLOBENCH_FABRIC_MCAST_BASE", "230.0.0")
_MCAST_PORT = int(os.environ.get("HOLOBENCH_FABRIC_MCAST_PORT", "12340") or 12340)
_MCAST_FIRST_OCTET = 10  # 230.0.0.10 was the PoC group; start there


class LabState(str, Enum):
    CREATED = "created"
    LAUNCHING = "launching"
    RUNNING = "running"
    PARTIAL = "partial"   # some nodes up, some failed
    STOPPED = "stopped"
    FAILED = "failed"


def _mac(lab_idx: int, node_idx: int, nic_idx: int) -> str:
    """Deterministic, collision-free locally-administered MAC. 52:54:00 is QEMU's
    OUI; the last three octets encode (lab launch, node, nic) so no two fabric
    NICs ever share a MAC (the v3.0-α gotcha — default MACs collided)."""
    return f"52:54:00:{lab_idx & 0xff:02x}:{node_idx & 0xff:02x}:{nic_idx & 0xff:02x}"


class RunningLab:
    """A launched lab: the spec + the node->session-id map + lab-level state."""

    def __init__(self, lab: Lab, lab_idx: int) -> None:
        self.lab = lab
        self.lab_idx = lab_idx
        self.state = LabState.CREATED
        self.node_sessions: dict[str, str] = {}   # node name -> session id
        self.node_errors: dict[str, str] = {}     # node name -> launch error
        # node name -> list of "segment@group:port" it joined (for status/UI edges)
        self.node_links: dict[str, list[str]] = {}
        self.usb_socks: list[str] = []            # per-usb-link sockets (for teardown)

    @property
    def id(self) -> str:
        return self.lab.id

    def view(self) -> dict:
        nodes = []
        for n in self.lab.nodes:
            nodes.append({
                "name": n.name,
                "profile": n.profile,
                "session_id": self.node_sessions.get(n.name),
                "error": self.node_errors.get(n.name),
                "segments": self.node_links.get(n.name, []),
            })
        links = []
        for link in self.lab.links:
            if link.type == "eth":
                links.append({"type": "eth", "segment": link.segment,
                              "members": link.members})
            else:
                links.append({"type": "usb", "host": link.host,
                              "device": link.device})
        return {
            "id": self.lab.id,
            "display_name": self.lab.display_name,
            "description": self.lab.description,
            "state": self.state.value,
            "nodes": nodes,
            "links": links,
        }


class LabCoordinator:
    """Owns the live labs, layered on a SessionManager. One per app."""

    def __init__(self, manager: SessionManager) -> None:
        self.manager = manager
        self._labs: dict[str, RunningLab] = {}
        self._lab_counter = 0
        self._used_groups: set[int] = set()

    # --- fabric allocation -------------------------------------------------
    def _alloc_group(self) -> str:
        """Lowest free 230.0.0.<octet> group, so concurrent segments don't bridge."""
        octet = _MCAST_FIRST_OCTET
        while octet in self._used_groups:
            octet += 1
            if octet > 250:
                raise LabError("no free fabric multicast group (too many segments)")
        self._used_groups.add(octet)
        return f"{_MCAST_BASE}.{octet}"

    def _free_group(self, group: str) -> None:
        try:
            self._used_groups.discard(int(group.rsplit(".", 1)[1]))
        except (ValueError, IndexError):
            pass

    # --- lifecycle ---------------------------------------------------------
    async def launch(self, lab: Lab, *, owner: Optional[str] = None,
                     minutes: Optional[int] = None) -> RunningLab:
        if lab.id in self._labs:
            raise LabError(f"lab '{lab.id}' is already running")

        self._lab_counter += 1
        running = RunningLab(lab, self._lab_counter)
        running.state = LabState.LAUNCHING
        self._labs[lab.id] = running

        # 1) Allocate an isolated mcast group per eth segment.
        seg_group: dict[str, str] = {}
        node_idx = {n.name: i for i, n in enumerate(lab.nodes)}
        try:
            for link in lab.links:
                if link.type == "eth" and link.segment not in seg_group:
                    seg_group[link.segment] = self._alloc_group()

            # 1b) Load each node's profile once (need it for the NIC model= below
            # and the launch). A bad profile downs only that node, not the lab.
            node_profiles: dict[str, object] = {}
            for node in lab.nodes:
                try:
                    node_profiles[node.name] = load_profile(node.profile)
                except Exception as exc:
                    running.node_errors[node.name] = str(exc)

            # 2) Build each node's nic_override (one socket NIC per segment it joins).
            # Append model=<fabric_nic_model> when the board needs it to bind the
            # right modeled NIC (MCXN947 ENET-QoS, i.MX9 FEC); else QEMU auto-attaches.
            node_nics: dict[str, list[str]] = {n.name: [] for n in lab.nodes}
            for link in lab.links:
                if link.type != "eth":
                    continue
                group = seg_group[link.segment]
                for member in link.members:
                    nic_i = len(node_nics[member])
                    prof = node_profiles.get(member)
                    model = getattr(prof.net, "fabric_nic_model", None) if prof else None
                    spec = (f"socket,mcast={group}:{_MCAST_PORT},"
                            f"mac={_mac(running.lab_idx, node_idx[member], nic_i)}"
                            + (f",model={model}" if model else ""))
                    node_nics[member].append(spec)
                    running.node_links.setdefault(member, []).append(
                        f"{link.segment}@{group}:{_MCAST_PORT}")

            # 2b) Build each node's usb_override (one usbredir link at a time).
            # The DEVICE end is the exporter/listener, the HOST end is the stock
            # `-device usb-redir` client (reconnect makes launch order forgiving).
            # One short unix socket per link, owned by the coordinator under the
            # session base dir so both QEMU procs can reach it.
            sock_dir = self.manager.base_dir or DEFAULT_BASE_DIR
            node_usb: dict[str, list[str]] = {n.name: [] for n in lab.nodes}
            usb_i = 0
            for link in lab.links:
                if link.type != "usb":
                    continue
                hp = node_profiles.get(link.host)
                dp = node_profiles.get(link.device)
                hrole = getattr(getattr(hp, "usb", None), "host", None) if hp else None
                drole = getattr(getattr(dp, "usb", None), "device", None) if dp else None
                if not hrole or not drole:
                    missing = link.host if not hrole else link.device
                    need = "usb.host" if not hrole else "usb.device"
                    running.node_errors[missing] = (
                        f"usb link {link.host}->{link.device}: '{missing}' profile lacks "
                        f"a {need} role (confirm with the emulator session, §7)")
                    continue
                sock = str(sock_dir / f"usb-{lab.id}-{usb_i}.sock")
                cid = f"hbusb{usb_i}"
                node_usb[link.device] += _usb_args(drole, cid, sock)   # exporter listens
                node_usb[link.host] += _usb_args(hrole, cid, sock)     # importer connects
                running.usb_socks.append(sock)
                running.node_links.setdefault(link.host, []).append(
                    f"usb->{link.device}@{sock}")
                running.node_links.setdefault(link.device, []).append(
                    f"usb<-{link.host}@{sock}")
                usb_i += 1

            # 3) Launch each node as a Session with its fabric NICs.
            any_ok = False
            for node in lab.nodes:
                profile = node_profiles.get(node.name)
                if profile is None:
                    continue  # profile load already recorded in node_errors
                try:
                    asset_dir = default_asset_dir(profile.id)
                    nics = node_nics[node.name] or None
                    session = await self.manager.launch(
                        profile, asset_dir=asset_dir, owner=owner, minutes=minutes,
                        nic_override=nics, usb_override=(node_usb[node.name] or None),
                    )
                    session.lab_id = lab.id
                    session.lab_node = node.name
                    running.node_sessions[node.name] = session.id
                    any_ok = True
                except Exception as exc:  # one bad node shouldn't kill the lab
                    running.node_errors[node.name] = str(exc)
        except BaseException:
            # Allocation failed wholesale: free groups + any partial sessions.
            await self._teardown(running, seg_group)
            self._labs.pop(lab.id, None)
            running.state = LabState.FAILED
            raise

        if not any_ok:
            await self._teardown(running, seg_group)
            self._labs.pop(lab.id, None)
            running.state = LabState.FAILED
            raise LabError(
                f"lab '{lab.id}' failed to launch any node: {running.node_errors}"
            )
        running._seg_group = seg_group  # remember for teardown
        running.state = (LabState.RUNNING if not running.node_errors
                         else LabState.PARTIAL)
        return running

    async def _teardown(self, running: RunningLab, seg_group: dict[str, str]) -> None:
        for sid in list(running.node_sessions.values()):
            try:
                await self.manager.destroy(sid)
            except SessionError:
                pass
        for group in seg_group.values():
            self._free_group(group)
        for sock in getattr(running, "usb_socks", []):
            try:
                Path(sock).unlink()
            except OSError:
                pass

    async def stop(self, lab_id: str) -> None:
        running = self.get(lab_id)
        await self._teardown(running, getattr(running, "_seg_group", {}))
        running.state = LabState.STOPPED
        self._labs.pop(lab_id, None)

    def get(self, lab_id: str) -> RunningLab:
        if lab_id not in self._labs:
            raise LabError(f"no running lab '{lab_id}'")
        return self._labs[lab_id]

    def peek(self, lab_id: str) -> Optional[RunningLab]:
        return self._labs.get(lab_id)

    def list(self) -> list[RunningLab]:
        return list(self._labs.values())

    async def shutdown_all(self) -> None:
        for lab_id in list(self._labs):
            try:
                await self.stop(lab_id)
            except LabError:
                pass

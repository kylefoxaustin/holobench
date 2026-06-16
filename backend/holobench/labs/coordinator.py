# SPDX-License-Identifier: GPL-2.0-or-later
"""Fabric coordinator — launches a whole lab and wires its nodes together.

Each node is just an ordinary Session (one QEMU + QMP + serial), launched through
the existing SessionManager. The coordinator's only extra job is the *fabric*:
for every `eth` link it allocates an isolated multicast group (a virtual L2
switch) and points each member board's modeled NIC at it via a stock
`-nic socket,mcast=...` backend (SessionManager.launch(nic_override=...)), giving
every member a unique MAC. No machine-model changes, no custom devices — exactly
the mechanism proven in the v3.0-α PoC (two i.MX91s pinging over one mcast group).

USB links are parsed and modeled but refused at launch until the board models
confirm usbredir host/device support (docs/TOPOLOGIES.md §USB escalation).
"""
from __future__ import annotations

import os
from enum import Enum
from pathlib import Path
from typing import Optional

from ..profiles.loader import default_asset_dir, load_profile
from ..session.manager import SessionError, SessionManager
from .models import Lab, LabError

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
        if lab.has_usb_links():
            raise LabError(
                "this lab declares USB links, which aren't launchable yet — usbredir "
                "host/device support must be confirmed by the board models first "
                "(see docs/TOPOLOGIES.md §USB). Ethernet links are fully supported."
            )

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
                        nic_override=nics,
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

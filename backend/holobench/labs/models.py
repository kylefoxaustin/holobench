# SPDX-License-Identifier: GPL-2.0-or-later
"""Lab (topology) spec models — v3.0.

A *lab* is a named set of **nodes** (each node = one board = one profile) plus
the **links** between them. It is the v3.0 unit of work, layered on top of the
single-board session machinery: the coordinator launches one Session per node
and wires their host-side netdev/USB backends to the links — all stock QEMU
interfaces (`-nic socket`/`mcast`, `usbredir`), never a custom inter-board device
(Prime Directive). See docs/TOPOLOGIES.md.
"""
from __future__ import annotations

from typing import Optional

from pydantic import field_validator, model_validator

from ..profiles.models import _Strict


class LabError(Exception):
    """Raised when a lab spec cannot be found or fails validation."""


class LabNode(_Strict):
    """One board in the topology. `profile` is a profile id (profiles/<id>.yaml)."""
    name: str
    profile: str


class LabLink(_Strict):
    """A connection between nodes.

    type="eth": a shared L2 segment (virtual switch) — every `members` node joins
    one multicast group, giving an L2 broadcast domain across separate QEMU procs.
    A two-member segment is just a point-to-point cable. PROVEN, stock QEMU.

    type="usb": usbredir transport between a `host` node (stock `-device usb-redir`
    importer) and a `device` node (usbredir exporter/listener); see
    docs/TOPOLOGIES.md §USB. VALIDATED end-to-end (2026-07-02): the gateway-lab
    i.MX93 host enumerates the MCXN947 CDC gadget at HIGH speed, binds /dev/ttyACM0.
    """
    type: str
    # eth segment:
    segment: Optional[str] = None
    members: list[str] = []
    # usb:
    host: Optional[str] = None
    device: Optional[str] = None
    # uart (symmetric point-to-point serial bridge between two nodes):
    a: Optional[str] = None
    b: Optional[str] = None

    @field_validator("type")
    @classmethod
    def _known_type(cls, v: str) -> str:
        if v not in ("eth", "usb", "uart"):
            raise ValueError(f"unknown link type '{v}' (expected 'eth', 'usb', or 'uart')")
        return v

    @model_validator(mode="after")
    def _shape(self) -> "LabLink":
        if self.type == "eth":
            if not self.segment:
                raise ValueError("eth link needs a 'segment' name")
            if len(self.members) < 2:
                raise ValueError(
                    f"eth segment '{self.segment}' needs >=2 members (got {len(self.members)})"
                )
            if len(set(self.members)) != len(self.members):
                raise ValueError(f"eth segment '{self.segment}' has duplicate members")
        elif self.type == "usb":
            if not (self.host and self.device):
                raise ValueError("usb link needs both 'host' and 'device'")
            if self.host == self.device:
                raise ValueError("usb link host and device must differ")
        elif self.type == "uart":
            if not (self.a and self.b):
                raise ValueError("uart link needs both 'a' and 'b'")
            if self.a == self.b:
                raise ValueError("uart link 'a' and 'b' must differ")
        return self


class Lab(_Strict):
    """A topology: nodes (boards) + links (how they're wired)."""
    id: str
    display_name: str
    description: str = ""
    nodes: list[LabNode]
    links: list[LabLink] = []

    @field_validator("nodes")
    @classmethod
    def _nodes_nonempty_unique(cls, v: list[LabNode]) -> list[LabNode]:
        if not v:
            raise ValueError("a lab needs at least one node")
        names = [n.name for n in v]
        if len(set(names)) != len(names):
            raise ValueError("duplicate node names in lab")
        return v

    @model_validator(mode="after")
    def _links_reference_nodes(self) -> "Lab":
        known = {n.name for n in self.nodes}
        for link in self.links:
            refs = (link.members if link.type == "eth"
                    else [link.host, link.device] if link.type == "usb"
                    else [link.a, link.b])
            for r in refs:
                if r not in known:
                    raise ValueError(
                        f"link references unknown node '{r}' (known: {sorted(known)})"
                    )
        return self

    def has_usb_links(self) -> bool:
        return any(link.type == "usb" for link in self.links)

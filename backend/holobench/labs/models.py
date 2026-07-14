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
    """One board in the topology. `profile` is a profile id (profiles/<id>.yaml).

    SCHEDULE (`start_at` / `stop_at`, seconds relative to lab launch). A lab used to
    be purely topological: every node came up at once and none ever left. That models
    a bench where someone plugs in three boards simultaneously and never unplugs one,
    which is not a bench that exists — and, more to the point, it is BLIND.

    The fleet's three emulator sessions found a QEMU `can_receive()` /
    `qemu_flush_queued_packets()` queue stall that is structurally invisible to any
    2-node test AND to any 3-node test whose nodes boot together. Their conclusion,
    verbatim: **"THE BUG CLASS LIVES IN TIME, NOT TOPOLOGY."** Not N>2 — N>2 *plus
    asynchronous arrival and departure*. Three synchronous self-verifying rehearsals
    all passed; the staggered co-launch found four distinct bugs.

    So time is a FIRST-CLASS field, not a convenience knob:
      start_at  — this node joins a segment that is ALREADY LIVE (or, at 0, broadcasts
                  alone into an empty one and must not mind).
      stop_at   — this node DEPARTS while the others keep running. The coordinator
                  issues the QMP quit at a known moment, which is the only reason an
                  early departure is a FACT and not an inference: a node that exits
                  itself makes "left" and "crashed" the same observation.
      rejoin_at — this node COMES BACK. Without it, a departure can only ever be shown to
                  be NOTICED, never SURVIVED: the survivors' heartbeat stops (they lost a
                  peer) and there is nothing left to distinguish "the wire absorbed the
                  loss" from "the wire stalled." **The RESUME is the assertion.** rt1180
                  measured it (2026-07-13): a surviving re-arming node beat 15,245 times,
                  went silent at 17.0s — exactly when its peer left — and resumed at
                  35.4s, exactly when it came back. THE GAP IS THE DEPARTURE, and the
                  resume is the recovery, and both are now numbers rather than silences.
                  (Requires the peers to RE-ARM: a latched node prints PASS once at t+5
                  and shows absolutely nothing here.)

    Defaults (0 / None) preserve the old behaviour exactly — every existing lab is
    unchanged: all nodes arrive at t=0 and nobody departs.

    `mac` pins the fleet's documented source MAC instead of the coordinator's
    auto-generated one, for labs whose peers identify each other by address.
    """
    name: str
    profile: str
    start_at: float = 0.0
    stop_at: Optional[float] = None
    rejoin_at: Optional[float] = None
    mac: Optional[str] = None

    @model_validator(mode="after")
    def _schedule_sane(self) -> "LabNode":
        if self.start_at < 0:
            raise ValueError(f"node '{self.name}': start_at must be >= 0")
        if self.stop_at is not None and self.stop_at <= self.start_at:
            raise ValueError(
                f"node '{self.name}': stop_at ({self.stop_at}) must be after "
                f"start_at ({self.start_at}) — a node cannot leave before it arrives"
            )
        if self.rejoin_at is not None:
            if self.stop_at is None:
                raise ValueError(
                    f"node '{self.name}': rejoin_at needs a stop_at — a node cannot come "
                    f"back if it never left"
                )
            if self.rejoin_at <= self.stop_at:
                raise ValueError(
                    f"node '{self.name}': rejoin_at ({self.rejoin_at}) must be after "
                    f"stop_at ({self.stop_at})"
                )
        return self


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
    # uart / spi / can (symmetric point-to-point bridge between two nodes 'a','b'):
    a: Optional[str] = None
    b: Optional[str] = None

    @field_validator("type")
    @classmethod
    def _known_type(cls, v: str) -> str:
        if v not in ("eth", "usb", "uart", "spi", "can", "i2c"):
            raise ValueError(
                f"unknown link type '{v}' (expected 'eth', 'usb', 'uart', 'spi', 'can', or 'i2c')")
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
        elif self.type in ("uart", "spi", "can", "i2c"):
            if not (self.a and self.b):
                raise ValueError(f"{self.type} link needs both 'a' and 'b'")
            if self.a == self.b:
                raise ValueError(f"{self.type} link 'a' and 'b' must differ")
        return self


class Lab(_Strict):
    """A topology: nodes (boards) + links (how they're wired) + WHEN they come and go."""
    id: str
    display_name: str
    description: str = ""
    nodes: list[LabNode]
    links: list[LabLink] = []
    # None = let the caller decide (CLI default: on). A raw-L2 lab sets this false:
    # its nodes talk in ethertypes, not IP, and a kernel `ip=` would be noise.
    auto_ip: Optional[bool] = None

    @property
    def is_staggered(self) -> bool:
        """True if this lab actually exercises TIME (someone arrives late, leaves, returns)."""
        return any(n.start_at > 0 or n.stop_at is not None or n.rejoin_at is not None
                   for n in self.nodes)

    @property
    def horizon_s(self) -> float:
        """Last scheduled event. A staggered lab observed for less than this has not
        been run — it has been interrupted."""
        return max([0.0]
                   + [n.start_at for n in self.nodes]
                   + [n.stop_at for n in self.nodes if n.stop_at is not None]
                   + [n.rejoin_at for n in self.nodes if n.rejoin_at is not None])

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

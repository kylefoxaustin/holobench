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

    KIND — EMULATED, OR ACTUAL SILICON.

    Every node in every lab until now was a QEMU process holobench launched. That
    made `kind` an unstated constant, and unstated constants are where a design
    quietly assumes the thing it should be proving.

    `kind: silicon` is a node holobench does **not** launch and does not own: a
    physical board on a physical wire, reached over ssh, running the fleet's v2
    beacon (`tools/l2beacon.py`). It has no profile, no QMP, no framebuffer, and
    holobench cannot reset it — it can only stage a beacon on it, start it, and
    read what its console says it RECEIVED.

    ⭐ WHY THAT LIMITATION IS THE POINT, AND NOT A GAP. The emulated node sits on
      a macvtap, where guest and host frames can be locally switched — so the
      QEMU side's own PASS is compatible with nothing ever leaving the NIC. A
      silicon node is the only oracle in the lab that CANNOT be fooled that way,
      because it is on the far side of a cable. Everything holobench cannot do to
      it is exactly why its word is worth more than ours.

      PROVEN BY AN EMULATED NODE   frames moved between things holobench controls.
      PROVEN BY A SILICON NODE     a frame crossed a cable into hardware that has
                                   no idea its peer is emulated.

    So a silicon node's `ethertype` is required and its console is load-bearing.
    A lab with no silicon node cannot make the claim this lab family exists for.
    """
    name: str
    # Emulated nodes carry a profile id; silicon nodes never do.
    profile: Optional[str] = None
    kind: str = "emulated"
    start_at: float = 0.0
    stop_at: Optional[float] = None
    rejoin_at: Optional[float] = None
    mac: Optional[str] = None
    # --- silicon (kind="silicon") only ---
    # ssh target for the real board, e.g. "root@10.0.1.181".
    host: Optional[str] = None
    # the board's own interface on the shared wire, e.g. "eth1" / "l4tbr0".
    iface: Optional[str] = None
    # this node's beacon ethertype. MUST be inside the fleet's beacon block
    # (0x88B5..0x88BF) or every enet-lab3 node silently ignores it and the gate
    # that needs to see it can never go green (95emulator, enet-lab3.c:511).
    ethertype: Optional[int] = None
    # ethertypes this board must SEE before it declares a PASS. Its console
    # saying so is the lab's load-bearing assertion.
    watch: list[int] = []
    # prefix the beacon with sudo on that board (a real board's raw socket needs
    # root; some boards log in as root already and do not want it).
    sudo: bool = False

    @model_validator(mode="after")
    def _kind_shape(self) -> "LabNode":
        if self.kind not in ("emulated", "silicon"):
            raise ValueError(
                f"node '{self.name}': unknown kind '{self.kind}' "
                f"(expected 'emulated' or 'silicon')")
        if self.kind == "emulated":
            if not self.profile:
                raise ValueError(f"node '{self.name}': an emulated node needs a profile")
            for f in ("host", "iface", "ethertype"):
                if getattr(self, f) is not None:
                    raise ValueError(
                        f"node '{self.name}': '{f}' is a silicon-node field, but this "
                        f"node is emulated — holobench launches it, it is not out on a wire")
        else:
            if self.profile:
                raise ValueError(
                    f"node '{self.name}': a silicon node has no profile — holobench does "
                    f"not launch real hardware, it only beacons on it and reads its console")
            for f in ("host", "iface", "ethertype"):
                if getattr(self, f) is None:
                    raise ValueError(f"node '{self.name}': a silicon node needs '{f}'")
            if self.stop_at is not None or self.rejoin_at is not None:
                # ⚠️ A DEPARTURE MUST BE THE COORDINATOR'S, NEVER THE NODE'S — the
                # lab's oldest rule. holobench cannot QMP-quit a physical board, so
                # scheduling one here would make "it left" and "it crashed" the same
                # observation, which is precisely the collapsed oracle stop_at exists
                # to prevent. Pull the board's cable by hand and say you did.
                raise ValueError(
                    f"node '{self.name}': a silicon node cannot be scheduled to stop or "
                    f"rejoin — holobench does not control its power. A departure it "
                    f"cannot ISSUE is a departure it cannot ASSERT.")
            for et in [self.ethertype] + list(self.watch):
                if not 0x88B5 <= et <= 0x88BF:
                    raise ValueError(
                        f"node '{self.name}': ethertype 0x{et:04x} is outside the fleet "
                        f"beacon block 0x88B5..0x88BF, so every enet-lab3 node on the "
                        f"segment would silently ignore it (enet-lab3.c:511) and any gate "
                        f"on it would be structurally unsatisfiable")
        return self

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

    TRANSPORT (eth links only) — AND THIS IS THE ONE THAT LETS SILICON IN.

    "mcast" (default, and every lab before this one): each member gets
    `-nic socket,mcast=<group>`. That is a QEMU-INTERNAL socket. It is a real L2
    broadcast domain between QEMU processes and it has found real bugs — but it
    NEVER TOUCHES A WIRE. No frame on an mcast segment has ever left the host, so
    no result on one can say anything about physical hardware.

    "macvtap": each emulated member gets a macvtap endpoint on a REAL host NIC
    (`wire:`), handed to QEMU as `-nic tap,fd=N`. Now the segment is an actual
    Ethernet segment, and anything else on that broadcast domain — including a
    physical board — is a peer.

    ⭐ THE WHOLE POINT OF THE DISTINCTION: on "mcast" the strongest claim
      available is "our models talk to each other." On "macvtap" it becomes
      "the model is indistinguishable from the silicon, TO THE SILICON."

    ⚠️ AND THE TRAP THAT COMES WITH IT, flagged by claude-connect before the
      first run and worth repeating at the point of use: a macvtap can LOCALLY
      SWITCH guest<->guest without a frame ever reaching the NIC. So a green run
      on a macvtap segment is achievable with the cable unplugged. That is why a
      macvtap segment is required below to contain at least one `kind: silicon`
      member — the only member whose console cannot be fooled by local switching,
      because it is on the far side of the cable.
    """
    type: str
    # eth segment:
    segment: Optional[str] = None
    members: list[str] = []
    # "mcast" (QEMU-internal, the default) or "macvtap" (a real host NIC).
    transport: str = "mcast"
    # macvtap only: the host NIC to attach to, e.g. "enp6s0" (LAN) or
    # "enx42b8036560ca" (the cdc_ncm USB gadget link to the Jetson).
    wire: Optional[str] = None
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
            if self.transport not in ("mcast", "macvtap"):
                raise ValueError(
                    f"eth segment '{self.segment}': unknown transport "
                    f"'{self.transport}' (expected 'mcast' or 'macvtap')")
            if self.transport == "macvtap" and not self.wire:
                raise ValueError(
                    f"eth segment '{self.segment}': transport 'macvtap' needs a "
                    f"'wire' (the host NIC to attach to)")
            if self.transport == "mcast" and self.wire:
                raise ValueError(
                    f"eth segment '{self.segment}': 'wire' is meaningless on a "
                    f"'mcast' transport — an mcast segment never touches a NIC")
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

    @model_validator(mode="after")
    def _macvtap_segment_needs_an_oracle_it_cannot_fool(self) -> "Lab":
        """⭐ A REAL-WIRE SEGMENT MUST CONTAIN AT LEAST ONE SILICON NODE.

        This is the lab's central invariant, enforced at load rather than left to
        whoever writes the YAML at 4am.

        A macvtap can locally switch guest<->guest without a frame ever reaching
        the NIC. So a segment of nothing but emulated nodes, on a macvtap, can go
        FULLY GREEN WITH THE CABLE UNPLUGGED — and it would look exactly like a
        successful real-wire run, which is worse than a red one. The green would
        be indistinguishable from the result we actually want, and that is the
        definition of an oracle that cannot fail.

        A `kind: silicon` member is the fix, and it is not a formality: it is the
        one node on the segment whose receive count cannot be produced by local
        switching, because it is on the far side of a physical cable. Requiring
        one is requiring that the segment contain something capable of falsifying
        it. If it cannot fail, it has not been tested.
        """
        by_name = {n.name: n for n in self.nodes}
        for link in self.links:
            if link.type != "eth" or link.transport != "macvtap":
                continue
            if not any(by_name[m].kind == "silicon" for m in link.members):
                raise ValueError(
                    f"eth segment '{link.segment}' uses transport 'macvtap' but has no "
                    f"'kind: silicon' member. A macvtap can locally switch guest<->guest "
                    f"with nothing reaching the NIC, so this segment could go fully green "
                    f"with the cable unplugged. Add the real board, or use transport "
                    f"'mcast' and stop claiming the wire.")
        return self

    @property
    def silicon_nodes(self) -> list[LabNode]:
        """The nodes holobench does NOT own — and whose consoles it must therefore
        read rather than assert on their behalf."""
        return [n for n in self.nodes if n.kind == "silicon"]

    def has_usb_links(self) -> bool:
        return any(link.type == "usb" for link in self.links)

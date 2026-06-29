<!-- SPDX-License-Identifier: GPL-2.0-or-later -->
# Holobench v3.0 — multi-board topologies (the virtual board lab)

**Status: design / in progress.** v1–v2 unit of work = *one board*. v3.0 = a
**topology of boards wired together** — a composable virtual lab. A gateway
i.MX93 with an MCXN947 sensor node on USB; two i.MX95s + an i.MX93 on a shared
Ethernet segment; a USB hub fanning out nodes. Things a fixed physical farm can't
do without literally cabling boards on a bench.

This stays inside the Prime Directive: boards interconnect over **stock QEMU
interfaces only** (`-netdev socket`/`mcast`, `usbredir`) — no custom inter-board
devices, no machine-model patches.

## The unit of work becomes a *lab*

A **lab** (topology) = a named set of **nodes** (each a board = a profile) plus
the **links** between them. Declared like profiles, in `labs/<id>.yaml`:

```yaml
id: gateway-lab
display_name: "i.MX93 gateway + MCXN947 sensor"
nodes:
  - { name: gw,     profile: imx93-evk-sd }
  - { name: sensor, profile: mcxn947-evk }
links:
  - { type: usb, host: gw, device: sensor }          # 93 (host) <-> MCX (device)

# a multi-board Ethernet segment (a virtual switch/hub):
# nodes: [{name: a, profile: imx95-evk-sd}, {name: b, profile: imx95-evk-sd}, {name: c, profile: imx93-evk-sd}]
# links: [{ type: eth, segment: lan0, members: [a, b, c] }]
```

The session manager already runs one QEMU per board; a **fabric coordinator**
launches all of a lab's nodes together and wires their netdevs/USB to the links.

## Link types and the stock-QEMU mechanism behind each

### Ethernet — ✅ stock QEMU, no model changes (the v3.0 foundation)
A board NIC is attached to a **socket netdev** instead of user-mode slirp:

- **Point-to-point cable** (two boards): `-nic socket,connect=` ⇄ `-nic socket,listen=`.
- **Shared segment / virtual switch** (N boards): every member joins one multicast
  group — `-nic socket,mcast=<group:port>` — giving a single L2 broadcast domain
  across N separate QEMU processes.

The boards' *modeled* NICs (ENET/ENETC/FEC) need no changes — only the host-side
netdev backend differs (`socket` vs `user`). Holobench's resolver gains a per-NIC
"fabric" backend (see `SessionRuntime.nic_override`); the coordinator allocates a
mcast group per `eth` segment and points each member's NIC at it. Addressing on a
bare segment is the lab's choice: static IPs, or one node runs DHCP.

**Per-node NIC `model=`.** Some boards expose more than one modeled NIC and need
disambiguation so the fabric binds the *functional* one — the MCXN947's ENET-QoS is
`model=mcxn-enet`, the i.MX9 FEC is `model=imx.enet` (NOT the eQOS registration
stub). A board carries this in `profile.net.fabric_nic_model`; the coordinator
appends `,model=<x>` to that node's socket spec (i.MX91, with FEC first, needs none
— it auto-attaches, as the α PoC proved). This is what lets a **mixed-binary**
lab work: the `mcx93-eth` lab puts an MCXN947 (forked `qemu-system-arm`) and an
i.MX93 (`qemu-system-aarch64`) on one mcast L2 segment — the segment is host-side
and binary-agnostic. Readiness ladder with the emulators: M1 raw L2 (broadcast +
ethertype 0x88B5), M2 i.MX93-Linux pings a static-IP MCX (ARP+ICMP), stretch iperf.
(M1+ needs the MCX booting its ENET test firmware, pending from the mcxn947 session;
the lab + wiring are ready now.)

### USB — 🟡 stock transport, but model-dependent (after MCX qemu)
**usbredir** (`usb-redir` chardev over a unix socket) is the upstream-clean
transport: one board exports a USB **device/gadget**, another imports it as a
**host**. Holobench would bridge the two processes' usbredir sockets.

**Dependency / escalation:** the board models must support the USB **host and
device (gadget) roles** and a **redirectable endpoint**. This is a board-capability
question for the emulator sessions (per CLAUDE.md §7) — Holobench can't add it.
Gated on: MCX qemu finished + 93/MCX confirming usbredir export/import. A **USB
hub** is then either a modeled `usb-hub` on the host node or a fan-out of redirects.

#### USB link socket contract (proposed — for 93/MCX to align to)

So the eventual coordinator wiring is a drop-in (same shape as the eth mcast
allocation), the per-link transport is a **unix-domain socket** the coordinator
allocates and hands to both nodes — analogous to one mcast group per eth segment:

- **Transport:** one unix socket per USB link at `<lab-run-dir>/usb-<link>.sock`
  (short path, under the lab's runtime dir — minds the ~108-char `sun_path` limit
  the session code already guards). Single host today (both QEMUs on one machine);
  TCP loopback is the trivial swap if nodes ever span hosts.
- **Roles (who binds vs connects):** the **host node** (the `host:` board, e.g.
  i.MX93) owns the chardev as the **listener** —
  `-chardev socket,id=hb-usb-<link>,path=<sock>,server=on,wait=off` feeding its
  **stock** `-device usb-redir,chardev=hb-usb-<link>` (i.MX side stays stock — no
  model coupling, Prime Directive intact). The **device node** (the `device:`
  board, e.g. MCXN947) **connects as client** —
  `-chardev socket,id=hb-usb-<link>,path=<sock>` (server off) wired into its
  device-mode usbredir exporter.
- **Rationale for host=listener:** the host is the long-lived stock-Linux board; a
  bare-metal/RTOS device end can restart and **reconnect** to a stable listener
  (`reconnect=<s>` on the client chardev) — and it matches 93's proposal (93
  listens, MCX dials in).
- **Launch order:** coordinator brings the **host (listener) up first**
  (`wait=off`, so it never blocks), then the **device (client)**. One device per
  link to start (point-to-point); a `usb-hub` on the host fans out later.
- **Lab spec stays as-is:** `links: [{ type: usb, host: <node>, device: <node> }]`
  (already in `gateway-lab.yaml`). The coordinator allocates the socket + emits the
  two chardev strings; nothing new in the YAML.

This is the contract holobench will implement in the `LabCoordinator` the moment a
device enumerates end-to-end through the host (M1). 93/MCX: align your two ends to
it and the lab is a drop-in.

### Future links
`can` (`-object can-bus` shared across procs), a second `serial` cross-link,
SPI/I2C bridges — same pattern: stock transport + per-board facts, never a custom
device.

## Architecture

```
            labs/<id>.yaml  (nodes = profiles, links = eth/usb/…)
                     │
            ┌────────▼─────────┐   allocates mcast groups / usbredir sockets,
            │ Fabric coordinator│   launches each node as a Session with the
            └────────┬─────────┘   right netdev/usb wiring, tracks the lab
        ┌────────────┼────────────┐
   ┌────▼───┐   ┌────▼───┐   ┌────▼───┐     each node = one existing Session
   │ node A │   │ node B │   │ node C │     (QEMU proc + QMP + serial), unchanged
   └────┬───┘   └────┬───┘   └────┬───┘
        └──── -nic socket,mcast=lan0 ───┘   ← stock L2 segment (virtual switch)
```

- **Reuses the session manager** — a node is just a `Session`. New: a `Lab`
  object owning N sessions + the link wiring, with lab-level launch/stop/reserve.
- **API:** `/api/labs` (list/launch/stop), `/api/labs/{id}` (status + per-node
  sessions). Per-node console/LCD/etc. reuse the existing session endpoints.
- **UI:** a topology view (nodes as tiles, links as edges) → click a node for its
  console/panels. The single-board UI becomes the per-node view.

## Sequencing

1. **v3.0-α — Ethernet foundation — ✅ PROVEN.** `nic_override` is in the resolver
   (swap a board NIC's backend to a `socket`/`mcast` segment). PoC: two i.MX91
   boards (separate QEMU procs), each `-nic socket,mcast=230.0.0.10:1234,mac=…`
   (unique MAC per node!), static IPs on eth0 (the first modeled NIC = the FEC) —
   `ping` across them: **3/3 packets, 0% loss, ~1 ms**. Core mechanism confirmed:
   multi-board L2 over stock QEMU, no model changes. Gotchas captured: give each
   node a unique `mac=` (else collision), and the i.MX91's first non-`lo` iface is
   `can0` alphabetically — the fabric NIC is `eth0`. Next: the coordinator + lab spec.
2. **v3.0-β — lab spec + coordinator + topology UI — ✅ SHIPPED.** `labs/*.yaml`
   (validated by pydantic, profiles checked at load), a **fabric coordinator**
   (`backend/holobench/labs/coordinator.py`) that allocates an isolated mcast group
   per `eth` segment, assigns a unique MAC per node-NIC, and launches each node as
   an ordinary Session via `nic_override`; `/api/labs` (list/launch/status/stop) +
   a **Labs** modal in the UI (topology view: node tiles → click to open a node's
   console; segment edges listed). CLI: `holobench labs` / `lab show` / `lab
   launch`. Verified end-to-end: the `eth-pair` lab boots two real i.MX91 boards
   on one mcast group with unique MACs (same wiring as the proven α PoC). Shipped
   labs: `eth-pair`, `lan-trio`, `gateway-lab` (gated). Next: USB links.
3. **v3.0 — USB links:** after MCX qemu + emulator usbredir confirmation; 93↔MCX
   over USB, then USB hubs.

## Open questions (to resolve with the emulator sessions / Kyle)
- USB: do the i.MX + MCX models support usbredir device/host export/import? (the
  gating dependency).
- Fabric addressing default: static IPs vs a DHCP node vs auto-assign.
- Reservation model for a lab (reserve the whole topology vs per node).
- Cross-host scale-out (later): mcast→a real bridge / VXLAN when nodes span hosts.

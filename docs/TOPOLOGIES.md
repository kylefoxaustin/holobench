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
**DELIVERED 2026-07-02**: the `gateway-lab` launches an i.MX93 host + an MCXN947
CDC-ACM device (`mcxn947-usb-device`) over one usbredir socket; the host enumerates
the gadget at HIGH speed and binds `/dev/ttyACM0` — validated through the
coordinator. A **USB hub** is then either a modeled `usb-hub` on the host node or a
fan-out of redirects.

#### USB link socket contract (proposed — for 93/MCX to align to)

So the eventual coordinator wiring is a drop-in (same shape as the eth mcast
allocation), the per-link transport is a **unix-domain socket** the coordinator
allocates and hands to both nodes — analogous to one mcast group per eth segment:

- **Transport:** one unix socket per USB link at `<lab-run-dir>/usb-<link>.sock`
  (short path, under the lab's runtime dir — minds the ~108-char `sun_path` limit
  the session code already guards). Single host today (both QEMUs on one machine);
  TCP loopback is the trivial swap if nodes ever span hosts.
- **Roles (who binds vs connects) — follows the usbredir convention:** the
  **device node** (the `device:` board, e.g. MCXN947) is the **exporter = listener**
  (the `usbredirserver` role) — `-chardev
  socket,id=hb-usb-<link>,path=<sock>,server=on,wait=off` into its device-mode
  usbredir exporter. The **host node** (the `host:` board, e.g. i.MX93) is the
  **importer = client** — its **stock** `-device usb-redir,chardev=hb-usb-<link>`
  over `-chardev socket,id=hb-usb-<link>,path=<sock>,reconnect=2` (i.MX side stays
  stock — no model coupling, Prime Directive intact).
- **Why device=listener:** it's the standard usbredir role split (the exporter
  listens; QEMU's importer `-device usb-redir` connects), and MCX's device end is
  already built that way. `reconnect=<s>` on the host's client chardev makes order
  and restarts forgiving on either end (the host retries until the device's socket
  is up, and re-attaches if the device bounces).
- **Launch order:** coordinator brings the **device (listener) up first**
  (`wait=off`), then the **host (client)** — though `reconnect` means strict order
  isn't required. One device per link to start (point-to-point); a `usb-hub` on the
  host fans out later.
- **Lab spec stays as-is:** `links: [{ type: usb, host: <node>, device: <node> }]`
  (already in `gateway-lab.yaml`). The coordinator allocates the socket + emits the
  two chardev strings; nothing new in the YAML.

This is the contract holobench will implement in the `LabCoordinator` the moment a
device enumerates end-to-end through the host (M1). 93/MCX: align your two ends to
it and the lab is a drop-in.

### UART — ✅ stock QEMU, DELIVERED 2026-07-03
A board-to-board serial bridge: each board's **spare link UART** wired to a stock
`-chardev socket` (one end `server=on`, the other `server=off`) + `-serial
chardev:<id>`, landing on the next `serial_hd()` after the declared consoles.
Symmetric point-to-point. Lab: `{ type: uart, a: <node>, b: <node> }`.

Per-board facts live in the profile's `uart.link` block (chardev template + the
guest device + an `attach_dtb` that ENABLES the link UART, since many EVK dtbs
leave the spare UART disabled). For i.MX91 the link UART is **LPUART2**
(`serial@44390000`, `/dev/ttyLP1`, `serial_hd(1)`); `tools/make-uart-dtb.sh`
generates `imx91-11x11-evk-uartlink.dtb` (flips `serial@44390000` disabled→okay,
no model change). **Validated** through the coordinator (`uart-link-91`): a payload
crossed boardA → LPUART2 → socket → LPUART2 → boardB **byte-exact** between two
i.MX91 guests — the coordinator-launched version of 91emulator's `run-uart.sh`.

### SPI — ✅ stock transport, DELIVERED 2026-07-03
A board-to-board SPI bridge: each board is an LPSPI master with the model's
`spi-link` SSI bridge peripheral on its bus, socket-bridged to the peer
(`-device spi-link,bus=<lpspi>,chardev=<id>` + `-chardev socket`, one server /
one reconnecting client). Lab: `{ type: spi, a: <node>, b: <node> }`. The
`spi-link` peripheral is provided by the board model (fleet-converged on 91's
`spi_link.c`; 91/93/mcx all ship it) — holobench only passes `-device`, adding
nothing to the model. Per-board facts in the profile's `spi.link` block; for
i.MX91 the link controller is **LPSPI1** (`spi@44360000`, `/dev/spidev0.0`), and
`tools/make-spi-dtb.sh` generates `imx91-11x11-evk-spilink.dtb` (enables lpspi1 +
a spidev child). **Validated** through the coordinator (`spi-link-91`): both i.MX91
bind `fsl_lpspi` (PIO) + expose `/dev/spidev0.0` over the live socket bridge.

### CAN — ✅ stock transport, DELIVERED 2026-07-03 (the fifth + final)
A board-to-board CAN bridge over the fleet-shared generic `can-host-chardev`
backend (bridges an emulated `can-bus` to a chardev — **no host vcan/SocketCAN,
no root**). Each end gets `-object can-bus` + `-chardev socket` (one server / one
reconnecting client) + `-object can-host-chardev`, and the machine's can-buses are
wired to the bridged bus via a `-machine ...,canbus0=cb,canbus1=cb` property
(`machine_extra`). Lab: `{ type: can, a: <node>, b: <node> }`. Per-board facts in
the profile's `can.link` block; for i.MX91 the FlexCAN is `can0` and the **stock
EVK DT already enables it — no overlay dtb**. **Validated** through the coordinator
(`can-link-91`): a frame `0x321 [5] 48 41 42 43 44` crossed FlexCAN → can-bus →
can-host-chardev → socket → FlexCAN **byte-exact** between two i.MX91 guests.

### I2C — ✅ stock QEMU, DELIVERED 2026-07-03 (the sixth)
A board-to-board I²C bridge: each board is an LPI2C master with the model's
`i2c-link` target device at a fixed address on its bus, socket-bridged to the peer
(`-device i2c-link,bus=<lpi2c>,address=0x42,chardev=<id>` + `-chardev socket`, one
server / one reconnecting client — the I²C analogue of `spi-link`). Lab:
`{ type: i2c, a: <node>, b: <node> }`. Per-board facts in the profile's `i2c.link`
block; for i.MX91 the bus is **LPI2C3** (`i2c@42530000`, `/dev/i2c-N`) and — unlike
UART/SPI — **no dtb patch** (LPI2C3 + `i2c-dev` are stock on the EVK dtb). The
`i2c-link` device was authored on 93emulator (`hw/i2c/…`) and carried on 91.
**Validated** through the coordinator (`i2c-link-91`): both i.MX91 register the
LPI2C adapters and bind `i2c-link` over the live socket (byte-exact data path proven
by the fleet's `run-i2c.sh`).

### Future links
Further multi-node fabrics — same pattern: stock transport + per-board facts, never
a custom device. **All six wired transports (eth, USB, UART, SPI, CAN, I2C) are now
delivered.**

## The schedule — a lab's *time* is part of its spec (2026-07-13)

Every lab above is a **topology**: which boards, wired how. That was the whole model,
and it was **half the model**. A lab also has a **timeline**, and the timeline is where
a whole class of bug lives.

The three emulator sessions (rt1180 + mcxn947 + 95emulator) built a three-node raw-L2
segment and hit a QEMU `can_receive()` / `qemu_flush_queued_packets()` **queue stall**
that is structurally invisible to any 2-node test **and** to any 3-node test whose nodes
boot together. All three, independently, asked for the same thing:

> **"DON'T LAUNCH THREE NODES. STAGGER THE ARRIVALS AND THE DEPARTURES, AND MAKE ONE
> LEAVE EARLY. A lab that starts them all at once will be green forever and will never
> find anything again."**
>
> **"THE BUG CLASS LIVES IN TIME, NOT TOPOLOGY."** Not N>2 — N>2 *plus asynchronous
> arrival and departure. The segment is only as patient as its least patient member,
> and only as durable as its shortest window.*

Three synchronous self-verifying rehearsals passed. The staggered co-launch found four
bugs. So `LabNode` grew two fields, and they are **first-class, not convenience knobs**:

```yaml
nodes:
  - { name: imx95,  profile: imx95-evk-enet-lab3, start_at: 0   }               # broadcasts ALONE into an empty segment
  - { name: mcx,    profile: mcxn947-enet-lab3,   start_at: 150, stop_at: 420 } # joins live; LEAVES while others run
  - { name: rt1180, profile: imxrt1180-evk-netc,  start_at: 210 }               # joins later still
```

* `start_at` — the node joins a segment **already in progress** (or, at 0, shouts into a
  void and must not mind: a peer that is not here *yet* is not a failure).
* `stop_at` — the node **departs while the others keep running**. The wire loses a member
  and must not stall.

Defaults (`0` / `None`) preserve the old behaviour exactly: every pre-existing lab still
launches all nodes at once and never retires one.

### Departure is the coordinator's, never the node's

**The coordinator issues a QMP `quit` at a moment it chose and recorded.** This is not an
implementation detail — it is the only thing that makes an early departure a *fact*:

> **A node that exits itself makes "it left" and "it crashed" THE SAME OBSERVATION.**

That collapsed oracle is what the whole fleet spent the week hunting, and holobench has
already been bitten by it once: onboarding the RT1180 over UART, a firmware that called
semihosting `SYS_EXIT` after PASS **terminated QEMU**, so the coordinator's QMP connect
was refused *even though the link had passed*. Holobench is a persistent board **farm** —
it holds every board over QMP for that board's whole life. Two consequences, both enforced:

1. **Lab firmware must be PERSISTENT** — beacon/echo forever, WFI idle, never self-exit.
2. **A scheduled departure `quit()`s the node; it does NOT `destroy()` the session.**
   `destroy()` calls `cleanup()`, which `rmtree`s the work dir — *and that dir holds the
   node's console log*. Destroying it would erase the evidence of the one node whose
   departure is the entire point ("did it PASS before it left?"). The board leaves the
   wire; its log survives until the lab itself is torn down. (Guarded by
   `test_departure_quits_the_node_but_does_NOT_destroy_its_session`.)

### Scoring a scheduled lab

* **Grep the TOKEN, not the substring.** The only evidence is the literal PASS line in a
  node's **own** console, printed by its **own** firmware. rt1180's monitor once matched
  its own `need 0x88b7` *banner* and shouted PASS twelve times at an empty wire.
* **A timeout is INCONCLUSIVE, never a FAILURE** (mcxn947's rule). A killed run is not a
  caught bug.
* **A staggered lab observed for less than its `horizon_s` has not been run — it has been
  interrupted.** `holobench lab launch` therefore defaults its hold to the remaining
  horizon rather than exiting before the last scheduled event.
* Any lab asserting a *timing* property must run under **`-icount`**, or it is measuring
  host load rather than the guest.

Reference lab: **`labs/mcx-rt1180-95-l2.yaml`** — MCXN947 (M33, bare metal) + i.MX RT1180
(M33, bare metal) + i.MX 95 (A55, Linux) on one stock `-nic socket,mcast=` wire; three
SoCs, two QEMU binaries, two instruction sets, distinct ethertypes (0x88B5/B6/B7), each
node PASSing only on **observing both others**.

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
   labs: `eth-pair`, `lan-trio`, `gateway-lab` (USB, validated). Next: USB hub / fan-out.
3. **v3.0 — USB links:** after MCX qemu + emulator usbredir confirmation; 93↔MCX
   over USB, then USB hubs.

## Open questions (to resolve with the emulator sessions / Kyle)
- USB: do the i.MX + MCX models support usbredir device/host export/import? (the
  gating dependency).
- Fabric addressing default: static IPs vs a DHCP node vs auto-assign.
- Reservation model for a lab (reserve the whole topology vs per node).
- Cross-host scale-out (later): mcast→a real bridge / VXLAN when nodes span hosts.

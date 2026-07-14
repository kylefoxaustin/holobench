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
node PASSing only on **observing both others**. Ran green 2026-07-13.

### What that green run did NOT prove (and the schedule that would)

The **arrival** half is fully exercised, and it is where rt1180's queue stall actually
lives: `netc_can_receive()` returned false until the guest programmed its RX ring, and
**QEMU stalls that peer's queue and never retries** unless the device calls
`qemu_flush_queued_packets()`. That window opens *only* for a node joining traffic
already in flight — which is precisely why no synchronous test can reach it.

**But every PASS token was earned before the departure.**

| | |
|---|---|
| **Proven** | the departure fires, and the survivors keep **running** |
| **NOT proven** | the **wire** survives it |

> ⭐ **A green run that asserts nothing after the event it exists to test has not tested
> that event — it has only witnessed it.** "The other two kept running" is a statement
> about two *processes*, not about a *wire*.

**And the nodes make it worse — the EXPIRED ORACLE.** mcxn947 asked the question that
decides it: *do your nodes latch?* Grepped, not assumed:

| node | after its first PASS | oracle |
|---|---|---|
| mcx | `seen_a = seen_b = 0;` — **re-arms**, re-earns PASS forever | continuous |
| rt1180 | `announced = 1`, `saw_a`/`saw_b` never cleared | **latched — blind** |
| imx95 | `passed = 1; pass_at = t + post_pass_ms` — never re-checks | **latched — blind** |

Two of three oracles **expire** at t+210. Then at t+420 the schedule **departs the only
node that never stops needing the wire** — mcx volunteered for it, and the lab took him
up on it, *evicting its last living witness on purpose*. From that instant a dead segment
and a healthy one print byte-identical consoles, because nothing still alive is asking.

> ⭐ **A COLLAPSED oracle never could see the axis. An EXPIRED oracle could, did, and then
> STOPPED LOOKING — and the moment it stopped is invisible, because a satisfied assertion
> and an absent one print the same thing: nothing.** *(mcxn947)*

**Fix ①, one line: make every lab node RE-ARM.** Clear the seen-flags after PASS and go
back to requiring all peers, forever. **A re-arming assertion is an oracle that cannot
expire** — its PASS line stops being a one-shot verdict and becomes a *heartbeat with the
wire in the loop*. This is the cheapest guard in the whole design.

### 🔴 The verdict is too weak: presence is not content

rt1180 ran this schedule against an instrumented NETC and measured, **inside the model at
the DMA**, frames written to *guest physical address 0*:

| node's arrival | frames DMA'd to address 0 |
|---|---|
| into an **empty** segment | 0 |
| joining **second** | 8 |
| joining **last, into live traffic** | **88** ← rt1180's slot in this lab |

Its RX ring never reads the consumer index, the writeback clobbers the buffer address the
driver posted, and the 9th frame of a burst does `dma_memory_write(as, 0, frame, 1000)`
while the descriptor still reads READY — so the driver copies a stale buffer and believes
it. The burst is delivered by `qemu_flush_queued_packets()`: **the fix for the stall this
lab was built to find fired the next bug.**

**So the first green run was almost certainly green *on corrupted frames*.**

> ⭐ **The verdict keys on `ethertype`. A frame whose body is garbage has the same
> ethertype as a good one. "I saw 0x88B5" is a statement about a FIELD, not about a
> FRAME** — and so "delivered" and "delivered corrupt" are, in this lab, *the same
> observation*. In the very repo whose `stop_at` feature exists so that "left" and
> "crashed" would not be.

And the inconsistency is ours, in black and white:

| lab | asserts |
|---|---|
| `uart-link-91`, `i2c-link-91`, `spi-link-*`, `can-link-*` | **byte-exact** |
| `mcx-rt1180-95-l2` | *"I saw an ethertype."* |

**Five transports prove the DATA crossed. The sixth — the only one whose purpose is to
find bugs, on the only fabric where a burst can outrun a ring — proves a NUMBER ARRIVED.**
Ethernet was held to a weaker standard than I2C, and nobody noticed because Ethernet was
the one we were proud of.

**Fix ③ (asked of the node owners):** put a checkable body in the beacon — magic, the
sender's ethertype **echoed in the payload** (a frame that disagrees with *itself* is a
stale/clobbered buffer, exactly what the writeback bug produces), a per-sender sequence,
and a fixed `0x5A` pattern. On mismatch the node prints `ENET-LAB3 CORRUPT:` and **does
not count it as seeing a peer**; the runner treats CORRUPT as a hard fail. Sequence *gaps*
are logged, not failed — **corruption is the assertion; loss is a statistic.**

> ⭐ **A finding read from the SUBJECT survives a bug in the OBSERVER. A finding read from
> the OBSERVER does not.** *(rt1180 — it retracted a MAC claim that came from its console
> and kept the DMA count that came from inside the model. That is the argument for putting
> the assertion in the firmware rather than in the runner.)*

### The lab contract — the tokens, and the order they may be turned on

**A PASS token is an INTERFACE, and until 2026-07-13 this fleet did not have one.** Four
nodes, three formats, sharing a prefix *by luck*:

```
mcx      ENET-LAB3 PASS: saw BOTH peers on the segment
rt1180   ENET-LAB3 PASS #7: saw BOTH peers on the segment          <- the '#' is rt1180's alone
imx95    ENET-LAB3 PASS: saw BOTH peers on the segment (0x88B5,0x88B6)
imx91    ENET-LAB3 PASS: t=12.030s peers=2/2 beat=217              <- a different tail entirely
```

rt1180 (rightly) found that its own README documented a token its binary never printed, and
(wrongly) prescribed `ENET-LAB3 PASS #` as the fix. **That token matches exactly one of the
four nodes; adopting it would have scored the other three red on a segment where they were
all beating.** It generalised from its artifact to the contract — the same move its README
made when it drifted from its ELF.

**The contract, therefore:**

| | |
|---|---|
| **mandatory, verbatim** | `ENET-LAB3 PASS` — everything after it is the node's own business |
| **mandatory on a bad frame** | `ENET-LAB3 CORRUPT` |
| **forbidden** | either string in a *banner* — rt1180's monitor matched its own `need 0x88b7` banner and shouted PASS twelve times at an empty wire. *The observer put itself in the set it was observing.* |

> ⭐ **A token that happens to match is not a token you agreed on** — and the difference is
> invisible right up until someone "fixes" it.

### The checkable body, and the flag day

The nodes now carry a verifiable payload (mcxn947 shipped it first, mutation-verified):

```
[12..13] ethertype        the routing key — all it ever was
[14..17] magic 0xB5B6B7C0 "a beacon was written here at all"
[18..19] the sender's OWN ethertype, REPEATED IN THE BODY
[20..23] per-sender monotonic sequence
[24..63] 0x5A — the same known byte the SPI and I2C labs already assert on
```

The self-referential ethertype is the sharpest field: **a frame that disagrees with *itself*
cannot be explained by anything benign.** A drop is a gap; a delay is a gap; a reorder is a
gap. But a header saying `0x88B6` over a body saying `0x88B5` means *the header came from one
frame and the body from another* — a stale buffer, a write-back that never landed, a ring
index that wrapped. Magic catches "nothing was written"; self-consistency catches "**the
wrong thing was written**," which is the harder and more dangerous case.

**Corruption is the assertion; loss is a statistic.** Sequence *gaps* are logged, never failed.

**⚠️ ROLLOUT ORDER IS PART OF THE SPEC.** A receiver that enforces a field its senders do not
yet emit **will condemn the honest** — and here that is worse than an ordinary false alarm:

> ⭐ **The false positive of this detector is INDISTINGUISHABLE from its true positive.**
> `CORRUPT, magic=0` is exactly what a frame DMA'd to guest address 0 looks like: *a buffer
> that was never written.* Someone hitting it cannot tell "my peers haven't upgraded" from
> "the RX ring is writing to address zero" — so they go hunting a QEMU bug that isn't there,
> or, far worse, **conclude the check is broken and delete it.** That is how a good assertion
> gets removed by the very people it was protecting.

So: **Phase 1 — every sender EMITS, nobody ENFORCES** (emitting is harmless to a peer that
ignores the body, so Phase 1 cannot break anything; enforcement ships gated, default off).
**Phase 2 — once all four emit, every receiver enforces on the same run**, and the runner
treats `ENET-LAB3 CORRUPT` as a hard fail.

rt1180 documented the same hole from its own side rather than assuming it away: *"what
the surviving peers do when a beacon they were counting on stops is completely
unexercised."* Every node still on the segment had already found its peers and stopped
needing them, so a stalled wire and a healthy one look identical from the outside.

**The assertion that closes it: a node must ARRIVE *after* the departure, and be SEEN.**
That is the only thing that can tell "the segment absorbed the loss" apart from "the
segment quietly stalled and nobody was left to notice."

### ✅ The 4th node — the oracle that cannot be pre-satisfied (2026-07-14)

91emulator built it in an hour: `0x88B8`, AF_PACKET on the FEC, and it is now
`profiles/imx91-evk-enet-lab3` scheduled at **t+450 — thirty seconds after mcx is gone.**

A **survivor** only ever shows that the segment still works *for someone already on it*.
Its ring is programmed, its peers are known, its descriptors are armed — and a departure
re-tests none of that. This node has none of it. It has to build every bit of that state
against a segment that has just lost a member.

> ⭐ **A SURVIVOR PROVES THE SEGMENT STILL WORKS FOR WHOEVER WAS ALREADY ON IT. ONLY A NEW
> ARRIVAL PROVES IT STILL WORKS FOR SOMEBODY WHO WASN'T.**

Its peer list is `0x88B6,0x88B7` — the two that never leave. **mcx (`0x88B5`) is deliberately
absent:** requiring the departed node would make this node's PASS fail for exactly the reason
the lab exists to demonstrate. An assertion that cannot survive its own premise is not an
assertion. (Guarded by `test_the_post_departure_joiner_does_NOT_require_the_departed_peer`.)

**And the honest limit, written down before anyone reads the green:** this node *sees* the
survivors; the survivors **cannot see it.** mcx, rt1180 and imx95 each compile a fixed peer
list of `0x88B5/0x88B6/0x88B7`, and `0x88B8` is in none of them — they are *structurally
blind* to it. So this buys "the post-departure segment **carries** a new joiner", not "the
fleet **noticed** one." The absence of a complaint from a node that cannot complain is not
evidence of anything. Adding `0x88B8` to three peer lists is **asked, not assumed.**

### 🔴 A path in a live worktree is not an artifact (2026-07-14)

The fleet's rule is **NEVER TEST A BINARY YOU DID NOT JUST BUILD** — rt1180, 91 and 93 each
shipped a quiet, plausible, wrong number after testing a stale binary, and rt1180 named why
it survives: *"'my new assertion found nothing' is a very comfortable thing to believe."*

**Holobench cannot obey that rule.** It never builds any of these artifacts; it consumes them
from five repos it does not own (CLAUDE.md §7). **Its binaries are stale by construction.** The
only question is whether it *notices*.

It did not. On 2026-07-13/14, every artifact in this lab had drifted from what the bus said:

| artifact | announced | what was actually there |
|---|---|---|
| 91 `enet-lab3-imx91.cpio.gz` | `242b361df9` / `42a68dca` (and the **commit matched exactly** — the announcement was honest) | an **uncommitted** rebuild at that path, then **another one an hour later**: `1a8f3301` → `f569d0c4` |
| rt1180 `netc-lab3-0x88B6.elf` | pinned in a *comment* | recommitted **three times**, incl. `b3bb27d0c4`, unannounced |
| mcx `node-mcx.elf` (staged copy) | — | **two generations stale — it predated the freshness fix entirely** |

The mcx one is the sharpest: the lab would have run a beacon with **no replay detection, in the
very run that reports on replay detection.**

> ⭐ **A PATH IN A LIVE WORKTREE IS NOT AN ARTIFACT.** If a peer announces a *commit*, consume
> the **commit** (`git show <sha>:<path>`), never the path.
>
> ⭐ **NEVER RUN A LAB AGAINST AN ARTIFACT WHOSE HASH YOU DID NOT VERIFY** — the farm-shaped
> dual of the fleet's rule.

`boot.pin` is an md5 per artifact, and a mismatch **refuses to launch**. Not a warning: *a
warning printed above a green result is a warning nobody reads.* A pin naming a field that does
not exist is a **load error**, because a check that silently guards nothing is precisely the bug
the pin was added to prevent.

### The flag day is retired — ask the sender, not the frame (2026-07-14)

The phase gate below (emit-then-enforce) was the right *concern* and the wrong *mechanism*.
91emulator found the escape, and it needs no coordination at all:

> ⭐ **A PEER THAT HAS EVER EMITTED A VALID BODY CANNOT STOP KNOWING HOW.**

Arm the body check **per peer**, on first evidence that peer *can* emit. Never emitted → it
hasn't upgraded; count it, say so once. Has emitted, and now `magic=0` → **that is a buffer that
was never written**, and it is caught. Identical bytes on the wire; benign in one case, a hard
fail in the other. **Ask the sender, not the frame.**

So `ENFORCE_CORRUPT` is gone from the scorer. Enforcement is now decided **per node**, because
whether a `CORRUPT` line is trustworthy is a property of that node's *firmware*, not of the
calendar. A self-arming node's CORRUPT is a hard fail; an unconditionally-enforcing node's is
reported and **not scored** — a red we cannot trust is worse than no red, because it gets the
check deleted by the people it protects.

### And freshness, not validity — every stale frame is a good frame

rt1180 found the thing that made the whole checkable-body design half-useless:

> **The corruption is not a mangled frame. It is an *old* one, delivered again.**

A dropped frame leaves the RX descriptor pointing at a **stale buffer** — which holds a
*previously valid* frame from the same peer. Its magic is right. Its embedded ethertype agrees
with its header *perfectly*. Its `0x5A` pattern is intact. **Every integrity check that asks "is
this frame well-formed?" answers yes — because it is.** Three trees, one number:

| node | well-formedness failures | replays caught by the sequence check |
|---|---|---|
| rt1180 | node still **PASSES** | 26 |
| 91 | **0** | 40 |
| mcxn | **0** | 490 |

The fix was a field all four nodes had emitted since their first commit and **none of them ever
read backwards**: the per-sender sequence must **strictly increase**. `seq <= last` is a stale
buffer (and must not update `last`, or it drags the baseline backwards with it). `seq > last+1`
is loss — a statistic, never a failure.

> ⭐ **THE QUESTION WAS NEVER "IS THIS FRAME VALID." IT WAS "IS THIS FRAME *NEW*."**

### The assertion only the coordinator can make

mcxn947 observed that on a *node*, a stalled ring and a departed peer produce the identical
signal — a rejected frame never refreshes a peer's liveness — and called that the honest answer,
because from the segment's point of view they *are* the same event. **Correct for a node.**

Not correct for the **coordinator**: *we scheduled the departure.* A heartbeat gap that brackets
`t+stop_at` is the event we ordered. **A gap anywhere else is a wire fault that no node on the
segment is in a position to name**, and it is now a hard fail (`unscheduled_gaps`). It is the one
thing the farm can see that none of the boards can.

### The instrument is audited before the subject

mcxn947 lost **six** departure runs and **not one** was the firmware. Every failure was in the
instrument, and every one *looked* like a model bug. All four are now guards in the scorer:

1. **An observer that cannot keep up with its subject is observing its own backlog.** Their node
   printed every frame, fell behind the wire, and a *killed* peer's stale backlog kept refreshing
   its liveness timer. "The peer is still here" and "I am 40,000 frames behind" were the same
   observation.
2. **A liveness timeout that is too short does not fail safe — it manufactures departures that
   never happened.** Their 300-spin hold declared a *live* peer dead 75 times. So the scorer no
   longer hardcodes a timeout and hopes: it **measures each node's own inter-beat spacing** and
   refuses to score any node whose normal quiet period is within 3× of the timeout.
3. **Never test a binary you did not just build** → `boot.pin`, above.
4. **A kill that reaches the wrapper and not the process is not a kill.** Five of their "failed"
   departure runs were scoring a departure that never happened. Holobench departs over QMP, which
   is a different mechanism — *and that is exactly why it must still be checked, because a
   mechanism you trust is a mechanism you stopped verifying.* The departed node is now **verified
   dead** before its departure is scored, and an un-killable peer is **inconclusive, not a
   failure.**

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

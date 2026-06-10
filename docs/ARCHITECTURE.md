# Holobench — Architecture

This document describes *how* Holobench works. The non-negotiable boundary
(stock QEMU interfaces only) is in `CLAUDE.md` → Prime Directive; everything
here lives inside that boundary.

---

## 1. Mental model

A **session** is one reserved virtual board: a single QEMU process launched from
a **profile**, plus the bridges that expose it to a browser. The backend
**orchestrator** is a fleet manager over sessions. The browser is a thin client
with four panels — console, LCD, controls, introspection — each backed by one
backend bridge. Board-specific knowledge lives entirely in profiles.

```
profile (data) ──▶ Session manager ──▶ QEMU process
                          │
        ┌────────────┬────┴─────┬──────────────┬───────────────┐
   console bridge  display    power/files    introspection   scheduler
     (serial)      (VNC)       (QMP+host)      (QMP/info)     (reservations)
        │            │            │                │               │
        └────────────┴── mediated WS / REST ───────┴───────────────┘
                                 │
                              Browser
```

## 2. Components

**Orchestrator / control daemon.** Owns the fleet. Allocates session IDs and
per-session resources (overlay image, port range, work dir). Launches QEMU from
the resolved profile command line. Holds the QMP connection. Exposes the REST +
WebSocket API. Supervises process health and tears sessions down.

**Session manager.** The session lifecycle state machine (§4) and the
abstraction seam for isolation. v1 implementation = subprocess + overlay +
ports; the interface is written so a container/namespace implementation can be
swapped in without API changes.

**Console bridge.** Connects a QEMU serial chardev (exposed as a pty or unix
socket per the profile) to a browser WebSocket; the frontend renders it with
xterm.js. Supports multiple UARTs (A-core console, M-core, debug) as separate
tabs when the profile declares them. This bridge is **read/write of terminal
bytes only** — it is not a control channel.

**Display bridge.** QEMU is started with a VNC server on a per-session port. The
backend runs websockify to bridge RFB → WebSocket; the frontend renders it with
noVNC. This is the "LCD / framebuffer" panel. Absent on profiles with no
display device (panel hidden).

**Power / lifecycle.** Mapped to QMP and process control:
- *Reset (warm):* QMP `system_reset`.
- *Pause / resume:* QMP `stop` / `cont`.
- *Power cycle (cold):* `quit` (or process kill) then relaunch from golden state.
- *Reinstall:* restore the golden disk image, then cold-boot.

**File injection.** Four mechanisms, all stock, chosen per use case and exposed
in the UI the way the hardware farm exposes NFS/TFTP:
- *virtio-9p* (`-fsdev`/`-virtfs`): a host dir mounted live in the guest. Best
  for "drop a file, it's there." No reboot.
- *user-net TFTP* (`-netdev user,tftp=DIR`): the guest's u-boot `tftpboot` pulls
  from a per-session dir — mirrors the farm's "upload your Image/dtb via TFTP."
- *host NFS export:* guest mounts it to `/mnt` — mirrors the farm's "NFS → /mnt."
- *image swap:* attach/replace the SD/eMMC image — for flashing a full image /
  "reinstall system."

**Introspection.** Read-only QMP / HMP `info`-class queries surfaced as a panel
(see §6). The differentiator vs. physical hardware.

**Scheduler / reservations.** Maps to the farm's reserve-and-countdown model.
For local single-user it's trivial; for shared deployment it enforces time
limits and fair-share. Designed-in from the session abstraction even if Phase 4.

## 3. How each capability maps to stock QEMU

| Holobench capability | Stock QEMU mechanism | Notes |
|---|---|---|
| Serial console | `-chardev` (pty/socket) + `-serial` | one per declared UART |
| LCD / framebuffer | `-vnc :N` / `-display vnc` | per-session port |
| Warm reset | QMP `system_reset` | |
| Pause / resume | QMP `stop` / `cont` | |
| Cold cycle / reinstall | `quit` + relaunch / restore image | golden image per profile |
| Live file drop | virtio-9p (`-fsdev`,`-virtfs`) | no reboot |
| Custom Image/dtb | user-net `tftp=` | u-boot `tftpboot` |
| Shared `/mnt` | host NFS export | guest mounts |
| Flash full image | `-drive`/`-sd` swap | |
| Memory map / devices | HMP `info mtree` / `info qtree`, `qom-list` | read-only |
| Status / events | QMP `query-status`, event stream | |
| Source debug | `-gdb tcp::N` gdbstub | |

Everything in the right column ships in upstream QEMU. Nothing here requires a
model change.

## 4. Session lifecycle

```
RESERVED ─▶ LAUNCHING ─▶ RUNNING ⇄ PAUSED
                            │           │
                            ├──▶ RESETTING ──▶ RUNNING
                            ├──▶ REINSTALLING ──▶ RUNNING
                            └──▶ STOPPING ─▶ STOPPED ─▶ RELEASED
```

- **RESERVED:** scheduler granted a slot; resources allocated, QEMU not yet up.
- **LAUNCHING:** QEMU spawned, QMP handshakes, bridges attach.
- **RUNNING / PAUSED:** normal operation; `stop`/`cont` toggle.
- **RESETTING / REINSTALLING:** warm reset or golden-image restore + cold boot.
- **STOPPING / STOPPED / RELEASED:** teardown; resources reclaimed; on timeout
  the scheduler forces this path.

State transitions are driven by mediated control verbs from the API — never by
the browser touching QMP directly.

## 5. Isolation & multi-session

- One QEMU **process** per session.
- One **overlay image** per session (qcow2 backed by the profile's golden image)
  so writes never touch the shared base and "reinstall" = drop the overlay.
- One **port range** per session (QMP, VNC, gdbstub, any user-net forwards).
- The session abstraction hides all of this so a future namespace/container
  backend is a drop-in.

## 6. Beating the hardware (the introspection panel)

The hardware farm has a live *video* of the board — useless for emulation, but
the screen real estate is valuable. Holobench fills it with what emulation can
show that silicon can't:
- **Memory map** (`info mtree`) and **device tree** (`info qtree` / `qom-list`).
- **QMP event stream** (resets, device events, watchdog, etc.).
- **gdbstub** attach for source-level debugging of guest code.
- **Snapshots** (savevm/loadvm) — "bookmark this boot state and jump back."
- **Register / peripheral state** views derived from `qom-get`.

All read-only, all via stock interfaces, all genuinely impossible on a real
board farm. This is the "why use the virtual one" story.

## 7. Security model

- **QMP and serial control sockets never leave the backend.** The browser
  receives only: terminal byte streams (console), RFB frames (display), and a
  fixed set of scoped control verbs (reset/pause/cont/reinstall/upload). It can
  never issue an arbitrary monitor command.
- **Uploads are hostile until proven otherwise:** size caps, confinement to the
  session's injection dir, no path traversal, type checks for Image/dtb.
- **Per-session isolation** (process/image/ports) limits blast radius.
- **Auth** gates everything before shared deployment; the API is auth-aware from
  day one with a swappable provider.

## 8. The profile is the contract

Profiles (`profiles/*.yaml`, schema in `docs/BOARD_PROFILES.md`) are the only
place board-specific facts live. The orchestrator resolves a profile into a QEMU
command line and a set of bridge configs. Adding a board is authoring a profile;
it is never a code change. The facts in a profile come from the board's emulator
repo (`CLAUDE.md` §7) — Holobench does not invent them.

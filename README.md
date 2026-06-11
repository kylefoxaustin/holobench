# Holobench

**A board-farm-style web front end for QEMU machine models. A "virtual EVK."**

> Project name token used throughout this repo: `Holobench`.
> To rename: find-replace `Holobench` → your chosen name across all files.

---

## The one-liner

You reserve a board, you get a browser tab with a live serial console, the
board's LCD framebuffer, power/reset controls, and a way to push boot files
onto it. Except there is no board. It's a QEMU machine model running on a
server, presented through the exact UX of a hardware board farm.

If you've used NXP's aiotcloud board farm (WEVK Remote Console: console window,
framebuffer panel, Power / File / System management), Holobench is that — but
the silicon is emulated, free, infinitely cloneable, and available before tapeout.

## Why this doesn't already exist

Every QEMU front end in the wild (libvirt/virt-manager, Proxmox, Cockpit,
AQEMU, the various noVNC-in-a-container projects) is **VM/datacenter-oriented**:
it abstracts a guest as a virtual machine — disk image, vCPU, RAM, lifecycle.

None of them present the **board abstraction**: "this is an i.MX 95 EVK, here is
its debug UART, here is its LCD, here is how you `tftpboot` a custom Image onto
it, here is the power button." That framing only becomes possible once
application-processor machine models with a working framebuffer, serial, and
boot flow actually exist — which is exactly what the companion emulator repos
have spent months building. Holobench is the front end that gap was waiting for.

## What it is / isn't

**Is:**
- A standalone web app that launches, supervises, and drives QEMU instances.
- A thin, machine-agnostic control plane over **stock QEMU interfaces only**.
- Board-aware via declarative **profiles**, not code. New SoC = new profile.

**Isn't:**
- Not a fork of QEMU. Not a patch to any machine model.
- Not coupled to i.MX. The i.MX 95/93/91 are the first profiles, not the design.
- Not a general datacenter VM manager. The unit of work is a *board*, not a VM.

## The Prime Directive (read this before touching anything)

Holobench drives the emulators **exclusively through standard, upstreamable
QEMU mechanisms**: QMP (standard commands only), standard serial chardevs,
standard VNC/display, standard block/SD/virtfs/netdev backends, and the
standard gdbstub.

It must **never** require a custom QMP command, a custom device, a machine-model
patch, or a forked QEMU. The companion machine models are being upstreamed to
qemu.org; any coupling would both block that upstreaming and chain Holobench to
a forked binary. See `CLAUDE.md` → *Prime Directive* for the full rule and the
escalation path when something is genuinely missing from a model.

## Architecture at a glance

```
  Browser                         Holobench backend                 QEMU instance
  ┌──────────────┐   WebSocket    ┌────────────────────┐  QMP sock   ┌───────────────┐
  │ xterm.js     │◀──────────────▶│  Console bridge     │◀──────────▶│  -serial      │
  │ (UART panel) │                │                     │            │  chardev      │
  ├──────────────┤   WS (RFB)     ├────────────────────┤            ├───────────────┤
  │ noVNC        │◀──────────────▶│  Display bridge      │◀──────────▶│  -display vnc │
  │ (LCD panel)  │                │  (websockify)        │            │               │
  ├──────────────┤   REST/WS      ├────────────────────┤  QMP        ├───────────────┤
  │ controls     │◀──────────────▶│  Orchestrator        │◀──────────▶│  reset/stop/  │
  │ (power/files)│                │  + Session manager   │            │  cont/quit    │
  ├──────────────┤                ├────────────────────┤  9p/TFTP/   ├───────────────┤
  │ introspect   │◀──────────────▶│  File injection      │  NFS/image │  virtio-9p /  │
  │ (mem/qom/    │                │  Introspection (QMP) │◀──────────▶│  usernet /    │
  │  events)     │                │                     │            │  block        │
  └──────────────┘                └─────────┬──────────┘            └───────────────┘
                                             │ reads
                                   ┌─────────▼──────────┐
                                   │  profiles/*.yaml    │  (the board contract)
                                   └────────────────────┘
```

Full detail: `docs/ARCHITECTURE.md`. Profile schema: `docs/BOARD_PROFILES.md`.

## Status

**Working.** Reserve a board in the browser, console into it, watch its LCD,
push files onto it, inspect its internals, and attach a debugger — backed by
QEMU i.MX SoC models, through stock interfaces only.

| Phase | Capability | State |
|---|---|---|
| 0 | Launch from profile + QMP control (all 3 boards boot to a Linux prompt) | ✅ |
| 1 | Live serial console in the browser (xterm.js, bidirectional) | ✅ |
| 2 | LCD / framebuffer panel (QMP `screendump`) | ✅ |
| 3 | File injection — virtio-9p (`/mnt`) + user-net TFTP | ✅ |
| 4 | Reservations + "remaining time" countdown + extend + cold reinstall | ✅ |
| 5 | Introspection — memory map, device tree, live QMP events, gdbstub, snapshots | ✅ |
| 6 | Auth scaffold + offline-vendored UI (deploy hardening) | ◐ in progress |

Boards: **i.MX 91 / 93 / 95** (9p on 91/93/95; everything else on all three).

## Quickstart

```bash
cd backend && python -m venv ../.venv && . ../.venv/bin/activate
pip install -e .
holobench serve                 # → http://127.0.0.1:8080
```
Open the URL → pick a board → **Reserve & Boot** → the serial console streams
live and the right-hand tabs show LCD / Memory / Devices / Events / Debug /
Snapshots / Files. Type at the guest shell; drop a file to see it at `/mnt`.

Boot artifacts live in `assets/<profile-id>/` (kernel `Image`, `dtb`, and an
`initrd.cpio.gz` built by `tools/make-initramfs.sh` from a BSP rootfs). Each
profile's `qemu.binary` points at that board's locally-built `qemu-system-*`.

CLI (headless, no UI):
```bash
holobench profiles                    # list boards
holobench command imx91-evk           # preview the resolved QEMU command line
holobench launch imx91-evk --hold 30  # boot + prove QMP control, print console
holobench ps | status | reset | stop  # act on a running session by id
```

## Multi-user / auth

Holobench runs **open** (no login) until you create a user — then it enforces
per-user login and **session ownership** (you only see/control your own boards;
admins see all). Dependency-free (stdlib PBKDF2 + HMAC-signed tokens).

```bash
holobench user add alice --admin        # prompts for a password; switches auth ON
holobench user add bob                   # a regular user
holobench user list
export HOLOBENCH_SECRET=…                 # stable token-signing key across restarts
holobench serve                          # UI now shows a login screen
```
Quotas (0 = unlimited): `HOLOBENCH_MAX_PER_USER`, `HOLOBENCH_MAX_SESSIONS`.
Users live in `data/users.yaml` (gitignored) or `$HOLOBENCH_USERS`.

## Run as a container (self-contained "virtual EVK")

The fat image bakes in the board's QEMU build + boot artifacts, so a user just
runs it and opens a browser — no setup, no host QEMU:

```bash
docker/build.sh imx91-evk            # bakes the imx91 qemu + assets -> holobench:imx91-evk
docker run --rm -p 8080:8080 holobench:imx91-evk
# open http://localhost:8080 → Reserve & Boot
```

`docker/build.sh <qemu-board> [asset-boards…]` stages a clean build context: the
Holobench app, the chosen board's forked `qemu-system-aarch64`, and the real boot
artifacts (`Image`/`dtb`/`initrd.cpio.gz`/`disk.img`). The image uses TCG (no
`/dev/kvm` needed). Add `-e HOLOBENCH_TOKEN=…` to require auth. `docker/compose.yaml`
runs a pre-built image. Path overrides: `HOLOBENCH_QEMU` (binary) and
`HOLOBENCH_ASSET_ROOT` (assets) — set automatically inside the image.

> The fat image embeds the emulator session's *forked* QEMU (the i.MX models
> aren't upstreamed yet) — fine for local use/demos; revisit publishing once the
> models land in stock QEMU. For a tiny image, stage no qemu/assets and mount
> them at run time instead.

## Repo layout

```
holobench/
  README.md  CLAUDE.md  ROADMAP.md
  docs/        ARCHITECTURE.md  BOARD_PROFILES.md
  profiles/    imx91-evk.yaml  imx93-evk.yaml  imx95-evk.yaml  virt-smoke.yaml
  backend/     pyproject.toml
    holobench/ profiles/ (models+loader)  session/ (command+manager+control)
               bridges/ (console tap)  api/ (FastAPI app)  cli.py
    tests/     pytest (profile + command-resolver unit tests)
  frontend/    index.html (React+htm+Tailwind+xterm.js)  vendor/ (offline deps)
  tools/       make-initramfs.sh  init-shell  init-busybox
  assets/      <profile-id>/ boot artifacts (gitignored)
```

## Related repos (the boards Holobench drives)

- `kylefoxaustin/qemu-imx95`  (branch `imx95-netc`)
- `kylefoxaustin/qemu-imx93`  (branch `imx93-dev`)
- `kylefoxaustin/qemu-imx91`

These are the source of truth for each board's machine type, serial topology,
display device, and boot flow. Holobench consumes them via profiles. It never
modifies them.

## License

TBD (MIT or Apache-2.0 recommended — keeps it reusable and contribution-friendly).

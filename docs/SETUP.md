<!-- SPDX-License-Identifier: GPL-2.0-or-later -->
# Holobench setup / "build me a board" — design

**Status: design + foundation landing.** Goal: a brand-new user with nothing but
Docker can get a board running. Decided (2026-06-16): an **in-app web wizard**,
**auto build-from-source** of the (still-forked) QEMU, and **two artifact paths** —
bring-your-own NXP BSP *or* an all-OSS demo image.

This exists because Holobench ships **no prebuilt images and no NXP BSP** (see
`docs/DEPLOY.md` — those are non-redistributable). So "build me" assembles the
redistributable pieces locally and points the result at artifacts the user is
allowed to have.

## The three hurdles a new user faces

1. **The forked QEMU.** The i.MX/MCX machine models aren't upstream yet, so there's
   no stock `qemu-system-aarch64` that knows `imx95-19x19-evk`. GPL, so we *can*
   build it — from the public fork at a pinned commit.
2. **Bootable artifacts.** kernel/dtb/rootfs, plus (i.MX95 only) the **M33 SM
   firmware** (`m33_image.elf`) — without it Linux SCMI fails and the board won't
   boot. The NXP-built versions are non-redistributable.
3. **The glue.** clone → build qemu → build image → supply artifacts → boot.

## QEMU source is per-board config (absorbs the upstreaming transition)

Kyle is upstreaming each model (~2 weeks out). The build engine must not hardcode
the fork, so the QEMU source for each board is a config entry that moves through
three stages with a one-line edit and **zero code change**:

```yaml
# tools/build-sources.yaml  (per board id)
imx95-evk-sd:
  qemu:
    repo: https://github.com/kylefoxaustin/qemu-imx95.git   # stage 1: fork
    ref:  25223218cd                                        #          pinned commit
    target_list: aarch64-softmmu
  # stage 2 (after upstreaming): repo: https://gitlab.com/qemu-project/qemu.git
  #                              ref:  v<release-with-imx95>
  # stage 3 (once in distros):   stock: { min_version: "10.1" }  -> NO build at all
```

- **Stage 1 — fork (today):** clone the public fork @ pinned commit, build a GPL
  `qemu-system-aarch64` in a Docker builder stage.
- **Stage 2 — upstream:** point `repo`/`ref` at qemu-project at the release that
  carries the model. Same build path; just a cleaner source.
- **Stage 3 — stock:** when the model is in a distro's qemu, declare a
  `stock: {min_version}` and the engine skips the build entirely — the apt
  `qemu-system-arm/-aarch64` already in the image suffices.

Pinned commits (verified 2026-06-16, public fork tip == local model tip):
imx91 `qemu-imx91@imx91-dev f816301c5e`, imx93 `qemu-imx93@imx93-dev cc125f516d`,
imx95 `qemu-imx95@imx95-netc 25223218cd`, mcxn947 `mcxn947qemu@mcxn947 f6831ff3aa`.
(Source of truth is each emulator session; reconfirm on the bus before a release.)

## Build engine (the foundation — both CLI and the wizard call it)

`tools/build-me.sh <board> [--demo|--bsp DIR] [--plan]` and a multi-stage
`docker/Dockerfile.buildme`:

1. Read `build-sources.yaml[<board>]`.
2. **Stage A (builder):** `git clone --depth … <repo>`, `git checkout <ref>`,
   `./configure --target-list=<target_list> --disable-docs …`, `make` → the GPL
   qemu binary. (Skipped if the board declares `stock:`.)
3. **Stage B (runtime):** the §DEPLOY distributable image (OSS app + that qemu),
   `HOLOBENCH_ASSET_ROOT=/artifacts`. No NXP BSP — the build guard still applies.
4. Artifacts at run time: `--bsp DIR` mounts the user's BSP; `--demo` fetches the
   OSS demo set (below). `--plan` prints the resolved steps without building.

The forked-qemu build is the slow part (~20–40 min) and fully cached after the
first run (keyed on repo+ref), so a re-`build-me` of the same board is fast.

## Artifacts: BYO BSP vs OSS demo

- **BYO BSP (works now):** the user drops their own NXP-built
  `Image`/`*.dtb`/rootfs(/`m33_image_M2.elf` for 95) into the mounted volume,
  laid out per board id. Always faithful; requires NXP access.
- **OSS demo (phased):** a fully-redistributable bootable set per board so someone
  with *no* NXP artifacts can still boot — mainline/Yocto kernel + a buildroot/
  busybox initramfs, published as GitHub **release assets** (all OSS, freely
  shippable). Start with **i.MX91/93** (plain direct-kernel). **i.MX95 is the open
  problem:** it needs the M33 SM (NXP) for SCMI — a fully-OSS 95 demo needs either
  an OSS SM stand-in or a model that tolerates SCMI-absent boot (escalate to the 95
  session, Prime Directive §2). Until then the 95 wizard path is BYO-BSP only.

## Web wizard (wraps the engine)

When Holobench has no launchable board, the UI shows **Set up a board**:

- `GET /api/setup/status` — docker present? which boards are built/cached? demo
  available? BSP detected?
- `POST /api/setup/build {board, source: demo|bsp, bsp_path?}` — runs `build-me.sh`,
  streams progress over a WS (reuse the console WS pattern).
- On success the board appears in the picker / auto-launches.

Security: the build runs server-side and touches the Docker socket — gate it to
admin/first-run, validate `board` against the profile list, never pass a
client-supplied repo/ref (only `build-sources.yaml` entries).

## Sequencing

1. **Foundation (this doc + landing):** `build-sources.yaml`, `Dockerfile.buildme`,
   `build-me.sh` (with `--plan`). CLI-buildable from a public fork, no host qemu.
2. **OSS demo — i.MX91/93:** redistributable kernel+initramfs as release assets;
   `--demo` fetches them. 95 tracked as an escalation.
3. **Web wizard:** `/api/setup/*` + the first-run UI, calling the engine.
4. **Upstream flip:** as each model lands upstream, move its `build-sources` entry
   to stage 2/3. No engine changes.

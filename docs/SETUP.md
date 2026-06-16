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

## Compliance litmus (the rule the wizard must satisfy)

From the 95 session, the one-line test for the whole flow: **does any NXP-built
binary originate from Holobench (a holobench-hosted mirror/registry), or only from
the operator / NXP-direct-with-operator-credentials?** It must be the latter. We
host and ship **nothing** NXP; the operator's artifacts reach QEMU only through a
runtime mount they own. (A purely-local build that never leaves the operator's box
could even bake them in — but the mount approach keeps the boundary obvious and is
what we do.) Reminder: a pushed registry layer counts as distributed even after a
tag delete — purge registry-side, never just `docker rmi`.

## Artifacts: BYO BSP vs OSS demo

The wizard validates a per-board **manifest** before it will run a board and
**refuses if a required artifact is missing** (95's advice). The manifest is
derived from the profile — boot artifacts referenced by relative name + any
`{asset_dir}/…` in `extra_args` — so it stays correct as profiles change:

| board | required in `<bsp>/<board>/` |
|---|---|
| imx95-evk-sd | `Image`, `imx95-19x19-evk.dtb`, `disk.wic`, **`m33_image_M2.elf`** (SM fw — only SCMI provider, REQUIRED) |
| imx93-evk-sd | `Image`, `imx93-11x11-evk.dtb`, `disk.wic` |
| imx91-evk-sd | `Image`, `imx91-11x11-evk.dtb`, `disk.wic` |

`GET /api/setup/manifest?board=…[&bsp=DIR]` returns `{required, present, missing,
ok}`; the wizard shows ✓/✗ per file.

Three ways the operator points at artifacts (all keep NXP bits off our servers):
**(a) local folder** — pick a dir of per-board files; the manifest validates it
(implemented). **(b) fetch from NXP** — the operator gets the bytes from nxp.com
themselves (design below, from the 95 session). **(c) build the BSP** — link the
NXP Yocto steps; produces the same dir (future, doc link).

### (b) NXP-credential fetch UX (design — 95 session)

Rule: **nothing NXP transits or is stored on Holobench** — credentials and bytes
are operator↔NXP only; we ship URLs + scripts + a validator, never binaries or
tokens. Two modes:

- **b1 — browser hand-off (preferred, zero credential handling):** the wizard
  shows the manifest + a per-file **source link out to nxp.com** (BSP/Yocto or
  prebuilt demo page). The operator logs in with *their* NXP account, accepts the
  EULA (operator action — the wizard must **not** click through for them),
  downloads to their machine, then points the wizard at the file(s); the validator
  checks present + sha256 and only then enables Run. Holobench never sees the login,
  EULA, or bytes.
- **b2 — local fetch script (headless/CI):** ship `tools/fetch-nxp.sh` that runs
  **on the operator host** (never our server), reads `NXP_USER`/`NXP_TOKEN` from the
  operator's own env, curls each manifest URL from nxp.com directly into their
  `/artifacts`, then validates. Never persist/log the token; scrub it from echoed
  commands.

Guardrails (live next to the litmus rule): Holobench hosts **no** NXP binaries and
**no** mirror (only links to nxp.com); stores **no** NXP credentials (operator-env
only, never server-side, never logged); EULA acceptance is the operator's (link
out, never auto-accept); any audit log records only "operator fetched <manifest> at
<time>", never bytes or creds. 95 source map: i.MX95 `Image`/`imx95-19x19-evk.dtb`/
`disk.wic` from the NXP i.MX Yocto BSP (or NXP prebuilt demo image, login+EULA);
`m33_image_M2.elf` is built **M=2 from NXP imx-sm source** — link the operator at
the imx-sm repo + the M=2 build line, not at a binary. (`fetch-nxp.sh` skeleton +
the optional known-good-sha256-per-file validator schema are the next build step.)

- **BYO BSP (works now):** the user drops their own NXP-built
  `Image`/`*.dtb`/rootfs(/`m33_image_M2.elf` for 95) into the mounted volume,
  laid out per board id. Always faithful; requires NXP access.
- **OSS demo (plumbing implemented; bundles pending):** a fully-redistributable
  bootable set per board so someone with *no* NXP artifacts can still boot —
  mainline/upstream kernel + generic dtb + buildroot/busybox rootfs, published as a
  GitHub **release asset** (all OSS). The mechanism is live:
  - `oss_demo: {url, sha256}` per board in `tools/build-sources.yaml` (empty until a
    bundle is published; `SetupManager.boards()` exposes `oss_demo: bool`).
  - `tools/fetch-oss-demo.sh <board>` downloads + sha256-verifies + extracts a
    bundle into the asset dir; `build-me.sh --demo` and the wizard's *OSS demo*
    option call it. Until a url is set it reports "not available yet" cleanly.
  - `tools/build-oss-demo.sh <board> <oss-dir>` packages a staged OSS artifact dir
    into the bundle + prints the sha256 and the `oss_demo:` snippet (and refuses if
    an NXP M33/imx-sm binary is in the dir).

  **The gating dependency is the boot recipe** — *which* OSS kernel/dtb actually
  boots each model. That is owned by the emulator session (Prime Directive §7,
  never guess); they are almost certainly producing exactly this as part of
  **upstreaming**, so the bundle source is likely their upstream-boot artifacts.
  **i.MX91/93 — supported** (plain direct-kernel; 93 is LIVE, 91 same recipe).
  **i.MX95 — definitively NOT possible** (95 session's verdict): the 95 boot is
  SCMI/SM-gated — Linux needs the M33 System Manager (NXP) as its only SCMI
  provider, so no mainline kernel boots standalone (93 has no SM → direct CCM/ANATOP
  → mainline boots; that's the difference). The i.MX95 wizard path is therefore
  **BYO-BSP only** (see the credential-fetch flow below), and its zero-NXP
  "see-it-run" tile is the bare-metal `tests/hello-imx95`, not a Linux demo.

### Validator manifest schema (95 session) — for the credential/BYO path

Per board, one entry per required file; the wizard renders `source.kind` to the
right UI (`build` = run-it button, `url` = fetch, `byo` = point-at-file) and keeps
Run disabled until every required file is present (and sha256-matched where given):

```yaml
artifacts:
  - name: m33_image_M2.elf
    sha256: <optional known-good>      # set -> integrity-checked; omitted -> presence-only
    required: true
    source: { kind: build, repo: https://github.com/nxp-imx/imx-sm, build: "make cfg=mx95evk M=2" }  # reproducible, NO creds
  - name: Image
    required: true
    source: { kind: byo, hint: "NXP i.MX Yocto BSP build, or prebuilt demo image (nxp.com login+EULA)" }
  - name: imx95-19x19-evk.dtb
    required: true
    source: { kind: byo, hint: "same Yocto BSP build" }
  - name: disk.wic
    required: true
    source: { kind: byo, hint: "Yocto core-image-* .wic, or prebuilt demo image" }
```

`tools/fetch-nxp.sh` (runs on the **operator** host, ships with the repo) =
**validate + build-the-buildable + hand-off-the-gated**: it builds the SM firmware
from `imx-sm` (no creds), validates each file's sha256, and for EULA-gated files
prints the hand-off (the operator downloads with their own nxp.com login). The two
design honesties (95): the BSP kernel/dtb/rootfs/.wic are behind a login + per-user
EULA (a cookie/session flow, so b1 browser hand-off is the real path, not a blind
token curl); the SM firmware is open-source-buildable (no creds). The optional
`url`+`NXP_TOKEN` mode is operator-env only, never logged.

**b1 source map (i.MX95, 95 session) — implemented.** `setup.nxp_manifest()` adds a
`source_url` + `hint` per file and an EULA `guidance` block; the wizard's *Fetch
from NXP* mode renders per-file link-out buttons. Link to **stable landing pages,
not deep download URLs** (the file links are session/EULA-gated and rot per release):
- `disk.wic` / `Image` / `imx95-19x19-evk.dtb` → the i.MX95 EVK **Getting Started
  guide** (routes to the current prebuilt demo image + the Yocto BSP). A stranger
  with an NXP login but no Yocto build wants the **prebuilt `.wic`** (kernel + dtb
  live in its boot partition; or build all three via the Yocto BSP / `imx-manifest`).
- `m33_image_M2.elf` → `github.com/nxp-imx/imx-sm`, `make cfg=mx95evk M=2` (open
  source, **no login**; stays `kind=build`).
- EULA notes surfaced: free nxp.com account required; accept the Software Content
  Register/EULA per-user (never auto-accepted); confirm a matching kernel/dtb/.wic
  set via the i.MX Linux release notes. Holobench links out only — hosts no NXP
  binaries and no mirror.

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

### Closing the build→boot seam (in-place boot)

So "Build" lands the board in the *current* app (no second `docker run`), a
successful build also **installs** it host-side: the QEMU binary is extracted from
the built image (`docker create` + `docker cp` from
`/opt/holobench/qemu/qemu-system-aarch64`) to `qemu-builds/<board>/` (gitignored),
and — for the OSS-demo path — the artifacts are fetched into the board's asset dir.
The launch path then prefers that binary: `build_command` resolves the QEMU as
`rt.qemu_binary` (the wizard-built one) > `$HOLOBENCH_QEMU` > the profile path, and
`POST /api/sessions` passes `setup.installed_qemu(board)`. `SetupManager.boards()`
reports `installed: true`, and the wizard shows **▶ Boot it now**, which launches the
board as an ordinary session in place. Net flow: Build → (qemu compiled + extracted,
artifacts fetched) → Boot it now → the running app launches the board.

## Sequencing

1. **Foundation (this doc + landing):** `build-sources.yaml`, `Dockerfile.buildme`,
   `build-me.sh` (with `--plan`). CLI-buildable from a public fork, no host qemu.
2. **OSS demo — i.MX91/93:** redistributable kernel+initramfs as release assets;
   `--demo` fetches them. 95 tracked as an escalation.
3. **Web wizard:** `/api/setup/*` + the first-run UI, calling the engine.
4. **Upstream flip:** as each model lands upstream, move its `build-sources` entry
   to stage 2/3. No engine changes.

# Holobench — Roadmap & Backlog

The shared work plan for the Holobench Claude session and the three emulator
Claude sessions (`qemu-imx95`, `qemu-imx93`, `qemu-imx91`). Each phase has a
goal, tasks, **testable acceptance criteria**, and explicit cross-repo
dependencies so sessions don't block or guess.

Ground rule for every task: it stays inside the Prime Directive
(`CLAUDE.md` §2) — stock QEMU interfaces only, no model coupling, no forked
binary. If a task can't be done that way, it's not a Holobench task; it's an
escalation to an emulator repo.

Legend: `[H]` Holobench session · `[E:95/93/91]` emulator session · `🔒` blocked
until a dependency clears.

---

## Milestone map

| Milestone | Phase | Headline |
|---|---|---|
| v0.1 | 0–1 | Launch a board, console into it in the browser |
| v0.2 | 2–3 | LCD panel + file injection (9p/TFTP/NFS/reinstall) |
| v0.3 | 4 | Multi-board fleet + reservations |
| v0.4 | 5 | Introspection panel (beats physical hardware) |
| v1.0 | 6 | Auth + hardening + deployable as a service |

---

## Phase 0 — Bring-up

**Goal:** launch one QEMU from a profile and prove QMP control. No UI.

Tasks
- `[H]` pydantic models for the profile schema in `docs/BOARD_PROFILES.md`;
  load + validate `profiles/imx95-evk.yaml`.
- `[H]` profile → QEMU command-line resolver (`flash` / `direct-kernel` modes;
  `command_template` escape hatch).
- `[H]` launch QEMU as an `asyncio` subprocess; capture serial to a session log
  file; allocate a per-session QMP socket + port range.
- `[H]` attach `qemu.qmp` client; `holobench launch <id>` / `reset <session>` /
  `stop <session>` CLI verbs.
- `[E:95]` confirm and return: exact `-M` machine string, A-core count, boot
  mode + artifact names (flash.bin / Image / dtb / rootfs), the canonical launch
  line, and the chardev id(s) the UART(s) are wired to.
- `[H]` replace every `CONFIRM` marker in `profiles/imx95-evk.yaml` with the
  confirmed values.

**Acceptance criteria — done when:**
- `holobench launch imx95-evk` boots the model from a clean checkout (assets
  present) and the captured serial log shows the board reaching a u-boot or
  Linux prompt.
- QMP `query-status` returns `running`.
- `holobench reset <session>` issues `system_reset` and the serial log shows the
  board restarting.
- `profiles/imx95-evk.yaml` has zero remaining `CONFIRM` markers.
- No machine-model file was touched; no custom QMP command was used.

🔒 **Blocked on `[E:95]`** for the machine string + boot artifacts. Everything
else `[H]` can scaffold in parallel against a placeholder.

---

## Phase 1 — Console (v0.1)

**Goal:** serial console in the browser.

Tasks
- `[H]` console bridge: serial chardev (pty/unix socket) ↔ WebSocket; handle
  terminal resize (cols/rows).
- `[H]` minimal React shell + xterm.js panel; one board, one session.
- `[H]` per-declared-UART tabs when the profile lists more than one.

**Acceptance criteria — done when:**
- Browser shows the boot log streaming live.
- User can type at the u-boot prompt and at a Linux login and get an
  interactive shell.
- If the profile declares multiple UARTs, each is a working tab.
- The browser holds **no** QMP/control socket — only the mediated terminal WS
  (verify: there is no code path from the client to an arbitrary monitor cmd).

---

## Phase 2 — Framebuffer (v0.2)

**Goal:** the LCD panel.

Tasks
- `[H]` launch QEMU with `-vnc` on the per-session port.
- `[H]` display bridge: websockify (embedded) ↔ noVNC panel.
- `[H]` hide the panel when `display.enabled: false`.
- `[E:95]` confirm the model drives a display device and what it renders (if
  display modeling is incomplete, profile sets `display.enabled: false` and this
  phase degrades gracefully — escalate per `CLAUDE.md` §2).

**Acceptance criteria — done when:**
- The board's framebuffer renders live in the browser for a display-enabled
  profile.
- A `display.enabled: false` profile shows no panel and no error.

---

## Phase 3 — File injection (v0.2)

**Goal:** push files onto the board the way the hardware farm does.

Tasks (ship in this order)
- `[H]` virtio-9p live share (`-fsdev` / `-virtfs`, `mount_tag` from profile).
- `[H]` user-net TFTP (`-netdev user,tftp=DIR`) for `tftpboot` of custom
  Image/dtb — mirrors the farm's TFTP upload.
- `[H]` host NFS export the guest mounts at `/mnt` — mirrors the farm's NFS flow.
- `[H]` image swap / "reinstall": restore golden image, cold boot.
- `[H]` upload mediation: size caps, path confinement to the session dir, no
  traversal, Image/dtb type checks.

**Acceptance criteria — done when:**
- A file dropped via the UI appears in the guest over 9p with no reboot.
- From u-boot, `tftpboot` of a user-supplied Image succeeds against the
  session's TFTP dir.
- The guest can mount the NFS export at `/mnt`.
- "Reinstall" restores the golden image and the next boot is clean.
- Uploads outside the session dir or above the size cap are rejected.

---

## Phase 4 — Fleet (v0.3)

**Goal:** many boards at once, reserved.

Tasks
- `[H]` multi-session: N isolated QEMU processes; per-session overlay image
  (qcow2 over golden) + per-session port range.
- `[E:93]` / `[E:91]` return the same facts as `[E:95]` did in Phase 0.
- `[H]` fill `profiles/imx93-evk.yaml` and `profiles/imx91-evk.yaml`; clear all
  `CONFIRM` markers.
- `[H]` reservation/scheduler: slot length, max length, expiry teardown, a
  "Remaining Time" countdown matching the farm.

**Acceptance criteria — done when:**
- Two different boards (e.g., imx95 + imx93) run simultaneously with fully
  independent consoles and displays.
- Sessions are isolated: writes in one never affect another's base image.
- A reservation that hits its time limit is torn down automatically and its
  resources reclaimed.
- imx93/imx91 profiles have zero `CONFIRM` markers.

🔒 imx93/imx91 profile completion **blocked on `[E:93]`/`[E:91]`**; the fleet
machinery itself is unblocked and testable with multiple imx95 sessions.

---

## Phase 5 — Beat the hardware (v0.4)

**Goal:** fill the dead "video" real estate with things silicon can't show.

Tasks (all read-only, all stock interfaces)
- `[H]` memory map view (`info mtree` via `human-monitor-command`).
- `[H]` device tree / object model browser (`info qtree`, `qom-list`/`qom-get`).
- `[H]` live QMP event stream panel.
- `[H]` gdbstub: launch with `-gdb tcp::PORT`, surface the connect string.
- `[H]` snapshots: `savevm`/`loadvm` bookmarks of boot state.

**Acceptance criteria — done when:**
- The memory map and device/object tree render for a running session.
- QMP events (reset, device, watchdog…) appear live as they fire.
- An external gdb attaches to the advertised stub port.
- A user can snapshot a boot state and jump back to it.

### Virtual camera (feed host images through the ISI) — ✅ DONE (all three, byte-exact)

**Shipped 2026-06-12.** All three boards capture staged host frames **byte-exact,
end-to-end**, turnkey through the Camera panel:

| Board | Sensor | Geometry | In-guest path | Verified |
|---|---|---|---|---|
| imx95-evk-sd | ov5640 (module) | 640×480×6 | `insmod /mnt/ov5640.ko && /mnt/imx95-isi-capture cap /dev/video0` | PASS 5/5 |
| imx91-evk-sd | mt9m114 (built-in) | 1280×720×3 | `/mnt/imx91-isi-capture cap /dev/video0` | PASS 5/5 |
| imx93-evk-sd | mt9m114 (built-in) | 1280×720×3 | `/mnt/imx93-isi-capture cap /dev/video0` | PASS 5/5 |

Holobench stages the GPL-2.0 static capture helper (and, for the 95, the matching
`ov5640.ko`) into the session 9p share → the guest runs it from `/mnt`. The 95
needs the module because its only modeled sensor (ov5640) is `=m` and absent from
the golden rootfs; 91/93 use the built-in mt9m114. Camera "arms" only when frames
are staged (empty frames dir is fatal in the ISI model), so a fresh board boots
normally; stage frames + reboot to arm. The whole capture path is bundled — no
media-ctl/v4l2-ctl needed (the core rootfs ships neither, and media-ctl can't even
drive the 95 crossbar).

#### How it got here (the investigation)

**95 enabled** (`1474277`); the frame FEED was validated **byte-exact** by
95emulator (5/5 staged frames DQBUF'd through the ISI). The in-guest capture
client turned out to be a real BSP/driver matter, not a Holobench one:

- **95 (8-ch crossbar ISI): `media-ctl` cannot drive it.** 95 ran the full
  `-l/-V` sequence on the golden full image — the formats apply but `STREAMON`
  EPIPEs; the crossbar's per-stream `link_validate` needs programmatic
  `SUBDEV_S_FMT`. The **only** working capture is a small V4L2 C client
  (`tests/camera/v4l2_cap.c` → 655 KB **static** aarch64 ELF, 0 deps): byte-exact
  on the same boot media-ctl failed. `imx-image-full` *does* ship media-ctl/
  v4l2-ctl — but they're insufficient for this driver.
- **91 (single-pipe ISI): no capture tool in the rootfs at all — CONFIRMED.**
  The 91 BSP builds only **imx-image-core**: `gst-launch` is present but the
  **`v4l2src` plugin is missing** (gst-alone tested → "no element v4l2src"), and
  there's **no `media-ctl`, no `v4l2-ctl`**. Sensor `-device` is unnecessary (the
  `imx93.isi` model streams without the sensor subdev).
- **93: symmetric with 91 — CONFIRMED.** Same `imx93.isi`, same core-rootfs with
  no turnkey capture tool.

**Unanimous (2026-06-12): all three boards need a bundled static capture client.**
No shipped turnkey path exists — 91/93 ship no usable capture tool in core; the
95's media-ctl EPIPEs on its crossbar. Each emulator has a **validated, byte-exact,
fully-static, zero-dep** `v4l2_cap.c` in its **public** repo and offered it
(91: `tests/camera-imx91/v4l2_cap.c`; 93: `tests/camera-imx93/v4l2_cap.c`;
95: `tests/camera/v4l2_cap.c`). 91/93 are single-pipe (simple); 95 is the crossbar
variant (does per-stream `SUBDEV_S_FMT`). Build: `aarch64-linux-gnu-gcc -O2 -static
-o imxNN-isi-capture <file>`; run: `./imxNN-isi-capture cap /dev/video0`.

**DECISION FOR KYLE — how to provide the in-guest capture client (coupling posture):**
1. **Holobench-authored helper** (Apache-2.0, one static aarch64 binary covering
   single-pipe + crossbar) — cleanest boundary, but reproduces the union of the
   three clients; the 95 crossbar setup is non-trivial (95 spent ~10 boots),
   effectively needs their source as reference.
2. **Bundle the emulators' `v4l2_cap.c`** (public, validated, static, zero-dep) —
   fastest, proven; mild coupling + a licensing check (what license those files
   carry vs Holobench's Apache-2.0 distribution).
3. **Document-only** — the panel feeds frames; the user brings their own client.
4. **BSP fix** — 91/93 grow `imx-image-full`; a working media-ctl path for the 95
   crossbar (escalation; not near-term).

Until decided: 95 `camera.capture_hint` states the truth (programmatic client
required); 91/93 stay disabled (the FEED works, but no shipped capture tool). This
is purely the capture-client decision — the feed side is done and validated.

#### (original notes)
### Virtual camera (feed host images through the ISI) — *plumbing DONE, enablement pending*

*Added 2026-06-11. All three emulator forks shipped an ISI host-frame-source
(91 `8281c330bb`, 93 `a569c85e87`, 95 `a15281f2559`). The user-facing interface
is identical across all three; only model internals + frame geometry differ —
a board-agnostic-with-profile-data feature.*

**Built 2026-06-12 (board-agnostic, tested):** `CameraSpec` profile model;
`command.py` emits `-global driver=<isi_type>,property=frames,value=<session
frames dir>` + an optional sensor-`dtb` override; per-session frames dir;
REST frame upload/list/delete with exact-size validation (a mismatched frame
silently shows the gradient, so we reject it); a **Camera** UI panel. Disabled
in every profile until per-board capture geometry is pinned.

**Enablement blocker (per 95emulator, hw/display/imx95_isi.c):** frame geometry
is **runtime-derived** from whatever the booted sensor dtb + V4L2 client
negotiate (`CHNL_IMG_CFG` / `CHNL_OUT_BUF_PITCH`), **not** a device constant; the
model does a **format-agnostic opaque byte copy** (raw must be byte-exact to the
negotiated fourcc/stride — no host convert) and is **LAUNCH-ONLY** (stage frames,
then reboot). 95's validated path used ov5640 @ 640×480×6, but our drone-sizer
deploy ships **os08a20 / ox03c10 / ox05b1s / ap1302**, no ov5640. So enabling on
the 95 = pick a sensor dtb → boot → read the guest `G_FMT` geometry → set
`camera.{dtb,width,height,bytes_per_pixel}` → verify a staged `.raw` flows. Per
board: confirm each `isi_type` + sensor dtb with its emulator (91/93 facts still
pending on the bus).

- **Standard interface (confirmed by all three E-repos):** a `frames` string
  property on the board's ISI, set via `-global driver=<isi-type>,property=frames,value=<path>`
  where `<path>` is a dir of sorted `*.raw` frames or one file of back-to-back
  raw frames (the model loops). Whole-frame host reads — **never a chardev**
  (their hard-won lesson: char-socket backend reads ~4KB/dispatch → multi-MB
  frames crawl + deadlock).
- **Per-board geometry → goes in the profile** (frames must match exactly or the
  model falls back to its gradient): 91 = 1280×720×**3bpp**; 95 = 640×480×**6bpp**
  (8-channel `imx95.isi`, separate model); 93 = single-channel width×bpp. ISI
  type string differs per board (`imx93.isi` / `imx95.isi`) — `-global` needs the
  dotted type name.
- **Use case:** drive the guest V4L2 → NPU vision pipeline with real images —
  impossible on a fixed physical board farm. Frames scan out in lexical filename
  order, looping (zero-pad: `frame000.raw`…).
- **Follow-ups:** confirm 91/93 `isi_type` + geometry + sensor dtb; verify + flip
  `camera.enabled` per board; optional live-swap (95 would add a property setter
  that reopens — small model change, not there today); optional host-side
  image→raw convert once a target fourcc/stride is fixed per sensor.

---

## Phase 6 — Harden (v1.0)

**Goal:** deployable as a shared "virtual EVK" service.

Done (2026-06-12 hardening pass)
- `[x]` Auth: enforced login + per-user session ownership (REST + WS), 8 h token
  expiry, login brute-force throttle, persistent signing key, WS `Origin`/CSWSH
  gate. (Pluggable IdP/OIDC still a future swap.)
- `[x]` Boundary audit + fixes: closed client-controlled asset-path argv
  injection (`HOLOBENCH_ALLOW_CLIENT_ASSETS` off by default); confirmed QMP =
  unix socket, gdb = localhost, browser gets only bytes/PNG/scoped verbs.
- `[x]` Per-session resource caps on the QEMU child: `RLIMIT_CORE=0` always,
  opt-in `HOLOBENCH_NICE` / `HOLOBENCH_MEM_CAP_MB`.
- `[x]` Deploy guide: `docs/DEPLOY.md` (TLS reverse-proxy, secret, origins,
  resource caps/cgroups, quotas, env reference, hardening checklist).

- `[x]` **Per-session cgroup v2 isolation** (`session/isolation.py`): each board's
  QEMU runs in its own cgroup with hard `memory.max`/`pids.max`/`cpu.max` it can't
  exceed; opt-in (`HOLOBENCH_CGROUP=1`, auto-detects the delegated parent),
  graceful no-op when unavailable, torn down on session end. Validated live
  (memory+pids on a delegated user session). Network is already isolated by
  design (user-mode slirp NICs — no host iface); per-session work dir gives FS
  containment.

Remaining
- `[ ]` Optional **netns/mount-ns** per board (slirp already covers network; this
  is defense-in-depth, not load-bearing). The cgroup backend covers the
  CPU/mem/pids DoS surface that mattered most.
- `[ ]` Audit log (who booted/reset/reinstalled what); admin user-mgmt over the API.
- `[ ]` Disk quota for per-session overlays/uploads.

**Acceptance criteria — done when:**
- With auth enabled, unauthenticated REST/WS requests are rejected. ✅
- A documented `deploy` path stands the service up with isolation intact. ✅ (docs;
  cgroup per-session isolation is the remaining hardening step)
- The mediation boundary is audited: no client path reaches raw QMP/serial. ✅

---

## Cross-repo dependency matrix

| Need | Owner | Consumed by | Phase |
|---|---|---|---|
| imx95 machine string, UARTs, boot line, display | `[E:95]` | imx95 profile | 0, 2 |
| imx93 machine string, UARTs, boot line, display | `[E:93]` | imx93 profile | 4 |
| imx91 machine string, UARTs, boot line, display | `[E:91]` | imx91 profile | 4 |
| Stock QMP/serial/VNC/9p/usernet/gdbstub support | upstream QEMU | all bridges | all |

If a board lacks a needed standard interface, that is an **emulator-repo issue**
(model exposes it via a standard mechanism, ideally upstreamable) — never a
Holobench workaround.

---

## Cross-session coordination protocol

> **Decision (2026-06-09, Kyle):** coordination is **message-bus (chat) only**.
> Holobench does **not** open GitHub issues or write any file into the emulator
> repos — this upholds `CLAUDE.md` §7 (locked: "the qemu repos stay pristine")
> and the no-pollute rule. The GitHub-issue steps below are **superseded**: the
> live ask happens on the bus; the durable record is the board profile in this
> repo. Emulator sessions still document their launch contract in their own
> READMEs at their discretion — Holobench only *reads* that.

The Holobench session and emulator sessions share state through **files in
these repos**, not chat:

1. Holobench opens a board-onboarding request listing the facts it needs (the
   `[E:*]` rows above). Record it as a GitHub issue in the emulator repo.
2. The emulator session answers by documenting the launch contract in its own
   README (machine string, UART topology, display presence, canonical boot
   line, known-good QMP commands).
3. Holobench transcribes those into the board profile and deletes the matching
   `CONFIRM` markers. A profile with no `CONFIRM` markers is the signal that a
   board is "onboarded."

The profile is the durable contract. A `CONFIRM` marker anywhere means "not yet
verified with the source of truth — do not rely on this value."

---

## Suggested GitHub labels

`phase:0`…`phase:6` · `blocked:emulator` · `area:backend` · `area:frontend` ·
`area:profile` · `prime-directive` (for anything touching the coupling boundary).

---

## Appendix — Phase 0 kickoff issue (paste-ready)

> **Title:** Phase 0 — Bring-up: launch i.MX 95 from a profile and prove QMP control
>
> **Labels:** `phase:0`, `area:backend`, `area:profile`, `blocked:emulator`
>
> **Context**
> First milestone toward the v0.1 target ("launch a board, console into it").
> Goal is a headless bring-up: load a profile, launch QEMU, drive it over QMP.
> No UI yet. Stays inside the Prime Directive (`CLAUDE.md` §2): stock QEMU
> interfaces only, no machine-model changes, no forked binary.
>
> **Tasks**
> - [ ] pydantic profile models matching `docs/BOARD_PROFILES.md`; load/validate
>       `profiles/imx95-evk.yaml`.
> - [ ] profile → QEMU command-line resolver (modes + `command_template`).
> - [ ] launch QEMU via `asyncio` subprocess; capture serial to a log; allocate
>       per-session QMP socket + port range.
> - [ ] attach `qemu.qmp`; add CLI verbs `launch` / `reset` / `stop`.
> - [ ] **(blocked on qemu-imx95)** obtain: exact `-M` string, A-core count,
>       boot mode + artifact names, canonical launch line, UART chardev id(s).
> - [ ] clear all `CONFIRM` markers in `profiles/imx95-evk.yaml`.
>
> **Acceptance criteria**
> - [ ] `holobench launch imx95-evk` boots from a clean checkout; serial log
>       reaches a u-boot/Linux prompt.
> - [ ] QMP `query-status` → `running`.
> - [ ] `holobench reset <session>` triggers a visible restart in the log.
> - [ ] zero `CONFIRM` markers remain in the imx95 profile.
> - [ ] no machine-model file touched; no custom QMP command used.
>
> **Blocked by**
> - qemu-imx95 board-onboarding request (machine string + boot artifacts).
>
> **Definition of done**
> Headless launch + reset of the i.MX 95 model works end-to-end from the profile,
> the imx95 profile is fully verified, and the Prime Directive is intact.

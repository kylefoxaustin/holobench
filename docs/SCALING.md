<!-- SPDX-License-Identifier: Apache-2.0 -->
# Scaling Holobench — density & capacity planning

How many emulated boards fit on a host, what gates it, and the knobs to tune.
Numbers below are **measured**, not guessed (see the load test in §Measure).

## TL;DR

- **CPU is the binding constraint, not RAM** — because these are **TCG** guests
  (aarch64-on-x86, no KVM) and the i.MX profiles pass **`cpuidle.off=1`**, an
  *idle* board **busy-spins** at **~1.24 host cores** instead of going to WFI.
- **Measured (imx95-evk-sd, full distro):** ~**1.15 GB RSS/board**, and **25
  idle boards saturated a 32-core box (97%)** purely spinning. RAM was a
  non-issue (28 GB / 94 GB).
- So today: ~**25 boards per 32-core box** (CPU-bound by idle spin). If the
  emulator gains a working **WFI idle** (drop `cpuidle.off=1`), idle boards fall
  to ~0 CPU → density flips to **RAM-bound (~50–80/box, more with KSM)** and you
  can heavily **oversubscribe** idle boards — the real virtual-EVK-farm economics.

## What it costs per board (measured: imx95-evk-sd)

| Resource | Per board | Notes |
|---|---|---|
| RAM (RSS) | ~1.15 GB | guest is `-m 4G` but lazily allocated; KSM dedups across identical distros |
| Disk | thin qcow2 overlay | golden `.wic` is shared read-only (page-cached once) |
| Host ports | 0 | QMP + serial are unix sockets; net is slirp (no host port/board) |
| **Idle CPU** | **~1.24 cores** | **only because of `cpuidle.off=1`** — the scaling wall |

## Knobs

| Var | Effect | Default |
|---|---|---|
| `HOLOBENCH_CGROUP=1` (+ `_MEM_CAP_MB`/`_CPU_CORES`/`_PIDS_MAX`) | Hard per-board cgroup caps so one board can't starve the host | off (see DEPLOY.md) |
| `HOLOBENCH_MAX_CONCURRENT_LAUNCHES` | Cap concurrent in-flight launches — prevents a "Reserve & Boot" stampede (qemu-img + exec + boot CPU) | 0 (unlimited) |
| `HOLOBENCH_LAUNCH_STAGGER_S` | After each launch, free the admission slot only after this delay → boots start in **waves** instead of all at once | 0 |
| `HOLOBENCH_LAZY_SERIAL=1` | Attach the serial tap only while a console is open (ref-counted), instead of an always-on pump per board — resource hygiene at density (trade-off: no boot scrollback before you connect) | off (full history) |
| `HOLOBENCH_MAX_SESSIONS` / `_MAX_PER_USER` | Hard session-count quotas (`429` at capacity) | 0 (unlimited) |

Recommended starting point for a busy single host: `HOLOBENCH_CGROUP=1`,
`HOLOBENCH_MAX_CONCURRENT_LAUNCHES=4`, `HOLOBENCH_LAUNCH_STAGGER_S=8`,
`HOLOBENCH_LAZY_SERIAL=1`, plus a `HOLOBENCH_MAX_SESSIONS` sized to your core
count (until the cpuidle spin is fixed, ~0.8 × cores is a sane ceiling).

## The cpuidle lever (emulator-side)

`cpuidle.off=1` is in the profiles because the i.MX models otherwise wedge at
boot (the idle path — PSCI/SCMI `CPU_SUSPEND` via the M33 System Manager —
doesn't yet resume cleanly). Fixing it (even a single shallow "standby"
idle-state that halts the core to the next IRQ) is the single biggest density
multiplier: it turns "idle boards cost ~1.24 cores each" into "idle boards cost
~0," i.e. from CPU-bound ~25/box to RAM-bound with heavy oversubscription. This
is tracked with the emulator repos, not Holobench.

## Beyond one host

`SessionManager` is single-process / single-host / in-memory. ~hundreds on one
fat box is vertical scaling (knobs above). A real multi-host fleet needs an
orchestration layer (a scheduler placing boards across nodes + a shared control
plane) — the session abstraction was built so a container/namespace backend can
drop in as the stepping stone. That's a future "scale-out" phase.

## Measure it yourself

`/tmp`-style load harness used for the numbers above: boot N boards staggered
under cgroups, then report avg RSS/board, host RAM delta, KSM dedup, and idle
host-CPU/board. To reproduce, boot N `imx95-evk-sd` sessions via `SessionManager`
with `HOLOBENCH_CGROUP=1`, wait for `login:`, then sample `/proc/stat` over ~10 s
with all boards idle and sum `VmRSS` across the QEMU pids. (32-core/94 GB host,
N=25 → 25/25 booted, 1158 MB/board, 97 % CPU idle-spin, ~1.24 cores/board.)

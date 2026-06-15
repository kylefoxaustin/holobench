<!-- SPDX-License-Identifier: GPL-2.0-or-later -->
# Scaling Holobench — density & capacity planning

How many emulated boards fit on a host, what gates it, and the knobs to tune.
Numbers below are **measured**, not guessed (see §Measure).

## TL;DR

- The density wall was an **idle-board host-CPU spin**, and it was **not** the
  A55s or `cpuidle.off=1` (both red herrings) — it was the **M33 System Manager**
  (a Cortex-M) busy-spinning ~1 host core per idle board. Two independent causes,
  both now fixed in the emulator:
  1. an **upstream QEMU Cortex-M WFI/WFE bug** (commit `d238858bff6`), fixed
     upstream by `6fd2fcdc61b` *"teach arm_cpu_has_work about halting reasons"*;
  2. the **stock (M=1) SM firmware's debug-monitor busy-poll** — a sibling cause,
     removed by building the SM firmware **M=2**.
- **Measured A/B (imx95-evk-sd, full distro, 32-core/94 GB host, N=25):** idle
  host-CPU/board **108.8% → 14.9%** (≈7.3×), i.e. **25 idle boards: 27.2 → 3.7
  host cores**. The limiter **flips CPU → RAM**. RSS is unchanged (~1.2 GB/board).
- So density goes from **~25–29 boards/box (CPU-bound by the spin)** to
  **RAM-bound** — ~50/box on this 94 GB host and **linear in box RAM** thereafter
  (more with KSM). That's the real virtual-EVK-farm economics.

> **Status / caveat.** The fix lives in the i.MX95 emulator fork (95's
> `qemu-system-aarch64` cherry-pick + M-core `power_state`/`halt_reason` hygiene)
> plus an **M=2** SM firmware build — it is **not upstream/shipping yet**.
> Holobench's profile deliberately stays on **stock interfaces**; whether to point
> `imx95-evk-sd` at the M=2 firmware is a coupling-posture decision (Kyle's call,
> see ROADMAP) and is **not** baked in. The A/B below used a throwaway in-memory
> firmware override, not a profile edit.

## What it costs per board (measured: imx95-evk-sd)

| Resource | Per board | Notes |
|---|---|---|
| RAM (RSS) | ~1.2 GB | guest is `-m 4G` but lazily allocated; KSM dedups across identical distros |
| Disk | thin qcow2 overlay | golden `.wic` is shared read-only (page-cached once) |
| Host ports | 0 | QMP + serial are unix sockets; net is slirp (no host port/board) |
| **Idle CPU (M=1 SM fw)** | **~1.1 cores** | the old wall — M33 SM spin (WFI bug + debug-monitor poll) |
| **Idle CPU (fixed qemu + M=2 fw)** | **~0.15 core** | spin gone; residual is timer ticks / settle, not a spin |

## The idle-spin root cause (emulator-side, now fixed)

The spin was **guest-invisible**: it never showed in guest counters or RSS, only
in host-side per-thread CPU (`top -H -p <pid>` or `/proc/<pid>/task/*/stat`
deltas — a "CPU N/TCG" thread pegged while the guest is idle). On the i.MX95 it
was always **CPU 6/TCG = the M33 System Manager**; the A55s already WFI-halt to
~0, and the M7 never takes an interrupt so it stayed clean.

Two mechanisms, same 1-core symptom:
1. **Upstream WFI bug** — `arm_cpu_has_work()` let the WFE *event register* gate
   WFI for M-profile; M-profile exception entry/return set that register and only
   WFE clears it, so an M-core that takes an IRQ then idles with `__WFI()` (never
   WFE) busy-spins forever. Generic (reproduces on stock `mps2-an505`); fixed
   upstream `6fd2fcdc61b`.
2. **M=1 SM debug-monitor busy-poll** — independent of the WFI bug; the stock SM
   firmware polls a monitor flag. Removed by an **M=2** firmware build (monitor
   only on console keystroke, idles via WFI).

Both are required to reach ~0: the qemu fix + M=2 firmware together. **Note**
`cpuidle.off=1` stays in the profiles (the A55 *deep* PSCI idle still hangs) but
it costs **no host CPU** — it was never the density lever.

> **Fleet note (other boards):** the same upstream WFI fix is correct hygiene
> everywhere but only matters if a board *runs an idling, interrupt-taking
> Cortex-M*. **i.MX93** is affected (NPU/ethos-u M33) but its spin is a *firmware
> busy-poll in a non-rebuildable ARM blob*, so it floors at ~1 core even after the
> fix — density lever there is lazy/on-demand NPU-firmware load. **i.MX91** is
> N/A (no Cortex-M instantiated; idle ≈ 11% of one core = ticks only).

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
`HOLOBENCH_LAZY_SERIAL=1`. Until the M33 fix is in the binary you run against,
size `HOLOBENCH_MAX_SESSIONS` to ~0.8 × cores (CPU-bound). Once it is, size to
RAM (~`usable_GB / 1.2`).

## Beyond one host

`SessionManager` is single-process / single-host / in-memory. ~hundreds on one
fat box is vertical scaling (knobs above). A real multi-host fleet needs an
orchestration layer (a scheduler placing boards across nodes + a shared control
plane) — the session abstraction was built so a container/namespace backend can
drop in as the stepping stone. That's a future "scale-out" phase.

## Measure it yourself

Harness `/tmp/density_ab.py` (used for the A/B below): deep-copies the
`imx95-evk-sd` profile, optionally swaps the M33 loader to the M=2 firmware,
boots N idle boards staggered, waits for `login:`, settles, then samples
**per-PID** host CPU (`/proc/<pid>/stat` utime+stime deltas) + RSS and computes
the CPU- vs RAM-bound ceiling. Per-PID sampling isolates the measurement from any
other QEMU on the box.

**A/B result — 2026-06-14, 32-core / 94 GB host, imx95-evk-sd full distro,**
**fixed qemu, `cpuidle.off=1`, 45 s settle, both legs N=25 (25/25 booted):**

| Leg | SM firmware | idle CPU/board | RSS/board | 25 boards | limiter | ceiling/box |
|---|---|---|---|---|---|---|
| **Before** | M=1 (stock) | **108.8%** (105.9–119.5) | 1200 MB | **27.2 cores** | CPU | ~29 |
| **After**  | M=2 | **14.9%** (13.8–17.1) | 1202 MB | **3.7 cores** | RAM | ~51 |

Single-board cross-check: M=1 109.2% / M=2 10.6% — per-board cost holds under 25×
load (no superlinear blow-up). Residual ~15% in the After leg is scheduling /
timer-tick overhead at 25× concurrency measured while systemd was still settling
post-login; it keeps dropping with a longer settle (single board → ~10%), and it
is **not** a spin.

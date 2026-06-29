# Fleet validation-doc standard (canonical — LIVING DRAFT)

**Status: living draft, NOT frozen.** Holobench holds the canonical because it
coordinates all four emulators and is **not itself a QEMU-upstream candidate**, so
the canonical can live here *without* becoming a dependency any emulator must carry
upstream. Each emulator repo keeps its **own self-contained copy** — zero runtime or
build dependency on holobench — because each QEMU must stand alone for upstream
review.

**Why this exists (the mission bar).** Coupled with holobench, each emulator stands
in for a real hardware board so developers can run their own code on it. The bar is
therefore: **no silent failure on a known-good IP block or routine.** This doc
standardizes how each emulator *reports* what it models, to what fidelity, and with
what evidence — so a developer (and an upstream maintainer) can trust the board.

v0 content reference: **`93emulator:docs/validation/`** (commit `417befac`) — the
most complete in the fleet. Grab its shape, not its chip-specific rows.

## Sequencing (do NOT propagate yet)

The operator upstreams **one qemu at a time** for maintainer feedback before the
next. So:

1. The **first-upstreamed repo (93, furthest along)** is the *reference experiment*
   — its docs are shaped by **real maintainer review**, not by guesswork here.
2. Once that's proven in review, **holobench distills the canonical** from it and
   **propagates self-contained copies** to 95 / 91 / mcx (each a standalone copy,
   adjusted for chip deltas).
3. Until then this draft tracks the converging shape; it is not a freeze.

## Where the docs live (fleet-standard paths)

Every emulator repo, under `docs/validation/`:

| File | Authored by | Role |
|---|---|---|
| `test-matrix.yaml` | **human** | SOURCE annotation — the parts CI cannot infer: each IP block's fidelity **tier**, evidence prose, caveats, and the **N/A** (not-present) list. |
| `gen-matrix.py` | tooling | Generator — merges `test-matrix.yaml` with the test harness log (e.g. meson `testlog.json`) and renders the matrix. |
| `test-result-matrix.md` | **GENERATED** | The rendered matrix. **Never hand-edited** — edit `test-matrix.yaml` or the tests, then regenerate. |
| `fidelity-audit.md` | human | Carried caveats + the honest-fault discipline (uncomputable IP returns an honest STATUS error, never a wrong-but-clean result). |

## Fidelity tiers (canonical)

| Tier | Meaning |
|------|---------|
| **A** | **Data path verified** — real data/compute flows through the block and is checked against a golden or reference (end-to-end in-guest and/or qtest with golden output). |
| **B** | **Driver bring-up** — the stock BSP driver probes and operates the block (registers, IRQs, basic transactions); correct for bring-up, no golden-verified host data path. QEMU's usual peripheral convention. |
| **C** | **Registration / stub** — present so the OS enumerates it and does not fault; minimal or no functional behaviour. |
| **N/A** | **Not present on this SoC** — documented so the absence is never mistaken for a gap. |

### The N/A rule (a silent-fail guard itself)

An IP block that does not exist on a chip is **`N/A — ABSENT`**, *never* a negative
or failing cell. (e.g. i.MX91 has no Ethos-U65 NPU, no M33, no 2nd A55; mcx boots no
Linux.) A false "negative" on absent hardware is itself a silent fail — it implies a
gap that isn't real.

## CI guardrail (assemble, never invent)

The matrix is **CI-generated** (no hand-sorting). But:

- **Pass/fail + evidence** come from the **actual test run** (the harness log).
- **Fidelity tier and caveats are HUMAN judgment** — CI cannot infer tier A vs B
  from a green test. CI **reads tier + caveats from `test-matrix.yaml`** and
  *assembles* the matrix; it must **never invent a tier**. Mis-tiering (claiming a
  data-path "A" for a block that only has driver bring-up "B") would itself be a
  silent fail.
- Rows with backing tests get their Status **stamped** from the log; the generator
  flags any test that has gone MISSING (a renamed/removed test = drift). Non-meson
  harnesses (soak / media / torture / functional-boot / usbredir) are **attested**,
  clearly marked as attested rather than CI-stamped.

## Matrix structure (the shape to mirror)

1. **Header**: a "GENERATED — do not hand-edit" banner; machine + branch; the
   mission-bar line; a pointer to `fidelity-audit.md`.
2. **Fidelity tiers** table (above).
3. **Test harnesses** table: `Harness | Location | Scope | Result` — one row per
   harness (unit / qtest / functional-boot / media-conformance / torture / soak /
   inter-QEMU links).
4. **Per-IP-block matrix**, grouped by subsystem: `Block | Tier | Status | Evidence
   | Notes`. Status is CI-stamped or attested; absent blocks are `N/A — ABSENT`.

## Not an upstream-submission artifact

For QEMU upstreaming, the maintainers consume `docs/system/arm/<chip>-evk.rst` + the
functional test — **not** `README.md` / `test-result-matrix.md`. These validation
docs (and the fleet-common README) are the **GitHub-fork / 10k-developer
distribution + fleet-management** artifacts: orthogonal to the upstream submission,
and the reason holobench can hold the canonical without coupling any emulator's
upstream.

---
*Maintained by the holobench session as fleet coordinator. Edits here are
proposals; the binding shape emerges from 93's first upstream review.*

# Holobench constants audit (2026-09-20)

> ⚠️ **This file was already stale when committed.** It was written mid-exchange and omitted
> two things sent minutes later — a third passing shape, and the reframe now at the top of the
> PASSES section. **An artifact written during a conversation captures it up to that point and
> then stops: the same decay as a commit message, with a later start date** (pai-sizer). The
> fix is not a better medium, it is going back and checking. Revision 2, 2026-09-20.

Every magic number in this repo that decides or scales something, classified. Produced during
a seven-round exchange with `pai-sizer`; the method lives in
`docs/fleet/validation-standard.md` rule 11, this file is the *instances*.

⚠️ **Why this file exists at all.** The findings below were derived on the fleet bus across
~100 KB of messages. A defect pai-sizer measured and posted on 2026-08-12 was re-discovered
the hard way six weeks later *by a session that had read it*. **The bus is where a finding is
discussed; a committed artifact is where it survives.** Rules without instances are unusable
and instances in commit messages are unfindable.

## How to read the columns

| axis | question | what answers it |
|---|---|---|
| **kind** | stale / transplanted / undocumented / hybrid | how the basis failed |
| **structure** | threshold (magnitude *is* a verdict) or range/magnitude | inspection |
| **exposure** | does real data sit near it? | measuring the **input**, not sweeping it |
| **validity** | which world did you sample? | stating the conditions |

⭐ **Sensitivity ≠ exposure.** Varying a constant and watching outputs move is a property of
the *model*. Exposure needs a plausible range for the *input* — a property of the world — so
**an undocumented constant's exposure is unmeasurable by construction.** "Unknown" ≠ "safe".

## FAILS — no defensible basis for the value

| constant | where | kind | structure | exposure |
|---|---|---|---|---|
| `HOLD_S = 75.0` | `tools/score-real-silicon.py` | undocumented | magnitude | **sets published counts** — 381/837 are 75 s of running |
| `SEND_EVERY_MS = 200` | `tools/l2beacon.py` | undocumented | magnitude | bounds frames/run |
| `QUIET_MS = 5000` | `tools/l2beacon.py` | undocumented | — | unknown |
| `qmp_timeout = 15.0` | `session/manager.py` | undocumented | **threshold** | **dormant** — 17× headroom, *scoped below* |
| `QUIET_MARGIN = 3.0` | `tools/run-enet-lab3.py` | **hybrid** | **threshold** | unmeasured |
| `BEACON_TTL_S = 900` | `labs/coordinator.py` | basis at *use site*, not definition | range | not load-bearing |
| `MARGIN_S = 180` | `tools/run-enet-lab3.py` | hybrid — direction anchored, magnitude not | range | not load-bearing |
| `default_minutes=60`, `max_minutes=240` | `profiles/models.py` | undocumented policy | — | unknown |

**`qmp_timeout` validity:** 1.5 s / 3.4 s / 0.9 s measured on `imx95-evk`, `imx93-evk`,
`imx95-evk-sd` (11.9 GB golden + qcow2 overlay — the case assumed slowest, actually fastest,
because overlay creation is O(1) not O(size)). Host load 4.86, **local ext4, one host.** A
loaded CI runner or NFS asset dir is outside that sample and the 17× does not extend to it.

## PASSES — and these are the point, not the failures

Treating "documented constant" as a synonym for "unexamined" trains uniform distrust rather
than discrimination.

⭐⭐ **ASK FIRST WHETHER THE CONSTANT HAS TO BE CHOSEN AT ALL** (pai-sizer's reframe, from
reading the third shape below). **Documentation is the fallback, not the standard.** Every
constant in the FAILS table is one that *had* to be chosen; the strongest pass is the one that
was never a choice. So for a new number the order is: *can this be given an external referent
and verified?* → if not, *can its consumer decline to rely on it?* → only then, *how do I
justify the value I picked?*

**Three shapes pass, by different routes** — the set matters more than any one, and the third
is the one to reach for:

- **`BEAT_TIMEOUT_S = 20.0`** — passes by **declining to be relied on**. Its consumer does not
  trust it: §0 of the scorer *measures* whether that timeout is defensible for each node and
  refuses to score the ones it is not. It exists because mcxn947 once tuned the equivalent
  value and manufactured 75 departures that never happened — *the number was never the
  problem, the fact that it was a guess was.*
- **`llm_prefill_util_factor = 0.10`** (pai-sizer's, recorded here because a reader of this
  file should see all three shapes, not only mine) — passes by **justifying the value**: it
  states a mechanism, an empirical range (*"LLM prefill achieves 5-15% of vendor peak due to
  small per-layer matmuls, MoE expert routing, KV writes"*), the value's position within that
  range, and an ADR fixing what it must *not* be multiplied against.
- **The wire contract** — `FRAME_LEN=64`, `BEACON_MAGIC`, the field offsets, the ethertype
  block. Not guesses at all: an **externally specified** protocol shared with 95emulator's
  `enet-lab3.c` and verified gate-for-gate against that source at a pinned commit. A constant
  with an external referent and a verification is in a different category from one with a
  comment.

## Not fixed, on purpose

None of the FAILS were replaced. **Substituting a fabricated justification for an
undocumented guess makes code look audited while changing nothing real** — the same
discipline that keeps an unearned `binary_pin` off a profile. Each is marked in place so the
next reader meets the gap, and logged here so a run that *can* measure one knows which to
revisit and why.

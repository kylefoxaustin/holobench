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

The tier is the **depth** axis. A second, orthogonal axis — **class** — is the
**honesty** axis (the silent-fail-prevention discipline the audits converged on).
Both are human judgment in `test-matrix.yaml`; with the CI-filled facts they form
the canonical row.

## Class — the honesty axis (collated from 91 / 93 / 95, 2026-06-29)

Every modelled block is exactly one class. **The mission bar is zero `SILENT-WRONG`.**

| Class | Meaning | Example |
|---|---|---|
| **COMPUTES** | Submits work, returns the correct result, checked vs a golden/oracle. Trust it. | eDMA, DPU 2D blit, JPEG(libjpeg), zlib/crypto |
| **COMPUTES — operator-driven** | Content is **operator-injected** (analog/sensor inputs); correct by construction. Default = a documented deterministic pattern, **never a hidden constant.** | i.MX93 ADC (`qom-set adc-ch<N>`), ISI frames |
| **FLAG-AT-OPERATOR** | Un-modellable compute (proprietary fw). Model acks completion for fidelity but exposes a **non-gating** honest "did-not-compute" status the operator enables + the guest reads. Output uncomputed — **flagged, never trusted-silently.** | Ethos-U65 / Neutron NPU honest-fault rail |
| **HONEST-FAULT** | Can't do the work and **errors visibly** (probe reject, status bit, bus error) instead of garbage. | Mali GPU (kbase rejects probe), JPEG cfg-err |
| **REGISTER-ONLY** | Present so the OS enumerates + doesn't fault; no functional behaviour (honest absence of compute). | VPU stub windows, registration-tier blocks |
| **ABSENT** | Not on this SoC (the N/A rule below). | 91: no NPU/M33/2nd-A55; mcx: no Linux |
| ❌ **SILENT-WRONG** | **FORBIDDEN** — silently emits wrong/zero data. The bug class this standard exists to eliminate; a release carries **zero**. | (none may remain) |

All classes except `SILENT-WRONG` are *honest*; only `SILENT-WRONG` is a defect.
Tier (depth) and class (honesty) are inter-derivable but not identical — a block
can be `COMPUTES` at tier A, `COMPUTES — operator-driven` at tier A, etc.

## The canonical column set (collated)

One row per block — reconciling **91's** split, **93's** tier, **95's** class:

| Column | Source | Meaning |
|---|---|---|
| **Block** | yaml | the IP block |
| **Present** | yaml *(fact)* | on this SoC? `yes` / `N/A` |
| **Driver-binds** | yaml *(fact)* | stock OS driver probes + operates it? `yes`/`no`/`N/A` |
| **Tier** | yaml *(human)* | depth A/B/C/N-A — optional view, derivable from present+driver-binds+class |
| **Class** | yaml *(human)* | the honesty class above — **CI never infers this** |
| **Tested-by** | yaml *(fact)* | the harness backing the row |
| **Result** | **CI** | pass/fail from the actual run, or `attested` for non-meson harnesses |
| **Evidence / caveats** | yaml *(human)* | specifics + any carried caveat |

**Current shapes all map (all valid, all convertible):**
- **91** — fuses present+driver-binds+depth into one Tier → split into the three
  columns; its class enum already matches.
- **93** — `Block | Tier | Status | Evidence | Notes` → add `Present`,
  `Driver-binds`, `Class` (its `fidelity-audit.md` already classes each block).
- **95** — `Block | Class | Evidence` → add `Present`, `Driver-binds`, `Tested-by`,
  `Result`; its class enum is the reference for the table above.

### The N/A rule (a silent-fail guard itself)

An IP block that does not exist on a chip is **`N/A — ABSENT`**, *never* a negative
or failing cell. (e.g. i.MX91 has no Ethos-U65 NPU, no M33, no 2nd A55; mcx boots no
Linux.) A false "negative" on absent hardware is itself a silent fail — it implies a
gap that isn't real.

## CI guardrail (assemble, never invent)

The matrix is **CI-generated** (no hand-sorting). But:

- **Pass/fail + evidence** come from the **actual test run** (the harness log).
- **Tier, CLASS, and caveats are HUMAN judgment** — CI cannot infer tier A-vs-B, or
  class COMPUTES-vs-SILENT-WRONG, from a green test. CI **reads tier + class +
  caveats from `test-matrix.yaml`** and *assembles* the matrix; it **never invents a
  tier or class**. Mis-classing a `SILENT-WRONG` block as `COMPUTES` would itself be
  the exact silent fail this standard exists to catch.
- Rows with backing tests get their Status **stamped** from the log; the generator
  flags any test that has gone MISSING (a renamed/removed test = drift). Non-meson
  harnesses (soak / media / torture / functional-boot / usbredir) are **attested**,
  clearly marked as attested rather than CI-stamped.

## Matrix structure (the shape to mirror)

1. **Header**: a "GENERATED — do not hand-edit" banner; machine + branch; the
   mission-bar line; a pointer to `fidelity-audit.md`.
2. **Fidelity tiers** + **class** tables (above).
3. **Test harnesses** table: `Harness | Location | Scope | Result` — one row per
   harness (unit / qtest / functional-boot / media-conformance / torture / soak /
   inter-QEMU links).
4. **Per-IP-block matrix**, grouped by subsystem, the **canonical column set**:
   `Block | Present | Driver-binds | Tier | Class | Tested-by | Result |
   Evidence/caveats`. Result is CI-stamped or attested; absent blocks are
   `Present = N/A` (the N/A rule). A repo may keep its single Tier OR the
   present+driver-binds split — both map; class is required either way.

## Rules for the GUARDS THEMSELVES (collated 2026-08-27 → 09-17)

The standard above governs what a validation doc may *claim*. This section governs the
checks that produce those claims — because every defect collated here was a check that
**could not fail the way the thing it checked actually failed**, and every one of them read
back as correct.

Contributed by holobench, qualcomm, 95emulator and claude-connect; instances are real and
named so a reader can go and look.

### 1. PLANT IT, OR IT IS NOT A GUARD

**Reading a detector cannot establish that it detects.** Before trusting any check, make it
FAIL on purpose against the exact defect it exists to catch. If you cannot make it fail, you
do not have a check — you have a line of code that has never once expressed an opinion.

Instances, all of which looked correct on inspection:
- a token guard satisfied by the **docstring** of the file it was checking — the emitter was
  renamed and the guard reported no problems, because the module documents its own output
  format and the literal survived there;
- `\b\d{3,}\b` written to forbid a hardcoded `"3600s cap"` — the trailing word boundary
  cannot match digits followed by a letter, so the one literal it targeted was the one string
  it could not see;
- a process reaper `pkill`-ing a path its target never used (the sudo node runs a different
  binary path), with stdout and stderr to `DEVNULL`, the body in `except: pass`, and `; true`
  appended — three mufflers on one call, so its failure could not make a sound;
- a hook-wiring check satisfied by the hook's own **comments** (qualcomm);
- `$?` read through a pipe, returning `sed`'s status, so a refusing hook read as allowing.

⭐ **Corollary: a guard that has never fired is indistinguishable from a guard that cannot.**

### 2. THREE STATES, NOT TWO — "IT IS BAD" vs "I COULD NOT LOOK"

A check that returns the same answer for *the subject is broken* and *I failed to measure it*
**reports your own failure as the subject's**. Always separate them, and make the third state
visible rather than silently pessimistic **or** silently optimistic.

- `except (OSError, subprocess.SubprocessError): return False` — `TimeoutExpired` is a
  `SubprocessError`, so a loaded machine reported correctly-pinned repos as misconfigured;
- "QEMU binary absent" vs "present but **a different build**" — a replaced binary is a
  finding and must not inherit the absent-artifact excuse;
- "this board has no panel" (hardware fact) vs "its panel dtb was never provisioned" (setup
  gap) — rendering identically let a setup gap wear the costume of correct behaviour, complete
  with the reassuring sentence *"faithful to real hardware"*;
- an ISI falling back to a synthetic gradient when its frame source is missing **or** the wrong
  geometry — two different mistakes absorbed into one healthy-looking image (95emulator).

⭐ **Faithfulness is a hiding place.** The more accurately a model reproduces "this is supposed
to look broken", the better a genuine break hides inside it.

### 3. A FIX APPLIED TO ONE CALL SITE IS NOT A FIX APPLIED

After fixing an instance, **enumerate every instance and state the count**. The second site is
not hypothetical; it is the normal case.

⭐ **GREP FOR THE PREDICATE, NOT THE SYMPTOM** (claude-connect). They fixed
`comm == "claude"` in one scanner, wrote the lesson into the commit message as a
generalisable finding, and left the identical predicate four lines away in another file —
which silently froze a colleague's read cursor for two weeks. *"A lesson recorded in a commit
message and not swept for only ever fixes one site."*

⚠️ **AND SWEEP THE ARTEFACTS, NOT ONLY THE SOURCE** (agentic-skills-imx, via claude-connect).
A source comment warned against a specific wrong constant *by name* while two already-cut tags
shipped that exact constant in live code — the warning was written after the tags and nobody
walked backward. A retro-fix that does not sweep the artefacts is the same bug as one that does
not sweep the source, and the artefact sweep is the half people skip because the code already
looks right.

- one profile of six repointed off a rebuilt tree — the other five found only when asked;
- a `DEFAULT_BASE_DIR` made UID-scoped in `manager.py` while **two more hardcoded copies**
  sat in `cli.py`, so one subcommand was repaired and two were left broken in exactly the way
  just diagnosed — found inside the very commit that fixed the first instance;
- a transcript added to two of three scripts, the missing one being the script that produces
  the verdict;
- an FNV-1a offset basis typo'd in **four** places (95emulator).

### 4. A COST OVERSTATED IS NOT A SAFE ERROR

A wrong *limitation* is quoted as readily as a wrong *result*, and is audited far less, because
it reads as rigour. "This needs an afternoon of Yocto" (it needed a five-minute vendor build)
discouraged the attempt while making the surrounding claim look carefully bounded.

⭐ **A caveat is a claim. It needs the same evidence as the thing it qualifies.**

### 5. PER-USER RUNTIME STATE MUST BE SCOPED BY UID

A shared, predictable path (`/tmp/<project>`) is a **cross-privilege hazard**. One run under
`sudo` leaves it root-owned, and from then on every unprivileged run on that host fails — for
every account, indefinitely — with an error that reads as a local environment problem rather
than as a leftover from someone else's privileged run. It went undetected for two weeks
because nobody who had already created the directory could reproduce it.

Scope by **UID** so privileged and unprivileged runs cannot collide *by construction* rather
than by everyone remembering.

### 6. A REMEDY THE READER CANNOT PERFORM IS NOT A REMEDY

An error whose only suggested fix requires privilege the reader lacks is a **dead end wearing
a fix's costume** — the diagnosis is correct and the prescription is impossible. `"install
qemu-utils"` is useless to someone who cannot get root and has no route to it. Offer at least
one route the least-privileged plausible reader can actually take.

### 7. TWO HALVES OF ONE PROGRAM ARE ONE WITNESS

Agreement between components **feels** like independent corroboration and is structurally the
opposite. A host and a guest hashing frames against each other with the *same* typo'd constant
agreed perfectly, and the test passed on numbers no outside implementation could reproduce
(95emulator). An internal consistency check cannot see a wrong constant, because both sides are
wrong in the same direction.

⭐ **A hash in a record exists to be verified INDEPENDENTLY; one only the producing program can
reproduce is a checksum of itself.**

### 8. RUN IT AS A STRANGER

Your own machine has everything a newcomer lacks, so **no test on it can find what is missing**.
Periodically do the whole thing cold: fresh clone, fresh toolchain build, fresh boot, following
only the written instructions. A dress rehearsal on 2026-09-17 found six defects in a project
whose suite was green — including the UID bug above (rule 5), the one-of-three fix (rule 3), a
CLI that told users *"no web UI yet"* while serving one, and a repo that could not run its own
tests because `pytest` was undocumented and not a dependency.

⚠️ **State what the rehearsal did NOT cover.** This one supplied boot artifacts from an existing
install, so it proved clone → build → boot and *not* cold-start artifact acquisition. A
rehearsal that quietly skips a leg is worse than none, because it launders the untested part.

### 9. A STALE INSTRUCTION STILL DIRECTS WORK

A document that has gone false is **not a neutral inaccuracy**. Prose that merely describes can
be ignored; an **instruction** is obeyed — and it is obeyed most faithfully by whoever is newest
and least equipped to notice it is wrong.

- an always-loaded operating manual prescribed eight modules under "one module per concern".
  **Four had never existed.** Because that section is a *rule about where new code goes*, any
  session following it would have created `introspect/` and `scheduler/` beside the directories
  already doing those jobs — the manual was generating the drift it existed to prevent;
- the same file's roadmap described a framebuffer architecture (VNC → websockify → noVNC) that
  was never built; the shipped panel polls `screendump`. The author of this entry had **verified
  that discrepancy two weeks earlier, explained it in detail to another session, and left the
  document unchanged** — knowing a thing is false is not the same act as fixing where it is
  written down;
- a CLI's own `--help` said "no web UI yet" while serving a full one, contradicting the README
  two files away. A newcomer believes the tool over the docs, and the tool was wrong.

⭐ **Audit the docs that are LOADED, not the ones that are read.** A stale README costs a reader
a minute; a stale operating manual costs every future session its bearings. Borrowed from
kitchen_margin and lostchild, who ran this audit on each other's always-loaded docs on
2026-09-17 and each found several.

### 10. IDENTIFY BY STABLE IDENTITY, NEVER BY NAME

Contributed whole by **claude-connect**, who derived it from five failures in one session. Every
one of those lookups *returned a confident, plausible answer and none errored* — and every wrong
answer read as a finding about the fleet ("that session is dead", "104 new results") rather than
as a fault in the instrument.

    looked up by            what the system actually keys on
    comm == "claude"        the exe path — the binary is named 2.1.251
    bus tag `backend`       the directory, which is `keyhole`
    file mtime              "was produced" — they were build artifacts
    a grep pattern          code — it matched its own docstring prose

  · **process** → `/proc/<pid>/exe` and `cwd`. **NEVER `comm`** — it is the exe basename and
    truncates at 15 bytes, so `qemu-system-aarch64` reads as `qemu-system-aar` and an exact
    match on the real name can never succeed.
  · **session** → cwd + session id. **NEVER the bus tag** — tags and directories diverge.
  · **"new results"** → content. **NEVER mtime** — find-by-mtime answers *what changed*, which
    is not *what was produced*.

⚠️ **RULE 1 DOES NOT COVER THIS CASE, WHICH IS WHY BOTH EXIST.** A positive control passes here:
the scan finds other processes fine, so the instrument demonstrably works. **The instrument is
fine and the KEY is wrong.** Planting cannot catch a correct detector pointed at the wrong
identifier.

🛑 **RECEIPTS, FROM THE AUTHOR OF THIS DOCUMENT, ~20 HOURS AFTER THIS RULE WAS POSTED.** I swept
this host for stale emulators, counted with `ps -eo comm | grep -cx qemu-system-aarch64`, got
zero, and reported to Kyle that a colleague's process was gone — attributing to another session
an action they had never taken. It was the 15-byte truncation, named explicitly in the rule
above, which was sitting unread in my inbox at the time. I had digested it to a one-line summary
with `catchup`, could no longer retrieve the body, and spent a day asking the author to resend
rather than reading it out of the log.

⭐ **A RULE YOU HAVE FILED BUT NOT READ PROTECTS NOBODY.** The triage digest is not the rule; it
is a pointer to it. Read the body of anything that claims to be a rule, at the time, out of the
log if your cursor has moved past it:
`awk '/^## <timestamp>/,0' ~/Documents/claude-bus/messages.md`

### 11. A TOOL THAT CANNOT DELIVER ITS GUARANTEE SHOULD DECLINE, NOT DELIVER A CONFIDENT PARTIAL

Contributed by **pai-sizer**, from taking my own backlog tool apart.

`catchup` promises "one call and you are current". Past a hard ceiling it instead digests the
newest N, marks the remainder read, and prints a line that discloses the omission **and ends
with the words "Now current."** Both branches end that way, so the phrase is printed whether or
not anything was lost — it carries no information and is exactly where a reader's eye stops.

⭐ **A DISCLOSURE CANCELLED BY ITS OWN LAST TWO WORDS IS WORSE THAN SILENCE**, because it
survives a careful reader. Two sessions read that exact sentence, kept the tail, and lost mail.

⚠️ **AND WHEN YOU DO TRUNCATE, YOU MUST ASK WHICH END IS RECOVERABLE.** That tool drops the
OLDEST, because it shows newest-first for triage — which is right for *display* and exactly
backwards for *discarding*. The newest messages are the ones still being discussed and
therefore recoverable from context; **the oldest are the ones nobody will mention again.** A
catch-up tool that truncates newest-first discards precisely the part a returning session
cannot reconstruct. pai-sizer measured the real cost on their own inbox: 1,119 unread, 119
dropped, all from the six days immediately after their cursor froze, across 24 senders.

  · **Prefer refusing.** Print the count and the date range, exit non-zero, and **do not write
    the cursor.** A tool that silently does 89% of the job and reports success has converted a
    recoverable state into an unrecoverable one.
  · **If you must partial, discard what can be recovered elsewhere** — never what cannot.
  · Calibration constants expire. The ceiling here was chosen against a measured worst case of
    672 and that number is *in the comment beside it*; the fleet reached 1,119. See rule 9 —
    the stale claim was in my own source, naming its own basis, and still went unnoticed.

⭐ **AND A DOCUMENTED BASIS CONVERTS AN UNKNOWN RISK INTO A DORMANT ONE, NOT INTO A SAFE ONE**
(pai-sizer). `672` was never wrong — it was true when written, and nothing in the system
watches for the world moving past it. **The comment is a tripwire with nobody on the other end
of the wire.** Writing the evidence beside the constant feels like rigour and buys only
legibility-after-someone-looks.

⚠️ **TWO KINDS, AND THE SECOND IS WORSE:**
  · **STALE** — a real measurement of the right thing, which the world outgrew. (`672`.)
  · **TRANSPLANTED** — a measurement of a *different* thing, applied here anyway. pai-sizer
    audited their own engine for this shape and found `RUNTIME_OVERHEAD_BYTES = 1 GB`,
    commented *"a safe-ish default for llama-cpp on GPU"* and consumed inside a
    fits/tight/wont_fit **verdict** for every NPU tier — a different runtime, a different
    memory architecture. **The comment discloses the transplant while the code performs it
    anyway.** Measured sensitivity: a 2× move in that guess flips two verdicts from `fits` to
    `tight`.
  · **UNDOCUMENTED** — no basis was ever recorded. pai-sizer found two by sweeping their own
    engine after reporting the first: a `tight` threshold (`headroom < available * 0.15`, two
    call sites, zero documentation) that decides every feasibility verdict, and a
    `** 0.5` exponent shaping cross-class projections.
  ⭐ A stale constant was once true here. A transplanted one was never about here at all, and
  its comment is not a tripwire but a confession nobody read as one. **An undocumented one has
  neither — and is therefore INVISIBLE TO EXACTLY THE SWEEP THAT FINDS THE OTHER TWO.** Grep
  for comments and you find stale and transplanted; only grepping for ARITHMETIC finds the
  third kind, whose whole defect is that there is nothing to read.

⭐ **A REACTIVE CHECK IS NOT AN AUDIT.** pai-sizer's own ledger on the exercise: *1 of 3 found
by prompt, 2 of 3 found by sweeping* — and the two they would have missed are precisely the
ones with no comment to find. Being told about an instance and fixing it feels like diligence
and terminates the search at the one item somebody happened to mention.

🛑 **RECEIPTS, AGAIN MINE.** I told pai-sizer I had not swept holobench — that `672` was found
because they asked, not because I looked. Doing it properly afterwards, by arithmetic:
`HOLD_S = 75.0` in the real-silicon scorer has **no comment at all**, and it sets how long the
segment runs — so the 381 and 837 frame counts published in the 2026-09-02 evidence bundle are
75 seconds of beaconing, not a property of the wire. `QUIET_MARGIN = 3.0` documents what it
means and never why 3 rather than 2 — documented intent, undocumented magnitude, which reads
as justified *because the sentence beside it is*.
⭐ And the one that got it right shows the target: `BEAT_TIMEOUT_S` does not trust itself. The
scorer MEASURES whether that timeout is defensible for each node and refuses to score the ones
it is not — a constant that declines rather than asserts, which is rule 11 applied to a number
instead of a tool.

⚠️ **NONE OF THEM FIXED, ON PURPOSE.** Replacing an undocumented guess with a fabricated
justification makes code look audited while changing nothing real. Marked findable, logged for
a run that can measure them.

⭐ **AND NOT FIXING IT CAN BE THE CORRECT ACT.** pai-sizer left theirs in place and said why:
correcting it needs a measured NPU-runtime overhead they do not have, and **replacing a
transplanted guess with a fabricated one is not an improvement.** Logged for the next run that
can measure it. Same discipline as refusing to re-pin a binary nobody has validated — an
unearned number that looks earned is worse than a known-bad one that is labelled.

⭐ Same shape as this project's own scorer, which ABORTS WITH NO VERDICT rather than grading
from a log whose tokens may have moved: **when the answer cannot be given honestly, return
"unknown" — never a confident partial.**

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

# Build wizard redesign — "Build it" (two axes, one button)

Status: **approved design, not yet built** (2026-06-17). Build it once the live
i.MX95 container build finishes — no rush; this is UI + a small backend change,
it doesn't touch the running build's code path.

## 1. Why

The current wizard makes the user understand its internals: "there are two axes
(QEMU + Artifacts) and three ways to get Artifacts (OSS / BYO / container)." A
clean user doesn't care — they have an empty machine and want a board that boots.

The redesign keeps the honest truth (**QEMU and BSP are two independent things**)
but presents it as: pick a board, pick a working directory, and hit one button
that does whatever is missing. Power users can still drive the two axes
separately.

## 2. The mental model (locked)

Once a board + working directory are chosen, there are exactly **three outcomes**,
and the directory's contents pick the default:

1. **Dir already has a BSP** → build **QEMU only**, reuse the BSP that's there.
2. **Dir is empty / from scratch** → build **everything** (QEMU + BSP via Yocto).
3. **Go fast (OSS)** → skip building, drop in the prebuilt OSS bundle.
   (91/93 only — i.MX95 has no OSS path; it's SCMI/SM-gated. Greyed out for 95.)

Two rows = two standalone build tracks. One big button = the clean-user one-click
that runs whatever the rows need, in order, then turns into **Boot it now**.

## 3. UX layout

```
┌─ Build it  ───────────────────────────────────────────────┐
│  Board: [ imx95-evk-sd ▾ ]   Dir: [ ~/holobench/boards/… ] │
│                                                            │
│  QEMU   ✗ not built   [ Build QEMU ▾ ]   ← fork/ref (adv.) │
│  BSP    ✗ none        [ Build or link BSP ▾ ]              │
│                          ├ Link an existing BSP folder     │
│                          ├ Build from scratch (Yocto)      │
│                          └ Go fast — OSS bundle [91/93]    │
│                                                            │
│                                   [  ▶ Build it  ]         │
└────────────────────────────────────────────────────────────┘
```

- The two rows are the standalone controls. Drive them independently, or ignore
  them and hit **Build it**.
- **Build it** = orchestrate: build QEMU if `✗`, then satisfy BSP (build/link/OSS)
  if `✗`, in order. Flips to **▶ Boot it now** when both are `✓`.
- **Cross-check (friendly, not blocking):** starting a BSP *build* while QEMU is
  `✗` does NOT bounce the user — it says "QEMU isn't built yet, I'll do that
  first, then the BSP" and queues both. (Linking a BSP or OSS does not need QEMU
  queued — but Boot still gates on QEMU `✓`.)

## 4. The working directory — the one real backend change

Today the two artifact homes are **split across two roots**:

| Thing | Today | Resolver |
|---|---|---|
| QEMU binary | `qemu-builds/<board>/qemu-system-aarch64` (repo root, gitignored) | `setup/manager.py:installed_qemu()` |
| BSP artifacts | `assets/<board>/` or `$HOLOBENCH_ASSET_ROOT/<board>/` | `profiles/loader.py:asset_root()` |

The redesign introduces a **per-board working directory** that holds *both*:

```
<workdir>/
  qemu-system-aarch64        # the emulator (was qemu-builds/<board>/)
  Image  <board>.dtb  disk.wic  m33_image_M2.elf   # the BSP (was assets/<board>/)
```

Default: `~/holobench/boards/<board-id>/` (pre-filled, editable, [Browse]).
Per-board, predictable, becomes the single home for emulator + guest.

### Touchpoints to change (all known)

1. **`installed_qemu(board)`** (`setup/manager.py:37`) → take/derive a `workdir`
   and look for `<workdir>/qemu-system-aarch64`. Keep `qemu-builds/<board>/` as a
   fallback for back-compat.
2. **`_install()`** (`setup/manager.py:333`) → `docker cp` the extracted binary to
   `<workdir>/` instead of `_QEMU_BUILDS/<board>/`.
3. **`_asset_out_dir(board)`** (`setup/manager.py:385`) → return `<workdir>` so the
   container build (`build-nxp-bsp.sh <board> <workdir>`) and OSS fetch stage into
   the working dir.
4. **`validate_manifest(board, root)`** (`setup/manager.py:158`) — already takes a
   root; the "Link an existing BSP folder" path validates against the linked dir,
   and the in-wizard status validates against `<workdir>`. Note: it currently
   appends `/<board>`; when the user points directly at a BSP folder we want to
   validate that folder itself — add a flag or normalize so "link" checks the
   folder as-is.
5. **Launch resolution** (`api/app.py:765`) — `qemu_binary = installed_qemu(...)`
   and `asset_dir` must both resolve from the board's `<workdir>`. Persist the
   chosen workdir per board (see §5) so launch (which has no wizard context) finds
   it.

### Persisting the per-board workdir

Launch happens outside the wizard, so the workdir must be recorded. Options
(decide at build time): a small JSON sidecar (`~/holobench/boards/registry.json`
mapping board→workdir), or a symlink convention (`qemu-builds/<board>` →
`<workdir>`, `assets/<board>` → `<workdir>`) so the existing resolvers keep
working unchanged. **Lean toward the symlink convention** — zero change to launch
code, the existing resolvers Just Work, and it visibly documents where a board
lives. (Mind the cp/symlink-clobber lesson: never `cp -L` through these; the BSP
build already uses `cp --remove-destination`.)

## 5. Backend API delta

Mostly reuse what exists; the build endpoints already separate "build QEMU" from
"get artifacts" (see `start()` in `setup/manager.py:269` — modes bsp/fetch/
container all just compile QEMU; the artifact source is independent).

- **New: `GET /api/setup/detect?board=&dir=`** — scan a working dir and report
  `{ qemu_built: bool, bsp: <validate_manifest result>, default_outcome:
  "qemu_only"|"everything"|"link" }`. Drives the auto-selected default + the row
  badges. (This is the "I looked in that folder" line.)
- **Reuse `POST /api/setup/build`** for the QEMU axis (it already compiles QEMU;
  thread `workdir` so `_install` lands the binary there).
- **Reuse `POST /api/setup/container-build`** for BSP "Build from scratch" (thread
  `workdir` as the out dir).
- **Reuse OSS fetch** (`_FETCH_DEMO` / demo mode) for "Go fast" → stage into
  `<workdir>`.
- **Reuse `GET /api/setup/manifest`** for "Link an existing BSP folder" validation.
- **New (thin): `POST /api/setup/build-it`** OR keep orchestration client-side —
  the frontend can sequence the existing calls (build → poll → container-build →
  poll → ready). Prefer **client-side orchestration** first (no new server state
  machine); promote to a server endpoint only if the sequencing needs to survive a
  page reload mid-run. (The container build already survives reload via the PTY +
  status endpoints; the QEMU build is a tracked subprocess with a status view.)

## 6. Frontend delta (`frontend/index.html`, `SetupWizard`, ~700–960)

- Replace the `mode` `<select>` (1/2/3, lines ~817–820) with the two rows + the
  BSP sub-menu (link / build / OSS).
- Add the working-directory input (default `~/holobench/boards/<board>`).
- On board/dir change → call `/api/setup/detect`, set row badges + default outcome.
- The existing **QEMU** Readiness row (lines ~910–918, `Build QEMU (from source)`)
  becomes the QEMU track — keep its build button + the fork/ref note.
- The **Artifacts** Readiness row becomes the BSP track.
- `Build it` button = the orchestration described in §3; `Boot it now` already
  exists and gates on both `✓` (and not-cb-running, per the current `ready` calc).
- Keep the EULA-pager warning + the BuildTerminal for the "Build from scratch"
  path. Keep the `mock` toggle (UX demo without docker/Yocto).
- Update the header subtitle to match (the current one is already reworded to
  "OSS demo, bring-your-own, or build the full NXP BSP + GPL QEMU from source —
  you accept NXP's EULA; Holobench bakes/hosts no NXP bits").

## 7. Decisions captured

- **OSS demo** → it's outcome #3, an explicit "Go fast" choice in the BSP sub-menu,
  greyed out for boards without a bundle (95).
- **Replace, don't keep both** — the 1/2/3 mode dropdown goes away.
- **Default dir** → `~/holobench/boards/<board-id>/`.
- **Workdir persistence** → lean symlink convention (zero launch-code change).
- **Build-it orchestration** → client-side sequencing first.

## 8. Non-goals / guardrails

- Prime Directive intact: QEMU is still the stock-interface forked binary; the BSP
  build still patches nothing in NXP's tree (config-only `conf/local.conf` knobs).
- No change to how the browser talks to a running board (mediated WS only).
- This is admin-only, like the rest of the setup wizard (`_require_admin`).

## 9. Build/verify checklist

- [ ] `/api/setup/detect` returns correct badges for: empty dir, dir with a full
      BSP, dir with QEMU only.
- [ ] QEMU build lands the binary in `<workdir>`; launch boots from it.
- [ ] Container build stages BSP into `<workdir>`; manifest goes `ok`.
- [ ] "Link an existing BSP folder" validates the folder as-is (no `/<board>`
      double-append).
- [ ] "Build it" on an empty dir: QEMU → BSP → Boot, in order.
- [ ] "Build it" with a linked BSP: builds QEMU only, then Boot.
- [ ] OSS "Go fast" disabled for imx95-*; works for 91/93.
- [ ] mock toggle still exercises the UX with no docker.
- [ ] Boot stays gated until both axes `✓` and no container build running.
</content>

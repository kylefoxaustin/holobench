# CLAUDE.md — Holobench

Operating manual for Claude Code sessions working in this repo. Read it fully
before writing code. If a decision here conflicts with something you're about
to do, this file wins; if this file is silent, prefer the smallest change that
keeps the Prime Directive intact.

---

## 1. Mission

Build a standalone web front end that lets a user reserve a **virtual board**,
then console into it, see its framebuffer, power-cycle it, and push boot files
onto it — backed by a QEMU machine model instead of physical silicon. The
target UX is a hardware board farm (NXP aiotcloud / WEVK Remote Console), but
the boards are emulated.

The first three boards are NXP i.MX 95, 93, and 91. They are **profiles**, not
special cases. The design must stay board-agnostic.

## 2. The Prime Directive (do not violate)

> **Holobench drives QEMU only through standard, upstreamable interfaces.**

Allowed control surface:
- **QMP** — standard commands only (`query-status`, `system_reset`, `stop`,
  `cont`, `quit`, `screendump`, `query-block`, `qom-list`, `qom-get`,
  `human-monitor-command` for read-only `info` queries, etc.).
- **Serial** — standard `-serial`/`-chardev` backends (pty, unix socket, tcp).
- **Display** — standard `-vnc` / `-display vnc`.
- **Storage / files** — standard `-drive`, `-sd`, virtio-9p (`-fsdev`/`-virtfs`),
  user-net TFTP (`-netdev user,tftp=`), and host-side NFS export.
- **Debug** — standard `-gdb`/`-s` gdbstub.

**Forbidden, no exceptions:**
- ❌ Custom QMP commands or a custom QAPI schema.
- ❌ Any patch, shim, or device added to a machine model "for Holobench."
- ❌ Depending on a forked/patched QEMU binary.
- ❌ Parsing machine-model internals or private files instead of QMP/`info`.

**Why:** the i.MX 95/93/91 models are being upstreamed to qemu.org. Upstream
accepts hardware models, not front ends, and reviewers will reject anything that
smells like front-end coupling. Coupling also chains Holobench to a non-stock
binary, killing portability to any other QEMU machine. Keeping the boundary at
"stock QEMU interfaces" is what lets the models stay clean *and* lets Holobench
work against any present or future machine.

**Escalation path** when a board genuinely lacks something Holobench needs
(e.g., no framebuffer device, a UART that isn't wired to a chardev): do **not**
work around it in this repo. Raise it with the emulator repo (see §7). The fix
belongs in the model, exposed through a standard interface, and ideally
upstreamable. Until then, the profile flags the capability as absent and the UI
degrades gracefully (e.g., hide the LCD panel).

## 3. Tech stack (decided — don't re-litigate without reason)

- **Backend:** Python 3.11+, **FastAPI** (async, native WebSocket). QMP via the
  official **`qemu.qmp`** package (`pip install qemu.qmp`). Process supervision
  via `asyncio` subprocesses.
- **Framebuffer:** QEMU built-in VNC → **websockify** (embeddable) → **noVNC**.
- **Frontend:** **React** + **Tailwind**, **xterm.js** for the console,
  **@novnc/novnc** for the LCD panel.
- **Profiles:** YAML, validated with **pydantic** models on load.
- **Isolation:** v1 = plain subprocess + per-session overlay image + per-session
  port range. Design the session abstraction so a later container/namespace
  backend drops in without touching the API.

Rationale: Kyle is Python-first; `qemu.qmp` is the canonical, maintained QMP
client; noVNC/xterm.js are the standard browser-side pieces and map 1:1 to the
two panels in the board-farm UX. Don't introduce a second language in the
backend without a strong reason.

## 4. Build order (phased — ship each phase working before the next)

- **Phase 0 — Bring-up.** Profile loader (pydantic) → launch one QEMU from
  `profiles/imx95-evk.yaml` → attach QMP → prove `query-status` and
  `system_reset`. No UI yet; a CLI or a couple of REST endpoints is fine.
  *Done when:* `holobench launch imx95-evk` boots and you can reset it via QMP.
- **Phase 1 — Console.** Serial chardev → WebSocket → xterm.js. One board, one
  session. *Done when:* you can type at u-boot/Linux in the browser.
- **Phase 2 — Framebuffer.** QEMU VNC → websockify → noVNC LCD panel.
  *Done when:* the board's display renders live in the browser.
- **Phase 3 — File injection.** virtio-9p first (drop a file, see it in guest),
  then user-net TFTP (so `tftpboot` works like the farm), then NFS export, then
  image swap / "reinstall." Mirror the farm's NFS-to-`/mnt` and TFTP-to-server
  semantics (see the Upload File dialog screenshots).
- **Phase 4 — Fleet.** Multi-session + isolation; fill `imx93-evk` and
  `imx91-evk` profiles; add a simple reservation/scheduler with time limits
  (the farm has a "Remaining Time" countdown — match it).
- **Phase 5 — Beat the hardware.** Introspection panel in the otherwise-dead
  "video" real estate: live memory map (`info mtree`), device tree (`info qtree`
  / `qom-list`), QMP event stream, gdbstub attach, snapshots. These are things a
  physical board farm structurally cannot show.
- **Phase 6 — Harden.** Auth, upload sanitization, deployment packaging so NXP
  could host it as a "virtual EVK" service.

## 5. Coding conventions

- Type hints everywhere on the backend; pydantic for all external data
  (profiles, API bodies).
- The orchestrator never shells out to `qmp-shell` or scrapes stdout for control
  — always use the `qemu.qmp` client. (Reading the *serial console* stream is
  fine; that's the user's terminal, not a control channel.)
- One module per concern: `profiles/`, `session/`, `bridges/console.py`,
  `bridges/display.py`, `bridges/files.py`, `introspect/`, `scheduler/`, `api/`.
- Keep board-specific knowledge out of code. If you're writing `if soc ==
  "imx95"`, stop — that belongs in the profile.
- Commit per phase milestone with a clear message; keep PRs reviewable.

## 6. Security guardrails

- **Never expose raw QMP or the serial control socket to the browser.** The
  backend is the only thing that talks QMP. The browser gets a mediated
  WebSocket (terminal bytes, framebuffer RFB, scoped control verbs) — nothing
  that can issue arbitrary monitor commands.
- Treat uploaded files as hostile: size limits, path confinement to the
  session's TFTP/NFS/9p dir, no traversal.
- Per-session isolation: separate process, separate overlay image, separate
  port range; design for separate namespace/container later.
- Auth is required before any shared/multi-user deployment (Phase 6), but build
  the API auth-aware from the start (a no-op auth dependency you can swap).

## 7. Coordinating with the emulator repos and other Claude sessions

The emulator repos — `qemu-imx95`, `qemu-imx93`, `qemu-imx91` — are owned by
**separate Claude Code sessions** and are the **source of truth** for each
board. Holobench is a *consumer*. The integration surface is the board profile
plus the Prime Directive's standard interfaces — **never source-level coupling**.

When onboarding a board, get these facts from its emulator repo (its README /
the emulator Claude) and encode them in a profile — do not guess:
- The exact `-M` machine type string.
- The serial topology: how many UARTs, which chardev IDs, which is the
  A-core console vs. M-core vs. debug.
- Whether a framebuffer/display device exists and what `-display vnc` shows.
- The canonical boot artifacts and command line (flash.bin / u-boot, kernel
  Image, dtb, rootfs) and the boot mode.
- Which standard QMP commands are known-good on that model.

If a board is missing something Holobench wants and the gap is in the *model's
own correctness* (e.g., no display device, a UART not wired to a chardev), file
it against the emulator repo (Prime Directive §2 escalation). That is model
work, exposed through standard interfaces — not a Holobench-specific addition.

**Profiles are centralized in this repo. Locked decision — do not reopen.**
Holobench **never** adds files to the emulator repos: no `board.yaml`, no
Holobench config, no profile, nothing. The emulator Claude *reports* facts
(machine type, UARTs, display, boot line, QMP) in chat or via whatever its own
README already documents for users; those facts are transcribed into
`profiles/<id>.yaml` here. The integration is read-only and one-directional:
the qemu repos stay pristine.

If you need to hand context between sessions, this repo's `profiles/*.yaml` and
this `CLAUDE.md` are the durable contract — write decisions there, not in chat.

## 8. What NOT to do (quick reference)

- Don't patch a machine model. Don't add a device. Don't add a QMP command.
- Don't depend on a forked QEMU.
- Don't put board logic in code (`if soc == ...`) — put it in a profile.
- Don't expose QMP/serial control sockets to the browser.
- Don't build a generic VM manager — the unit is a *board*.
- Don't skip phases; each must work end-to-end before the next.
- Don't invent machine-type strings or boot artifacts — confirm with the
  emulator repo.

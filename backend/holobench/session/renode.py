# SPDX-License-Identifier: GPL-2.0-or-later
"""Renode's Monitor, spoken over its own standard TCP interface.

⭐ THE PRIME DIRECTIVE APPLIES TO RENODE TOO (CLAUDE.md §2). Everything here goes
through interfaces Renode ships for external callers and documents in `--help`:

    --port <n>                  the Monitor on a TCP socket   (the QMP analogue)
    --pid-file <path>           lifecycle
    CreateServerSocketTerminal  the guest UART on a TCP socket
    .repl / .resc               the machine and its launch script

No patched Renode, no plugin written "for Holobench", no reaching into rt1180renode's
tree for anything but the platform files it already publishes to its own users. If a
board needs something Renode cannot express, that is an escalation to the emulator repo
— not a workaround here.

═══════════════════════════════════════════════════════════════════════════════
THE PROTOCOL, AS MEASURED against Renode 1.17.0 on 2026-09-21 — not as documented,
because it is not documented. Every claim below was read off a live socket.

  1. TELNET NEGOTIATION ON CONNECT. The first bytes are IAC sequences:
         ff fd 00   ff fd 1f   ff fb 01   ff fb 03   ff fc 22
     (DO BINARY, DO NAWS, WILL ECHO, WILL SGA, WONT LINEMODE). We never answer them —
     the spike proved an unanswered negotiation still yields a fully working session —
     but we MUST strip them, because 0xFF bytes inside decoded text corrupt it.
     ⚠️ The banner itself arrives LOSSY: the measured stream reads `...ff fc 22` then
     "enode, version 1.17.0" — the R of "Renode" is simply not on the wire. That is why
     nothing here parses the banner. We wait for a PROMPT, which is reliable.

  2. THE PROMPT IS THE COMPLETION MARKER, and it names the selected machine:
         "(rt1180) "     a machine is selected
         "(monitor) "    NO machine is selected
     There is no length prefix, no status code, no terminator — the prompt is all you get.

  3. COMMANDS ARE ECHOED. Sending "machine IsPaused\n" yields
         b'machine IsPaused\n\rFalse\r\r\n(rt1180) '
     so the first line of every response is the command itself and must be dropped.

  4. LINE ENDINGS ARE INCONSISTENT — "\n\r", "\r\r\n" and "\r\n\r" all appear in one
     response. We normalise by deleting every \r rather than trying to honour them.

  5. ERRORS ARE PROSE ON A SUCCESS-SHAPED RESPONSE. A bad command returns
         "No such command or device: nosuchcommand_xyz"
     and then the ordinary prompt. There is no error channel, so the only way to fail
     loudly is to match the text.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Optional


class RenodeError(Exception):
    """The Monitor refused a command, or the transport broke."""


# The prompt, anchored to end-of-buffer. Renode machine names are the identifiers a
# `mach create "<name>"` accepts; `monitor` is the no-machine-selected form.
#
# ⚠️ ANCHORED AND CHARACTER-CLASSED ON PURPOSE. A looser "ends with )" test — which an
# early spike of this file used — fires on any output whose last line happens to close a
# paren, and `peripherals` prints "(SystemBus)" on nearly every line. That truncates a
# response mid-stream and hands the caller a plausible, short, WRONG answer.
_PROMPT = re.compile(r"\(([A-Za-z0-9_.\-]+)\) $")

# Renode's own failure prose. Matched at the START of a line: a command whose legitimate
# output quotes one of these phrases mid-sentence is not a failure.
_ERRORS = (
    "No such command or device:",
    "Bad parameters for command",
    "There was an error executing command",
    "Could not find",
)


def strip_iac(buf: bytes) -> bytes:
    """Remove telnet IAC sequences, leaving the payload.

    IAC(0xFF) + WILL/WONT/DO/DONT(0xFB-0xFE) + option  -> 3 bytes
    IAC + SB(0xFA) ... IAC + SE(0xF0)                   -> variable
    IAC + IAC                                           -> a literal 0xFF
    IAC + anything else                                 -> 2 bytes
    """
    out = bytearray()
    i, n = 0, len(buf)
    while i < n:
        b = buf[i]
        if b != 0xFF:
            out.append(b)
            i += 1
            continue
        if i + 1 >= n:
            break                       # dangling IAC at a read boundary; drop it
        c = buf[i + 1]
        if c == 0xFF:                   # escaped literal 0xFF
            out.append(0xFF)
            i += 2
        elif 0xFB <= c <= 0xFE:         # WILL / WONT / DO / DONT + option byte
            i += 3
        elif c == 0xFA:                 # subnegotiation, runs to IAC SE
            j = buf.find(b"\xff\xf0", i + 2)
            i = n if j < 0 else j + 2
        else:
            i += 2
    return bytes(out)


@dataclass
class RenodeCaps:
    """What this control plane can actually do, so callers never guess.

    ⭐ A CAPABILITY IS A MEASUREMENT, NOT AN ASPIRATION. Every flag here was set by
    running the verb against a live i.MX RT1180 and reading the answer. Where Renode
    genuinely has no equivalent the flag is False and the UI hides the control — the
    same graceful degradation CLAUDE.md §2 requires when a board has no framebuffer.
    """

    status: bool = True          # machine IsPaused
    reset: bool = True           # machine Reset
    pause: bool = True           # pause
    resume: bool = True          # start
    quit: bool = True            # quit
    device_tree: bool = True     # peripherals   (the qom-list analogue)
    uptime: bool = True          # currentTime   (QEMU has no equivalent at all)
    snapshot_save: bool = True   # Save @path    (measured: a real 1.6 MB file)
    # ⚠️ FALSE, AND NOT BECAUSE RENODE LACKS `Load`. It has one, it runs, and it
    # restores. Disabled on TWO measured findings, the second of which is decisive:
    #
    #   1. `Load` DESELECTS THE MACHINE. The prompt drops "(rt1180)" -> "(monitor)" and
    #      the next `machine IsPaused` fails with "No such command or device: machine".
    #      ⭐ THIS ONE IS FIXABLE: `mach set "<name>"` restores the selection, and status
    #      queries work again afterwards. Measured, not assumed.
    #
    #   2. THE GUEST CONSOLE DIES. Measured 2026-09-21: the CreateServerSocketTerminal
    #      port ACCEPTS connections before `Load` and returns ConnectionRefusedError
    #      after it. The restored emulation has no socket terminal, because the terminal
    #      was created by the .resc against the emulation the snapshot replaced.
    #      ⭐ THIS ONE IS NOT FIXABLE FROM HERE. Recreating the terminal would hand the
    #      board a NEW port while every attached xterm.js client still holds the old one
    #      — the user watches their console go silent and nothing says why. On a board
    #      farm, a restore that silently severs the console is a worse failure than an
    #      absent button, because the board looks alive and answers status.
    #
    # The escalation, if this is ever wanted: it is Renode-side (a terminal that survives
    # Load, or a documented recreate hook), which is an emulator-repo conversation under
    # CLAUDE.md §2 — not something to paper over in Holobench.
    snapshot_load: bool = False
    # QEMU-only, and there is no Renode analogue of these. Declared so the API can say
    # "this backend cannot" instead of raising something shaped like a bug.
    screendump: bool = False
    hmp: bool = False
    qmp_events: bool = False


class RenodeMonitor:
    """An async client for the Renode Monitor socket.

    Deliberately shaped like the slice of `qemu.qmp.QMPClient` that Session uses, so the
    session layer can hold either one without knowing which it has.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 0,
                 machine_name: str = "board") -> None:
        self.host = host
        self.port = port
        self.machine_name = machine_name
        self.caps = RenodeCaps()
        self._r: Optional[asyncio.StreamReader] = None
        self._w: Optional[asyncio.StreamWriter] = None
        self._lock = asyncio.Lock()
        self._prompt: Optional[str] = None   # machine named by the last prompt seen

    # ── transport ──────────────────────────────────────────────────────────────
    async def connect(self, timeout: float = 20.0) -> None:
        """Connect and wait for the first prompt.

        ⚠️ RETRIES, BECAUSE THE PORT OPENS BEFORE THE SCRIPT FINISHES. Renode binds the
        Monitor port early and only then runs the .resc, so a connect can succeed while
        `mach create` has not happened yet. Waiting for a PROMPT (not for the port) is
        what makes "connected" mean "ready for a command".
        """
        deadline = asyncio.get_running_loop().time() + timeout
        last: Optional[Exception] = None
        while asyncio.get_running_loop().time() < deadline:
            try:
                self._r, self._w = await asyncio.open_connection(self.host, self.port)
                break
            except OSError as exc:
                last = exc
                await asyncio.sleep(0.25)
        else:
            raise RenodeError(
                f"Renode Monitor at {self.host}:{self.port} never accepted a connection "
                f"within {timeout}s ({last})")
        remaining = max(1.0, deadline - asyncio.get_running_loop().time())
        try:
            await self._read_to_prompt(remaining)
        except RenodeError as exc:
            raise RenodeError(f"connected to the Renode Monitor but saw no prompt: {exc}")

    async def close(self) -> None:
        if self._w is not None:
            try:
                self._w.close()
                await self._w.wait_closed()
            except Exception:
                pass
        self._r = self._w = None

    # ── framing ────────────────────────────────────────────────────────────────
    async def _read_to_prompt(self, timeout: float) -> str:
        """Accumulate until the prompt, or raise. Never returns a partial response."""
        assert self._r is not None
        buf = bytearray()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            budget = deadline - loop.time()
            if budget <= 0:
                raise RenodeError(
                    f"no prompt within {timeout}s; got {len(buf)} byte(s): "
                    f"{strip_iac(bytes(buf))[-200:]!r}")
            try:
                chunk = await asyncio.wait_for(self._r.read(65536), timeout=budget)
            except asyncio.TimeoutError:
                continue
            if not chunk:
                raise RenodeError("Renode Monitor closed the connection")
            buf += chunk
            text = strip_iac(bytes(buf)).decode("utf-8", "replace").replace("\r", "")
            m = _PROMPT.search(text)
            if m:
                self._prompt = m.group(1)
                return text[: m.start()]

    async def execute(self, command: str, timeout: float = 20.0) -> str:
        """Run one Monitor command; return its output with echo and prompt removed.

        Raises RenodeError on Renode's prose errors, so a caller cannot mistake a
        refusal for an empty-but-successful response.
        """
        if self._w is None:
            raise RenodeError("not connected to the Renode Monitor")
        async with self._lock:          # one command in flight: the prompt is shared state
            self._w.write(command.encode() + b"\n")
            await self._w.drain()
            raw = await self._read_to_prompt(timeout)

        lines = raw.split("\n")
        if lines and lines[0].strip() == command.strip():
            lines = lines[1:]                       # measured fact 3: commands echo
        body = "\n".join(lines).strip()
        for line in body.split("\n"):
            s = line.strip()
            for err in _ERRORS:
                if s.startswith(err):
                    raise RenodeError(f"{command!r} -> {s}")
        return body

    @property
    def selected_machine(self) -> Optional[str]:
        """The machine named by the most recent prompt, or None if it said 'monitor'."""
        return None if self._prompt in (None, "monitor") else self._prompt

    # ── the verbs ──────────────────────────────────────────────────────────────
    async def is_paused(self) -> bool:
        out = await self.execute("machine IsPaused")
        first = out.strip().split("\n")[0].strip().lower()
        if first not in ("true", "false"):
            raise RenodeError(f"machine IsPaused returned {out!r}, not True/False")
        return first == "true"

    async def query_status(self) -> dict:
        """The `query-status` analogue, in QMP's own shape so callers need no branch."""
        paused = await self.is_paused()
        return {"running": not paused,
                "status": "paused" if paused else "running",
                "singlestep": False}

    async def system_reset(self) -> None:
        await self.execute("machine Reset")

    async def pause(self) -> None:
        await self.execute("pause")

    async def resume(self) -> None:
        await self.execute("start")

    async def device_tree(self) -> str:
        """`peripherals` — Renode's device tree. The qom-list analogue, and richer:
        it prints each peripheral's class AND its address range in one pass."""
        return await self.execute("peripherals", timeout=30.0)

    async def uptime(self) -> dict[str, str]:
        """Virtual vs real elapsed time. QEMU exposes NOTHING equivalent over QMP —
        this is a place the Renode backend is strictly ahead."""
        out = await self.execute("currentTime")
        got: dict[str, str] = {}
        for line in out.split("\n"):
            if ":" in line:
                k, _, v = line.partition(":")
                k = k.strip().lower()
                if k.startswith("current virtual time"):
                    got["virtual"] = v.strip()
                elif k.startswith("current real time"):
                    got["real"] = v.strip()
        if "virtual" not in got:
            raise RenodeError(f"currentTime returned no virtual time: {out!r}")
        return got

    async def save_snapshot(self, path: str) -> str:
        await self.execute(f"Save @{path}", timeout=120.0)
        return path

    async def load_snapshot(self, path: str) -> str:
        """DISABLED — see RenodeCaps.snapshot_load for the measurement behind it."""
        raise RenodeError(
            "snapshot restore is not exposed on the Renode backend. `Load` runs and "
            "restores, but MEASURED: it kills the guest console. The socket terminal "
            "accepts connections before the Load and refuses them after, because the "
            "restored emulation never had CreateServerSocketTerminal run against it. "
            "Every attached console client would go silent with no indication why, on a "
            "board that still answers status queries. (The related machine-deselect is "
            "fixable with `mach set`; the console is not fixable from this side.)")

    async def quit(self) -> None:
        """Ask Renode to exit. The socket dies mid-command, which is SUCCESS here."""
        if self._w is None:
            return
        try:
            self._w.write(b"quit\n")
            await self._w.drain()
        except Exception:
            return
        # Measured: Renode answers "Renode is quitting" and closes. Reading to a prompt
        # would time out, because a prompt is exactly what a quitting Monitor never sends.
        try:
            await asyncio.wait_for(self._r.read(4096), timeout=5.0)  # type: ignore[union-attr]
        except Exception:
            pass

# SPDX-License-Identifier: GPL-2.0-or-later
"""Interactive container build (the wizard's 'container build' artifact source).

Runs a long, INTERACTIVE build command (the NXP Yocto BSP build) on a PTY so the
browser can attach an xterm: the operator SEES the NXP EULA and types to accept it
(we never auto-accept — they accept NXP's license, not us), then DETACHES (closes
the terminal) while the multi-hour build keeps running, with a live status + a Stop
button. Output goes through a SerialTap-style ring buffer + subscribers so the WS
endpoint reuses the same pump pattern as the board console; stdin (the EULA accept,
or any keystroke) is written back to the PTY master.

The build itself is `docker run` of the nxp-bsp builder image, which emits the
operator's BSP (Image/dtb/.wic + the SM firmware) into a mounted asset dir. The
build command is injected, so a mock command can validate the terminal mechanism
without a real Yocto build.
"""
from __future__ import annotations

import asyncio
import fcntl
import os
import pty
import signal
import struct
import termios
import time
from collections import deque
from typing import Awaitable, Callable, Optional

Subscriber = Callable[[bytes], "Awaitable[None] | None"]


class ContainerBuildError(Exception):
    pass


class ContainerBuild:
    """One interactive PTY-backed build job (e.g. `docker run` the BSP builder)."""

    def __init__(self, board: str, argv: list[str], *, name: Optional[str] = None,
                 stop_argv: Optional[list[str]] = None, env: Optional[dict[str, str]] = None,
                 scrollback: int = 1 << 20) -> None:
        self.board = board
        self.argv = argv
        self.name = name                      # docker container name (for stop), if any
        self.stop_argv = stop_argv            # e.g. ["docker", "stop", name]
        self.env = env                        # full env for the child (None = inherit ours)
        self.state = "created"                # created|running|done|failed|stopped
        self.started_at: Optional[float] = None
        self.ended_at: Optional[float] = None
        self.returncode: Optional[int] = None
        self._buf = bytearray()               # scrollback (bytes), capped
        self._cap = scrollback
        self._subs: list[Subscriber] = []
        self._master_fd: Optional[int] = None
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    # --- subscribers / scrollback (mirrors SerialTap) ----------------------
    def subscribe(self, fn: Subscriber) -> None:
        self._subs.append(fn)

    def unsubscribe(self, fn: Subscriber) -> None:
        try:
            self._subs.remove(fn)
        except ValueError:
            pass

    def snapshot(self) -> bytes:
        return bytes(self._buf)

    def _emit(self, data: bytes) -> None:
        self._buf += data
        if len(self._buf) > self._cap:
            del self._buf[: len(self._buf) - self._cap]
        for fn in list(self._subs):
            try:
                r = fn(data)
                if asyncio.iscoroutine(r) and self._loop:
                    self._loop.create_task(r)
            except Exception:
                pass

    # --- lifecycle ---------------------------------------------------------
    async def start(self) -> None:
        if self.state != "created":
            raise ContainerBuildError("already started")
        self._loop = asyncio.get_event_loop()
        master, slave = pty.openpty()
        self._master_fd = master
        try:
            os.set_blocking(master, False)
            self._set_winsize(master, 40, 120)
        except Exception:
            pass
        # Spawn the build with the PTY as its stdio so EULA prompts/`read` work and
        # programs see a tty. We hold the master end for read (output) + write (stdin).
        self._proc = await asyncio.create_subprocess_exec(
            *self.argv, stdin=slave, stdout=slave, stderr=slave,
            start_new_session=True, env=self.env,  # None -> inherit (default)
        )
        os.close(slave)  # child owns it now
        self.state = "running"
        self.started_at = time.time()
        self._loop.add_reader(master, self._on_readable)
        self._loop.create_task(self._wait())

    def _set_winsize(self, fd: int, rows: int, cols: int) -> None:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    def _on_readable(self) -> None:
        try:
            data = os.read(self._master_fd, 65536)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            data = b""  # PTY closed (child exited)
        if data:
            self._emit(data)
        else:
            if self._master_fd is not None and self._loop:
                self._loop.remove_reader(self._master_fd)

    async def _wait(self) -> None:
        assert self._proc is not None
        rc = await self._proc.wait()
        self.returncode = rc
        self.ended_at = time.time()
        if self.state == "running":
            self.state = "done" if rc == 0 else "failed"
        if self._master_fd is not None:
            try:
                self._loop.remove_reader(self._master_fd)
            except Exception:
                pass
            try:
                os.close(self._master_fd)
            except OSError:
                pass
            self._master_fd = None

    def write(self, data: bytes) -> None:
        """Browser keystrokes -> the PTY (EULA accept, Ctrl-C, etc.)."""
        if self._master_fd is not None and self.state == "running":
            try:
                os.write(self._master_fd, data)
            except OSError:
                pass

    def resize(self, rows: int, cols: int) -> None:
        if self._master_fd is not None:
            try:
                self._set_winsize(self._master_fd, rows, cols)
            except Exception:
                pass

    async def stop(self) -> None:
        """Stop the build. Prefer `docker stop <name>` (graceful container stop);
        else signal the process group."""
        if self.state != "running":
            return
        self.state = "stopped"
        if self.stop_argv:
            try:
                p = await asyncio.create_subprocess_exec(
                    *self.stop_argv,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                await p.wait()
            except Exception:
                pass
        if self._proc and self._proc.returncode is None:
            try:
                os.killpg(os.getpgid(self._proc.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass

    def view(self) -> dict:
        return {
            "board": self.board,
            "state": self.state,
            "returncode": self.returncode,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "elapsed_s": (int((self.ended_at or time.time()) - self.started_at)
                          if self.started_at else None),
            "name": self.name,
        }

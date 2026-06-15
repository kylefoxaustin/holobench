# SPDX-License-Identifier: GPL-2.0-or-later
"""Serial console tap: connect to a QEMU serial chardev unix socket and stream
its bytes to a log file (and optional subscribers).

This is the backend half of the Phase 1 console bridge. In Phase 0 it captures
the boot log so we can prove a board reaches a prompt; in Phase 1 the same tap
feeds a WebSocket → xterm.js. QEMU is started with the chardev as a *listening*
unix socket (``server=on``), so the tap is the client that connects to it.

It is a terminal byte pipe, not a control channel (Prime Directive / security
model): reading and writing terminal bytes only, never QMP.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Awaitable, Callable, Optional

Subscriber = Callable[[bytes], Awaitable[None] | None]


class SerialTap:
    def __init__(self, socket_path: Path, log_path: Path) -> None:
        self.socket_path = socket_path
        self.log_path = log_path
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._task: Optional[asyncio.Task] = None
        self._log = None
        self._subscribers: list[Subscriber] = []

    def subscribe(self, fn: Subscriber) -> None:
        self._subscribers.append(fn)

    def unsubscribe(self, fn: Subscriber) -> None:
        try:
            self._subscribers.remove(fn)
        except ValueError:
            pass

    def snapshot(self) -> bytes:
        """Bytes captured so far (console scrollback) for a new subscriber."""
        try:
            return self.log_path.read_bytes()
        except FileNotFoundError:
            return b""

    async def start(self, *, connect_timeout: float = 10.0) -> None:
        deadline = asyncio.get_event_loop().time() + connect_timeout
        last: Optional[Exception] = None
        while asyncio.get_event_loop().time() < deadline:
            try:
                self._reader, self._writer = await asyncio.open_unix_connection(
                    str(self.socket_path)
                )
                break
            except (FileNotFoundError, ConnectionRefusedError) as exc:
                last = exc
                await asyncio.sleep(0.1)
        else:
            raise RuntimeError(f"serial socket never came up: {self.socket_path}: {last}")

        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log = self.log_path.open("ab", buffering=0)
        self._task = asyncio.create_task(self._pump())

    async def _pump(self) -> None:
        assert self._reader is not None
        try:
            while True:
                data = await self._reader.read(4096)
                if not data:
                    break
                if self._log is not None:
                    self._log.write(data)
                for fn in self._subscribers:
                    res = fn(data)
                    if asyncio.iscoroutine(res):
                        await res
        except asyncio.CancelledError:
            pass

    async def send(self, data: bytes) -> None:
        """Write terminal bytes to the guest (e.g. keystrokes)."""
        if self._writer is None:
            raise RuntimeError("serial tap not connected")
        self._writer.write(data)
        await self._writer.drain()

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._writer is not None:
            try:
                self._writer.close()
            except Exception:
                pass
            self._writer = None
        if self._log is not None:
            self._log.close()
            self._log = None

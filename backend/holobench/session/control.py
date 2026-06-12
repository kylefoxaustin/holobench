# SPDX-License-Identifier: Apache-2.0
"""Daemon-less control of an already-running session via its QMP socket.

Phase 0 has no long-lived backend daemon, but `holobench launch --keep` leaves a
QEMU running. The session's QMP socket lives on disk at
``<base>/<session>/qmp.sock``; these helpers connect to it, issue one standard
QMP command, and disconnect. This is how `reset`/`stop`/`status` act on a session
by id without a daemon — still strictly stock QMP (Prime Directive).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from qemu.qmp import QMPClient

from .manager import DEFAULT_BASE_DIR


class ControlError(Exception):
    pass


def find_session_dir(session: str, base_dir: Optional[Path] = None) -> Path:
    """Resolve a session by exact id or unambiguous prefix."""
    base = base_dir or DEFAULT_BASE_DIR
    exact = base / session
    if (exact / "qmp.sock").exists():
        return exact
    matches = [
        d
        for d in (base.glob(f"{session}*") if base.is_dir() else [])
        if (d / "qmp.sock").exists()
    ]
    if not matches:
        raise ControlError(f"no running session matching '{session}' under {base}")
    if len(matches) > 1:
        names = ", ".join(sorted(d.name for d in matches))
        raise ControlError(f"ambiguous session '{session}': matches {names}")
    return matches[0]


def list_sessions(base_dir: Optional[Path] = None) -> list[str]:
    """Session ids that currently have a live QMP socket."""
    base = base_dir or DEFAULT_BASE_DIR
    if not base.is_dir():
        return []
    return sorted(d.name for d in base.iterdir() if (d / "qmp.sock").exists())


async def _with_qmp(session: str, base_dir: Optional[Path], fn) -> Any:
    sock = find_session_dir(session, base_dir) / "qmp.sock"
    qmp = QMPClient("holobench-ctl")
    try:
        await qmp.connect(str(sock))
    except Exception as exc:
        raise ControlError(f"could not connect to QMP at {sock}: {exc}") from exc
    try:
        return await fn(qmp)
    finally:
        try:
            await qmp.disconnect()
        except Exception:
            pass


async def status(session: str, base_dir: Optional[Path] = None) -> dict[str, Any]:
    return await _with_qmp(session, base_dir, lambda q: q.execute("query-status"))


async def reset(session: str, base_dir: Optional[Path] = None) -> None:
    await _with_qmp(session, base_dir, lambda q: q.execute("system_reset"))


async def pause(session: str, base_dir: Optional[Path] = None) -> None:
    await _with_qmp(session, base_dir, lambda q: q.execute("stop"))


async def resume(session: str, base_dir: Optional[Path] = None) -> None:
    await _with_qmp(session, base_dir, lambda q: q.execute("cont"))


async def stop(session: str, base_dir: Optional[Path] = None) -> None:
    """Tear the session down (QMP quit). QEMU exits and removes its sockets."""
    await _with_qmp(session, base_dir, lambda q: q.execute("quit"))

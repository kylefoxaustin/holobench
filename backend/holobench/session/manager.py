# SPDX-License-Identifier: Apache-2.0
"""Session lifecycle: launch a QEMU process from a profile and drive it via QMP.

A *session* is one reserved virtual board: one QEMU subprocess plus the
backend-owned control channel (QMP over a unix socket). This module is the
isolation seam described in ARCHITECTURE.md §5 — v1 is plain subprocess +
per-session work dir; a future container/namespace backend can replace the
internals without changing the public methods.

Only standard QMP commands are issued here (Prime Directive). The QMP socket
never leaves the backend; callers get mediated control verbs, not raw QMP.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
import tempfile
import time
import uuid
from collections import deque
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from qemu.qmp import EventListener, QMPClient

from ..bridges.console import SerialTap
from ..profiles.models import Profile
from .command import SessionRuntime, build_command, command_str


DEFAULT_BASE_DIR = Path(tempfile.gettempdir()) / "holobench"


def _capture_helper_path(name: str) -> Optional[Path]:
    """Locate a vendored static capture helper by filename.

    Honors $HOLOBENCH_CAPTURE_DIR (set in the container), else resolves the repo's
    vendor/camera/bin relative to this package (dev / pip -e install).
    """
    candidates = []
    env = os.environ.get("HOLOBENCH_CAPTURE_DIR")
    if env:
        candidates.append(Path(env) / name)
    # manager.py = backend/holobench/session/manager.py -> parents[3] = repo root
    candidates.append(Path(__file__).resolve().parents[3] / "vendor" / "camera" / "bin" / name)
    candidates.append(Path("/opt/holobench/vendor/camera/bin") / name)
    for c in candidates:
        if c.is_file():
            return c
    return None


def _free_tcp_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class SessionState(str, Enum):
    CREATED = "created"
    LAUNCHING = "launching"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPED = "stopped"
    FAILED = "failed"


class SessionError(Exception):
    pass


class Session:
    """One emulated board instance."""

    def __init__(
        self,
        profile: Profile,
        *,
        base_dir: Optional[Path] = None,
        asset_dir: Optional[Path] = None,
        session_id: Optional[str] = None,
        owner: Optional[str] = None,
    ) -> None:
        self.profile = profile
        self.id = session_id or f"{profile.id}-{uuid.uuid4().hex[:8]}"
        self.owner = owner  # username that reserved this board (None in open mode)
        self.state = SessionState.CREATED

        # Reservation: a slot of default_minutes, extendable up to max_minutes
        # of total lifetime. The reaper tears the session down at expiry.
        self.created_at = time.time()
        self.expires_at = self.created_at + profile.reservation.default_minutes * 60

        # Keep the work dir short: unix socket paths have a ~108 char limit,
        # so /tmp/holobench-<id> beats a deep nested path.
        base = base_dir or DEFAULT_BASE_DIR
        self.work_dir = base / self.id
        self.asset_dir = asset_dir

        fi = profile.file_injection
        # One per-session inject dir backs BOTH 9p (mounted at /mnt) and TFTP
        # (served at 10.0.2.2): upload once, reach it either way.
        share_dir = (
            self.work_dir / "share"
            if (fi.nine_p.enabled or fi.tftp.enabled)
            else None
        )
        self.share_dir = share_dir
        gdb_port = _free_tcp_port() if profile.introspection.gdbstub.enabled else None
        self.gdb_port = gdb_port
        snapshot_disk = (
            self.work_dir / "snap.qcow2" if profile.introspection.snapshots else None
        )
        # Image-swap: resolve the golden disk + a per-session overlay path.
        img = profile.file_injection.image_swap
        disk_name = profile.boot.artifacts.disk
        self._golden_disk: Optional[Path] = None
        disk_overlay: Optional[Path] = None
        if img.enabled and disk_name:
            self._golden_disk = (
                Path(disk_name)
                if Path(disk_name).is_absolute() or asset_dir is None
                else asset_dir / disk_name
            )
            disk_overlay = self.work_dir / "disk-overlay.qcow2"
        # Virtual camera: a per-session dir the ISI host-frame-source reads.
        self.camera_frames_dir: Optional[Path] = (
            self.work_dir / "frames" if profile.camera.enabled else None
        )
        self.runtime = SessionRuntime(
            work_dir=self.work_dir,
            qmp_socket=self.work_dir / "qmp.sock",
            serial_sockets={
                port.chardev: self.work_dir / f"{port.chardev}.sock"
                for port in profile.serial
            },
            asset_dir=asset_dir,
            share_dir=share_dir,
            gdb_port=gdb_port,
            snapshot_disk=snapshot_disk,
            disk_overlay=disk_overlay,
            camera_frames_dir=self.camera_frames_dir,
        )
        self.argv: list[str] = []
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._qmp: Optional[QMPClient] = None
        self._log_path = self.work_dir / "qemu.log"
        self._taps: dict[str, SerialTap] = {}
        self._events: deque[dict] = deque(maxlen=256)
        self._evlistener: Optional[EventListener] = None
        self._evtask: Optional[asyncio.Task] = None

    # -- lifecycle ----------------------------------------------------------

    async def launch(self, *, qmp_timeout: float = 15.0, tap_serial: bool = True) -> None:
        if self.state not in (SessionState.CREATED,):
            raise SessionError(f"session {self.id} already launched")
        self.state = SessionState.LAUNCHING
        self.work_dir.mkdir(parents=True, exist_ok=True)
        if self.share_dir is not None:
            self.share_dir.mkdir(parents=True, exist_ok=True)
        if self.camera_frames_dir is not None:
            # Keep the upload target dir present, but ARM the ISI host-frame-source
            # only when frames are actually staged: the ISI model fatally errors on
            # an empty frames dir, and an unused camera must not break a normal boot.
            # No frames -> rt.camera_frames_dir = None -> build_command boots the
            # plain board (no camera dtb/-device/-global). Stage frames + reboot to
            # arm it (frames are launch-only anyway).
            self.camera_frames_dir.mkdir(parents=True, exist_ok=True)
            self.runtime.camera_frames_dir = (
                self.camera_frames_dir
                if any(self.camera_frames_dir.glob("*.raw"))
                else None
            )
            # Stage the GPL-2.0 static capture helper into the 9p share so the
            # guest runs it from /mnt (the imx8-isi media links start disabled, so
            # no shipped tool can capture; this standalone helper does the link
            # setup + DQBUF itself). Staged whenever the camera is enabled.
            self._stage_capture_helper()
        if self.runtime.snapshot_disk is not None:
            await self._create_snapshot_disk(self.runtime.snapshot_disk)
        if self.runtime.disk_overlay is not None:
            if self._golden_disk and self._golden_disk.exists():
                await self._create_overlay(self._golden_disk, self.runtime.disk_overlay)
            else:
                # golden disk missing -> degrade gracefully (no swap drive)
                self.runtime.disk_overlay = None
        self.argv = build_command(self.profile, self.runtime)

        log = self._log_path.open("wb")
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *self.argv, stdout=log, stderr=asyncio.subprocess.STDOUT
            )
        except FileNotFoundError as exc:
            self.state = SessionState.FAILED
            raise SessionError(
                f"QEMU binary not found: {self.profile.qemu.binary}"
            ) from exc

        try:
            await self._connect_qmp(qmp_timeout)
        except Exception:
            self.state = SessionState.FAILED
            await self._kill_proc()
            raise

        if tap_serial:
            await self._start_serial_taps()
        self._start_event_capture()
        self.state = SessionState.RUNNING

    def _stage_capture_helper(self) -> None:
        """Copy the board's static V4L2 capture helper into the 9p share (->/mnt)."""
        cam = self.profile.camera
        if not (cam.enabled and cam.capture_binary and self.share_dir is not None):
            return
        src = _capture_helper_path(cam.capture_binary)
        if src is None:
            return  # binary not built/available -> degrade (panel hint still shown)
        try:
            dest = self.share_dir / cam.capture_binary
            shutil.copy2(src, dest)
            dest.chmod(0o755)
        except OSError:
            pass

    def _start_event_capture(self) -> None:
        if self._qmp is None:
            return
        self._evlistener = EventListener()  # all events
        self._qmp.register_listener(self._evlistener)
        self._evtask = asyncio.create_task(self._pump_events())

    async def _pump_events(self) -> None:
        assert self._evlistener is not None
        try:
            async for event in self._evlistener:
                self._events.append(
                    {
                        "event": event.get("event"),
                        "timestamp": event.get("timestamp"),
                        "data": dict(event.get("data", {})),
                    }
                )
        except asyncio.CancelledError:
            pass

    async def _start_serial_taps(self) -> None:
        for port in self.profile.serial:
            sock = self.runtime.serial_sockets.get(port.chardev)
            if sock is None:
                continue
            tap = SerialTap(sock, self.work_dir / f"{port.chardev}.log")
            try:
                await tap.start()
            except Exception:
                # A console tap failing must not kill the session.
                continue
            self._taps[port.chardev] = tap

    def get_tap(self, chardev: Optional[str] = None) -> Optional[SerialTap]:
        """The live serial tap for a chardev (default = the default UART)."""
        if chardev is None:
            dp = self.profile.default_serial
            chardev = dp.chardev if dp else None
        if chardev is None:
            return None
        return self._taps.get(chardev)

    def console_log(self, chardev: Optional[str] = None) -> Optional[Path]:
        """Path to a serial port's captured log (default = the default UART)."""
        if chardev is None:
            dp = self.profile.default_serial
            chardev = dp.chardev if dp else None
        if chardev is None:
            return None
        return self.work_dir / f"{chardev}.log"

    async def _connect_qmp(self, timeout: float) -> None:
        sock = self.runtime.qmp_socket
        deadline = asyncio.get_event_loop().time() + timeout
        # Wait for QEMU to create the socket, and bail early if it died.
        while not sock.exists():
            if self._proc and self._proc.returncode is not None:
                raise SessionError(
                    f"QEMU exited (code {self._proc.returncode}) before QMP "
                    f"came up. See {self._log_path}"
                )
            if asyncio.get_event_loop().time() > deadline:
                raise SessionError(f"QMP socket never appeared at {sock}")
            await asyncio.sleep(0.05)

        qmp = QMPClient(self.id)
        last: Optional[Exception] = None
        while asyncio.get_event_loop().time() < deadline:
            try:
                await qmp.connect(str(sock))
                self._qmp = qmp
                return
            except Exception as exc:  # connection races the server coming up
                last = exc
                await asyncio.sleep(0.1)
        raise SessionError(f"could not connect to QMP at {sock}: {last}")

    # -- mediated control verbs (standard QMP only) -------------------------

    async def _execute(self, cmd: str, args: Optional[dict[str, Any]] = None) -> Any:
        if self._qmp is None:
            raise SessionError("QMP not connected")
        return await self._qmp.execute(cmd, args)

    async def query_status(self) -> dict[str, Any]:
        return await self._execute("query-status")

    async def system_reset(self) -> None:
        await self._execute("system_reset")

    async def pause(self) -> None:
        await self._execute("stop")
        self.state = SessionState.PAUSED

    async def resume(self) -> None:
        await self._execute("cont")
        self.state = SessionState.RUNNING

    # -- introspection (read-only; the "beat the hardware" panel) -----------

    async def hmp(self, command: str) -> str:
        """Run a read-only HMP `info` query via human-monitor-command."""
        return await self._execute("human-monitor-command", {"command-line": command})

    async def qom_list(self, path: str = "/machine") -> Any:
        return await self._execute("qom-list", {"path": path})

    async def qom_get(self, path: str, prop: str) -> Any:
        return await self._execute("qom-get", {"path": path, "property": prop})

    def recent_events(self) -> list[dict]:
        return list(self._events)

    # -- snapshots (savevm/loadvm; needs the scratch qcow2) -----------------

    @staticmethod
    async def _run_qemu_img(*args: str, what: str) -> None:
        try:
            proc = await asyncio.create_subprocess_exec(
                "qemu-img", *args,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            # qemu-img not installed -> degrade cleanly instead of a raw 500.
            raise SessionError(
                f"{what} needs qemu-img, which is not installed "
                f"(install qemu-utils)"
            ) from exc
        _, err = await proc.communicate()
        if proc.returncode != 0:
            raise SessionError(f"{what} failed: {err.decode(errors='replace')}")

    @classmethod
    async def _create_snapshot_disk(cls, path: Path, size: str = "256M") -> None:
        await cls._run_qemu_img(
            "create", "-f", "qcow2", str(path), size, what="snapshot disk create"
        )

    # The QEMU SD-card model requires the image size to be a multiple of 512 KiB.
    _SD_ALIGN = 512 * 1024

    @classmethod
    async def _create_overlay(cls, golden: Path, overlay: Path) -> None:
        """Fresh qcow2 overlay over the golden disk (writes isolated)."""
        await cls._run_qemu_img(
            "create", "-f", "qcow2", "-b", str(golden.resolve()), "-F", "raw",
            str(overlay), what="disk overlay create",
        )
        # Round the overlay's virtual size up to a 512 KiB boundary so it's a
        # valid -drive if=sd image (the extra space is unpartitioned/zero-filled,
        # so it never touches the golden's data or partition table).
        size = golden.stat().st_size
        rounded = -(-size // cls._SD_ALIGN) * cls._SD_ALIGN
        if rounded != size:
            await cls._run_qemu_img(
                "resize", str(overlay), str(rounded), what="disk overlay resize"
            )

    async def snapshot_save(self, name: str) -> str:
        return await self.hmp(f"savevm {name}")

    async def snapshot_load(self, name: str) -> str:
        return await self.hmp(f"loadvm {name}")

    async def snapshot_delete(self, name: str) -> str:
        return await self.hmp(f"delvm {name}")

    async def snapshot_list(self) -> list[dict]:
        text = await self.hmp("info snapshots")
        snaps: list[dict] = []
        for line in text.splitlines():
            line = line.strip()
            if (
                not line
                or line.startswith(("List of", "ID", "There is no"))
            ):
                continue
            parts = line.split()
            if len(parts) >= 2:
                snaps.append({"id": parts[0], "tag": parts[1], "raw": line})
        return snaps

    async def screendump(self, path: Path, fmt: Optional[str] = "png") -> Path:
        """Capture the board's framebuffer (LCDIF/DPU) via QMP screendump."""
        args: dict[str, Any] = {"filename": str(path)}
        if fmt:
            args["format"] = fmt
        await self._execute("screendump", args)
        return path

    async def quit(self) -> None:
        """Graceful QMP quit, then ensure the process is gone."""
        try:
            if self._qmp is not None:
                await self._execute("quit")
        except Exception:
            pass
        for tap in self._taps.values():
            await tap.stop()
        self._taps.clear()
        if self._evtask is not None:
            self._evtask.cancel()
            try:
                await self._evtask
            except (asyncio.CancelledError, Exception):
                pass
            self._evtask = None
        await self._disconnect_qmp()
        await self._kill_proc()
        self.state = SessionState.STOPPED

    # -- teardown -----------------------------------------------------------

    async def _disconnect_qmp(self) -> None:
        if self._qmp is not None:
            try:
                await self._qmp.disconnect()
            except Exception:
                pass
            self._qmp = None

    async def _kill_proc(self) -> None:
        if self._proc is None:
            return
        if self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._proc.kill()
                await self._proc.wait()

    def cleanup(self) -> None:
        """Remove the session work dir. Call after quit()."""
        shutil.rmtree(self.work_dir, ignore_errors=True)

    @property
    def pid(self) -> Optional[int]:
        return self._proc.pid if self._proc else None

    # -- reservation --------------------------------------------------------

    @property
    def remaining_seconds(self) -> int:
        return max(0, int(self.expires_at - time.time()))

    @property
    def expired(self) -> bool:
        return time.time() >= self.expires_at

    def extend(self, minutes: int) -> int:
        """Extend the reservation, capped at max_minutes total lifetime."""
        ceiling = self.created_at + self.profile.reservation.max_minutes * 60
        self.expires_at = min(self.expires_at + minutes * 60, ceiling)
        return self.remaining_seconds


class SessionManager:
    """Owns the live fleet of sessions, keyed by session id."""

    def __init__(self, base_dir: Optional[Path] = None) -> None:
        self.base_dir = base_dir
        self._sessions: dict[str, Session] = {}

    async def launch(
        self,
        profile: Profile,
        *,
        asset_dir: Optional[Path] = None,
        owner: Optional[str] = None,
    ) -> Session:
        session = Session(
            profile, base_dir=self.base_dir, asset_dir=asset_dir, owner=owner
        )
        await session.launch()
        self._sessions[session.id] = session
        return session

    def get(self, session_id: str) -> Session:
        if session_id not in self._sessions:
            raise SessionError(f"no session '{session_id}'")
        return self._sessions[session_id]

    def peek(self, session_id: str) -> Optional[Session]:
        return self._sessions.get(session_id)

    def list(self) -> list[Session]:
        return list(self._sessions.values())

    async def reinstall(self, session_id: str) -> Session:
        """Cold cycle: tear the session down and relaunch the same board fresh."""
        old = self.get(session_id)
        profile, asset_dir, owner = old.profile, old.asset_dir, old.owner
        await self.destroy(session_id)
        return await self.launch(profile, asset_dir=asset_dir, owner=owner)

    async def destroy(self, session_id: str, *, cleanup: bool = True) -> None:
        session = self.get(session_id)
        await session.quit()
        if cleanup:
            session.cleanup()
        del self._sessions[session_id]

    async def shutdown_all(self) -> None:
        for sid in list(self._sessions):
            await self.destroy(sid)

    async def reap_expired(self) -> list[str]:
        """Tear down sessions whose reservation has expired. Returns their ids."""
        reaped = []
        for sid, session in list(self._sessions.items()):
            if session.expired:
                try:
                    await self.destroy(sid)
                    reaped.append(sid)
                except SessionError:
                    pass
        return reaped

    async def run_reaper(self, interval: float = 15.0) -> None:
        """Background loop that enforces reservation deadlines."""
        while True:
            await asyncio.sleep(interval)
            await self.reap_expired()


__all__ = [
    "Session",
    "SessionManager",
    "SessionState",
    "SessionError",
    "command_str",
]

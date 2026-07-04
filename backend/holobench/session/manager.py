# SPDX-License-Identifier: GPL-2.0-or-later
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
from .isolation import SessionCgroup, memory_max_bytes


DEFAULT_BASE_DIR = Path(tempfile.gettempdir()) / "holobench"


def _make_qemu_preexec(cgroup_procs: Optional[str] = None):  # pragma: no cover
    """Build the preexec_fn for a QEMU child (runs after fork, before exec).

    Joins the per-session cgroup (if one was created) by writing its own pid to
    cgroup.procs, then applies rlimits. Safe-by-default: RLIMIT_CORE=0 always (a
    crashed multi-GB-RAM QEMU would otherwise dump a multi-GB core — disk-fill +
    guest-memory info-leak). HOLOBENCH_NICE deprioritizes the board. Memory is
    NOT capped via RLIMIT_AS — QEMU/TCG reserves tens of GB of (unbacked) virtual
    address space, so an RLIMIT_AS sized to guest RAM kills it; hard memory caps
    come from the cgroup's RSS-based memory.max (session/isolation.py, DEPLOY.md).
    """

    def _pre() -> None:
        import os
        import resource

        if cgroup_procs:
            try:
                with open(cgroup_procs, "w") as f:
                    f.write(str(os.getpid()))
            except OSError:
                pass
        try:
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        except Exception:
            pass
        try:
            nice = int(os.environ.get("HOLOBENCH_NICE", "0") or 0)
            if nice:
                os.nice(nice)
        except Exception:
            pass

    return _pre


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
        minutes: Optional[int] = None,
        lcd_attached: bool = False,
        nic_override: Optional[list[str]] = None,
        usb_override: Optional[list[str]] = None,
        uart_link_override: Optional[list[str]] = None,
        spi_link_override: Optional[list[str]] = None,
        i2c_link_override: Optional[list[str]] = None,
        can_link_override: Optional[list[str]] = None,
        machine_extra: Optional[str] = None,
        append_extra: Optional[str] = None,
        dtb_override: Optional[str] = None,
        qemu_binary: Optional[str] = None,
    ) -> None:
        self.profile = profile
        # Boot with the attachable display panel (display.attach_dtb) so the DPU
        # scans out. Carried across reinstall/relaunch so the LCD stays attached.
        self.lcd_attached = bool(lcd_attached and profile.display.attach_dtb)
        self.id = session_id or f"{profile.id}-{uuid.uuid4().hex[:8]}"
        self.owner = owner  # username that reserved this board (None in open mode)
        self.state = SessionState.CREATED

        # Reservation: a slot of `minutes` (or the profile default), extendable up
        # to max_minutes. A value <= 0 means INFINITE — expires_at is None, the
        # reaper never touches it. The reaper tears finite sessions down at expiry.
        self.created_at = time.time()
        # Reservation TIMERS REMOVED: every session is infinite (expires_at = None),
        # so there is no countdown and the reaper never tears a board down. The
        # `minutes` arg and profile.reservation are kept for API/back-compat but no
        # longer bound a session's lifetime — a reserved board stays up until the
        # operator stops it. (Per Kyle: no timers on the reservation system.)
        self.expires_at: Optional[float] = None

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
            lcd_attached=self.lcd_attached,
            nic_override=nic_override,
            usb_override=usb_override,
            uart_link_override=uart_link_override,
            spi_link_override=spi_link_override,
            i2c_link_override=i2c_link_override,
            can_link_override=can_link_override,
            machine_extra=machine_extra,
            append_extra=append_extra,
            dtb_override=dtb_override,
            qemu_binary=qemu_binary,
        )
        # v3.0 fabric: which lab (if any) owns this node, and its node name in it.
        self.lab_id: Optional[str] = None
        self.lab_node: Optional[str] = None
        self.argv: list[str] = []
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._qmp: Optional[QMPClient] = None
        self._cgroup: Optional[SessionCgroup] = None
        self._log_path = self.work_dir / "qemu.log"
        self._taps: dict[str, SerialTap] = {}
        # Lazy serial: when on, taps attach on first console connect (ref-counted)
        # and detach on the last disconnect — no always-on serial pump per board
        # (resource hygiene at density). Off = full boot history captured always.
        self._lazy_serial = os.environ.get("HOLOBENCH_LAZY_SERIAL") == "1"
        self._tap_refs: dict[str, int] = {}
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

        # Per-session cgroup v2 caps (opt-in; no-op when disabled/unavailable).
        self._cgroup = SessionCgroup.create(
            self.id,
            memory_max=memory_max_bytes(self.profile.qemu.memory),
            pids_max=int(os.environ.get("HOLOBENCH_PIDS_MAX", "512") or 512),
            cpu_cores=float(os.environ["HOLOBENCH_CPU_CORES"])
            if os.environ.get("HOLOBENCH_CPU_CORES")
            else None,
        )
        procs = self._cgroup.procs_file if self._cgroup else None

        log = self._log_path.open("wb")
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *self.argv, stdout=log, stderr=asyncio.subprocess.STDOUT,
                preexec_fn=_make_qemu_preexec(procs),
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

        if tap_serial and not self._lazy_serial:
            await self._start_serial_taps()
        self._start_event_capture()
        self.state = SessionState.RUNNING

    def _stage_capture_helper(self) -> None:
        """Stage the V4L2 capture helper + any sensor .ko into the 9p share (->/mnt)."""
        cam = self.profile.camera
        if not (cam.enabled and self.share_dir is not None):
            return
        # The static capture helper (vendored, GPL-2.0).
        if cam.capture_binary:
            src = _capture_helper_path(cam.capture_binary)
            if src is not None:
                try:
                    dest = self.share_dir / cam.capture_binary
                    shutil.copy2(src, dest)
                    dest.chmod(0o755)
                except OSError:
                    pass
        # Sensor kernel modules the rootfs lacks (resolved from the asset dir,
        # like the dtb); the guest insmods /mnt/<name> to bind the sensor.
        for mod in cam.guest_modules:
            src = (
                Path(mod)
                if Path(mod).is_absolute() or self.asset_dir is None
                else self.asset_dir / mod
            )
            try:
                if src.is_file():
                    shutil.copy2(src, self.share_dir / Path(mod).name)
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

    def _resolve_chardev(self, chardev: Optional[str]) -> Optional[str]:
        if chardev is None:
            dp = self.profile.default_serial
            chardev = dp.chardev if dp else None
        return chardev

    async def ensure_tap(self, chardev: Optional[str] = None) -> Optional[SerialTap]:
        """Get the tap for a chardev, lazily starting it (ref-counted) if needed.
        With lazy serial off, the always-on tap already exists."""
        cd = self._resolve_chardev(chardev)
        if cd is None:
            return None
        tap = self._taps.get(cd)
        if tap is None:
            sock = self.runtime.serial_sockets.get(cd)
            if sock is None:
                return None
            tap = SerialTap(sock, self.work_dir / f"{cd}.log")
            try:
                await tap.start()
            except Exception:
                return None
            self._taps[cd] = tap
        self._tap_refs[cd] = self._tap_refs.get(cd, 0) + 1
        return tap

    async def release_tap(self, chardev: Optional[str] = None) -> None:
        """Drop a console consumer; stop the tap when the last one leaves (lazy mode)."""
        cd = self._resolve_chardev(chardev)
        if cd is None or cd not in self._tap_refs:
            return
        self._tap_refs[cd] -= 1
        if self._tap_refs[cd] <= 0:
            self._tap_refs.pop(cd, None)
            if self._lazy_serial:  # only tear down lazily-started taps
                tap = self._taps.pop(cd, None)
                if tap is not None:
                    await tap.stop()

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
        """Remove the session work dir + cgroup. Call after quit()."""
        if self._cgroup is not None:
            self._cgroup.cleanup()
            self._cgroup = None
        shutil.rmtree(self.work_dir, ignore_errors=True)

    @property
    def pid(self) -> Optional[int]:
        return self._proc.pid if self._proc else None

    # -- reservation --------------------------------------------------------

    @property
    def infinite(self) -> bool:
        return self.expires_at is None

    @property
    def remaining_seconds(self) -> Optional[int]:
        """Seconds left, or None for an infinite (no-expiry) reservation."""
        if self.expires_at is None:
            return None
        return max(0, int(self.expires_at - time.time()))

    @property
    def expired(self) -> bool:
        return self.expires_at is not None and time.time() >= self.expires_at

    def extend(self, minutes: int) -> Optional[int]:
        """Extend the reservation. Returns the new remaining seconds (None if
        infinite). Rules: an already-infinite reservation stays infinite (extend
        never downgrades it). minutes <= 0 requests infinite — granted only on an
        unbounded profile (max_minutes <= 0), else clamped to the max_minutes
        ceiling. A finite extend is capped at max_minutes total lifetime."""
        if self.expires_at is None:
            return None  # already infinite — stays infinite
        maxm = self.profile.reservation.max_minutes
        if minutes <= 0:
            if maxm <= 0:
                self.expires_at = None
                return None
            self.expires_at = self.created_at + maxm * 60  # clamp to the cap
            return self.remaining_seconds
        ceiling = None if maxm <= 0 else self.created_at + maxm * 60
        new = self.expires_at + minutes * 60
        self.expires_at = new if ceiling is None else min(new, ceiling)
        return self.remaining_seconds


class SessionManager:
    """Owns the live fleet of sessions, keyed by session id."""

    def __init__(self, base_dir: Optional[Path] = None) -> None:
        self.base_dir = base_dir
        self._sessions: dict[str, Session] = {}
        # Admission control: cap concurrent in-flight launches (0 = unlimited) so a
        # burst of "Reserve & Boot" doesn't stampede the host (qemu-img + QEMU exec
        # + boot CPU storm). After each launch the slot frees only after
        # HOLOBENCH_LAUNCH_STAGGER_S, pacing boot starts in waves. See docs/SCALING.md.
        n = int(os.environ.get("HOLOBENCH_MAX_CONCURRENT_LAUNCHES", "0") or 0)
        self._launch_sem = asyncio.Semaphore(n) if n > 0 else None
        self._launch_stagger = float(os.environ.get("HOLOBENCH_LAUNCH_STAGGER_S", "0") or 0)

    async def _release_after(self, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        finally:
            if self._launch_sem is not None:
                self._launch_sem.release()

    async def launch(
        self,
        profile: Profile,
        *,
        asset_dir: Optional[Path] = None,
        owner: Optional[str] = None,
        minutes: Optional[int] = None,
        lcd_attached: bool = False,
        nic_override: Optional[list[str]] = None,
        usb_override: Optional[list[str]] = None,
        uart_link_override: Optional[list[str]] = None,
        spi_link_override: Optional[list[str]] = None,
        i2c_link_override: Optional[list[str]] = None,
        can_link_override: Optional[list[str]] = None,
        machine_extra: Optional[str] = None,
        append_extra: Optional[str] = None,
        dtb_override: Optional[str] = None,
        qemu_binary: Optional[str] = None,
    ) -> Session:
        if self._launch_sem is not None:
            await self._launch_sem.acquire()
        try:
            session = Session(
                profile, base_dir=self.base_dir, asset_dir=asset_dir, owner=owner,
                minutes=minutes, lcd_attached=lcd_attached, nic_override=nic_override,
                usb_override=usb_override, uart_link_override=uart_link_override,
                spi_link_override=spi_link_override, i2c_link_override=i2c_link_override,
                can_link_override=can_link_override,
                machine_extra=machine_extra, append_extra=append_extra,
                dtb_override=dtb_override,
                qemu_binary=qemu_binary,
            )
            await session.launch()
            self._sessions[session.id] = session
        except BaseException:
            if self._launch_sem is not None:
                self._launch_sem.release()
            raise
        # Success: free the admission slot — after a stagger delay if configured, so
        # the next queued launch's boot starts later (response returns immediately).
        if self._launch_sem is not None:
            if self._launch_stagger > 0:
                asyncio.create_task(self._release_after(self._launch_stagger))
            else:
                self._launch_sem.release()
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
        # Preserve the reservation kind across a reinstall (infinite stays infinite).
        minutes = 0 if old.infinite else max(1, (old.remaining_seconds or 0) // 60)
        await self.destroy(session_id)
        return await self.launch(profile, asset_dir=asset_dir, owner=owner, minutes=minutes,
                                 lcd_attached=old.lcd_attached)

    async def set_lcd(self, session_id: str, on: bool) -> Session:
        """Reboot the board with/without the attachable display panel.

        The panel is a boot-time dtb, so toggling it relaunches QEMU (like
        reinstall) with display.attach_dtb selected. Reservation kind + owner are
        preserved. No-op fast path if already in the requested state.
        """
        old = self.get(session_id)
        if not old.profile.display.attach_dtb:
            raise SessionError("this board has no attachable display panel")
        if bool(old.lcd_attached) == bool(on):
            return old
        profile, asset_dir, owner = old.profile, old.asset_dir, old.owner
        minutes = 0 if old.infinite else max(1, (old.remaining_seconds or 0) // 60)
        await self.destroy(session_id)
        return await self.launch(profile, asset_dir=asset_dir, owner=owner, minutes=minutes,
                                 lcd_attached=on)

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

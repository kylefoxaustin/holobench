"""Resolve a validated profile into a concrete QEMU command line.

This is the only place a profile becomes process arguments. It stays strictly
board-agnostic: every board-specific value comes from the profile, never from
``if soc == ...`` branches. It emits only standard, upstreamable QEMU flags
(Prime Directive): ``-machine``, ``-m``, ``-smp``, ``-qmp`` (unix socket),
``-chardev``/``-serial``, ``-display vnc``/``-display none``, ``-gdb``, and the
profile's boot artifacts.
"""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ..profiles.models import BootMode, Profile


@dataclass
class SessionRuntime:
    """Per-session resources the resolver wires QEMU up to.

    The session manager allocates these (work dir, sockets, ports) before
    building the command line. The browser never sees any of them directly;
    they are the backend-side control surface.
    """

    work_dir: Path
    qmp_socket: Path
    # chardev id -> unix socket path the console bridge will connect to
    serial_sockets: dict[str, Path] = field(default_factory=dict)
    vnc: Optional[str] = None  # e.g. "unix:/path/vnc.sock" or ":1"
    gdb_port: Optional[int] = None
    # Directory holding the profile's boot artifacts (flash.bin / Image / dtb).
    asset_dir: Optional[Path] = None
    # Host dir shared into the guest over virtio-9p (file injection).
    share_dir: Optional[Path] = None
    # Scratch qcow2 backing savevm/loadvm snapshots (not used by the guest).
    snapshot_disk: Optional[Path] = None
    # Per-session qcow2 overlay over the golden disk (image-swap / reinstall).
    disk_overlay: Optional[Path] = None


class CommandError(Exception):
    """Raised when a profile cannot be turned into a runnable command line."""


def _resolve_artifact(name: Optional[str], asset_dir: Optional[Path]) -> Optional[str]:
    if not name:
        return None
    p = Path(name)
    if p.is_absolute() or asset_dir is None:
        return str(p)
    return str(asset_dir / p)


def _boot_args(profile: Profile, rt: SessionRuntime) -> list[str]:
    boot = profile.boot
    art = boot.artifacts

    # Explicit override wins: format the template, then shell-split it.
    if boot.command_template:
        tokens = {
            "flash_bin": _resolve_artifact(art.flash_bin, rt.asset_dir) or "",
            "kernel": _resolve_artifact(art.kernel, rt.asset_dir) or "",
            "dtb": _resolve_artifact(art.dtb, rt.asset_dir) or "",
            "initrd": _resolve_artifact(art.initrd, rt.asset_dir) or "",
            "rootfs": _resolve_artifact(art.rootfs, rt.asset_dir) or "",
            "session": str(rt.work_dir),
        }
        try:
            rendered = boot.command_template.format(**tokens)
        except KeyError as exc:
            raise CommandError(
                f"command_template references unknown token {exc} in profile "
                f"'{profile.id}'"
            ) from exc
        return shlex.split(rendered)

    flash = _resolve_artifact(art.flash_bin, rt.asset_dir)
    kernel = _resolve_artifact(art.kernel, rt.asset_dir)
    dtb = _resolve_artifact(art.dtb, rt.asset_dir)
    initrd = _resolve_artifact(art.initrd, rt.asset_dir)
    rootfs = _resolve_artifact(art.rootfs, rt.asset_dir)

    args: list[str] = []
    if boot.mode in (BootMode.flash, BootMode.uboot):
        # i.MX flash boot: the boot blob is loaded via -bios (imx-boot/flash.bin).
        if flash:
            args += ["-bios", flash]
    elif boot.mode is BootMode.direct_kernel:
        if kernel:
            args += ["-kernel", kernel]
            if dtb:
                args += ["-dtb", dtb]
            if initrd:
                args += ["-initrd", initrd]
            if boot.append:
                args += ["-append", boot.append]

    if rootfs:
        # SD/eMMC image for boards that root off a disk rather than initramfs.
        args += ["-drive", f"file={rootfs},format=raw,if=sd"]
    return args


def build_command(profile: Profile, rt: SessionRuntime) -> list[str]:
    """Build the full QEMU argv for a session. Pure function, no side effects."""
    q = profile.qemu
    # HOLOBENCH_QEMU overrides the profile's binary path (used by the container
    # image so one baked qemu serves every profile); else expand any ${VARS}.
    binary = os.environ.get("HOLOBENCH_QEMU") or os.path.expandvars(q.binary)
    argv: list[str] = [binary]

    argv += ["-machine", q.machine]
    argv += ["-m", q.memory]
    if q.smp is not None:
        argv += ["-smp", str(q.smp)]
    # Always pin the audio backend (default driver=none) so QEMU never grabs the
    # host's real audio device — the i.MX models would otherwise beep.
    if q.audio:
        argv += ["-audio", f"driver={q.audio}"]

    # Control plane: QMP over a unix socket the backend owns. Never exposed
    # to the browser.
    argv += [
        "-qmp",
        f"unix:{rt.qmp_socket},server=on,wait=off",
    ]

    # Serial consoles: one unix-socket chardev per declared UART, in order.
    for port in profile.serial:
        sock = rt.serial_sockets.get(port.chardev)
        if sock is None:
            raise CommandError(
                f"profile '{profile.id}' declares serial chardev "
                f"'{port.chardev}' but no socket was allocated for it"
            )
        argv += [
            "-chardev",
            f"socket,id={port.chardev},path={sock},server=on,wait=off",
            "-serial",
            f"chardev:{port.chardev}",
        ]

    # Display: VNC for the LCD panel, otherwise headless.
    if profile.display.enabled and profile.display.vnc and rt.vnc:
        argv += ["-display", f"vnc={rt.vnc}"]
    else:
        argv += ["-display", "none"]

    # Debug: standard gdbstub, bound to localhost only.
    if profile.introspection.gdbstub.enabled and rt.gdb_port:
        argv += ["-gdb", f"tcp:127.0.0.1:{rt.gdb_port}"]

    # Snapshots: a scratch qcow2 (if=none, not wired to the guest) gives savevm
    # somewhere to store VM state on boards that boot from initramfs (no disk).
    if profile.introspection.snapshots and rt.snapshot_disk is not None:
        argv += [
            "-drive",
            f"if=none,id=hbsnap,file={rt.snapshot_disk},format=qcow2",
        ]

    # Networking: user-mode (slirp) NICs. The board's modeled NICs auto-attach
    # in order. The first NIC carries the TFTP server when file injection wants
    # it (guest `tftp -g ... 10.0.2.2` / u-boot tftpboot, mirroring the farm).
    tftp = profile.file_injection.tftp
    for i in range(profile.net.user_nics):
        opts = "user"
        if i == 0 and tftp.enabled and rt.share_dir is not None:
            opts += f",tftp={rt.share_dir}"
        argv += ["-nic", opts]

    # Image-swap: per-session qcow2 overlay over the golden disk. Guest writes hit
    # the overlay; the golden is never touched; "reinstall" = fresh overlay.
    # Attachment depends on the board's rootfs medium (image_swap.target_drive):
    #   "sd"   -> -drive if=sd            (uSDHC SD card; 91/93)
    #   "emmc" -> -drive if=none + -device emmc  (non-removable eMMC; 95)
    # Either way the guest sees /dev/mmcblk0 (eMMC) or the SD's mmcblkN.
    img = profile.file_injection.image_swap
    if img.enabled and rt.disk_overlay is not None:
        if (img.target_drive or "sd") == "emmc":
            argv += [
                "-drive", f"if=none,id=hbdisk,file={rt.disk_overlay},format=qcow2",
                "-device", "emmc,drive=hbdisk",
            ]
        else:
            argv += ["-drive", f"if=sd,file={rt.disk_overlay},format=qcow2"]

    # File injection: virtio-9p live share (host dir -> guest mount_tag).
    nine_p = profile.file_injection.nine_p
    if nine_p.enabled and rt.share_dir is not None:
        # security_model=none: the 9p server uses the host file's real perms, so
        # files the backend drops in are readable by the guest and guest writes
        # land as real host files (a simple bidirectional drop-box). mapped-xattr
        # would hide host-created file content from the guest (missing virtfs
        # xattrs). Runs unprivileged.
        argv += [
            "-fsdev",
            f"local,id=hbfs0,path={rt.share_dir},security_model=none",
            "-device",
            f"{nine_p.device},fsdev=hbfs0,mount_tag={nine_p.mount_tag}",
        ]

    argv += _boot_args(profile, rt)
    argv += list(q.extra_args)
    return argv


def command_str(argv: list[str]) -> str:
    """A copy-pasteable, shell-quoted rendering of an argv (for logs/CLI)."""
    return " ".join(shlex.quote(a) for a in argv)

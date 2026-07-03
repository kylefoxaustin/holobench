# SPDX-License-Identifier: GPL-2.0-or-later
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
    # Per-session dir of raw frames fed to the board's ISI (virtual camera).
    camera_frames_dir: Optional[Path] = None
    # Boot with the display.attach_dtb (panel attached) instead of the stock dtb,
    # so the DPU has a connector/mode and scans out ("Attach LCD").
    lcd_attached: bool = False
    # v3.0 fabric: replace the profile's user-mode NICs with these exact `-nic`
    # backend specs (e.g. "socket,mcast=230.0.0.10:1234"), so a board joins a
    # multi-board L2 segment instead of slirp. None = use the profile's user NICs.
    nic_override: Optional[list[str]] = None
    # v3.0 fabric (USB): raw extra QEMU args wiring this board into a usbredir
    # inter-board link — a stock `-chardev socket` + the importer `-device usb-redir`
    # (host role) or the exporter `-global <usbdev>.chardev=` (device role). Built by
    # the lab coordinator from the profile's `usb:` block. None = no inter-board USB.
    usb_override: Optional[list[str]] = None
    # Per-board QEMU binary built on this host by the setup wizard ("build me a
    # board"). When set it wins over $HOLOBENCH_QEMU / the profile path, so the
    # running app boots a board with the QEMU it just built — closing the
    # build->boot seam. None = normal resolution.
    qemu_binary: Optional[str] = None
    # External-console mode: expose each declared UART as a labeled host PTY
    # (`-chardev pty`) instead of the browser bridge's unix socket, so a plain
    # terminal (PuTTY -serial, screen, minicom) attaches directly — the way a dev
    # consoles into a real EVK. QEMU prints the assigned /dev/pts/N per label.
    external_console: bool = False
    # When set, add a stock user-net NIC with a host->guest :22 forward on this
    # port (SSH access), on a virtio-net-device so it works on any board with a
    # virtio-mmio bus regardless of whether the SoC's own ENET binds a netdev.
    ssh_forward_port: Optional[int] = None
    # v3.0 fabric (UART): raw extra QEMU args wiring this board's spare link-UART
    # into a board-to-board serial bridge — a stock `-chardev socket` (server on
    # one end, client on the other) + `-serial chardev:<id>`, appended right after
    # the declared consoles so the link UART is the next serial_hd(). Built by the
    # lab coordinator from the profile's `uart:` block. None = no inter-board UART.
    uart_link_override: Optional[list[str]] = None
    # v3.0 fabric (SPI): raw extra QEMU args wiring this board's spare LPSPI into a
    # board-to-board SPI bridge — a stock `-chardev socket` + `-device spi-link`
    # (the model's inter-QEMU SSI bridge peripheral). Appended verbatim; built by the
    # lab coordinator from the profile's `spi:` block. None = no inter-board SPI.
    spi_link_override: Optional[list[str]] = None
    # v3.0 fabric (CAN): raw extra QEMU args wiring this board's FlexCAN into a
    # board-to-board CAN bridge — a stock `-object can-bus` + `-chardev socket` +
    # `-object can-host-chardev` (the fleet-shared generic CAN transport, no host
    # vcan/SocketCAN). Appended verbatim; built by the coordinator from the can:
    # block. None = no inter-board CAN.
    can_link_override: Optional[list[str]] = None
    # Extra `-machine` properties a fabric link needs (e.g. CAN's canbus0=cb,
    # canbus1=cb wiring the machine's can-buses to the bridged bus). None = normal.
    machine_extra: Optional[str] = None
    # Boot a specific dtb instead of the profile default (e.g. the LPUART2-enabled
    # dtb a UART link needs, or the LPSPI+spidev dtb an SPI link needs). Wins over
    # the LCD/camera dtb selection. None = normal.
    dtb_override: Optional[str] = None


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
    # A camera profile may need a sensor/CSI-enabled dtb to surface the V4L2
    # capture node in the guest; that override wins over the board's default dtb —
    # but ONLY when the camera is "armed" (frames are staged: rt.camera_frames_dir
    # set). With no staged frames we boot the plain board (the ISI model fatally
    # errors on an empty frames dir, and an unused camera shouldn't alter boot).
    # dtb precedence: an attached LCD panel wins (it's an explicit user action and
    # the panel dtb is built from the stock base), then an armed camera's sensor
    # dtb, else the board default. (LCD + camera together would need a merged dtb;
    # that's a future combine — for now attaching the LCD boots without the sensor.)
    cam = profile.camera
    disp = profile.display
    if rt.dtb_override:                    # a fabric link needs a specific dtb (e.g. LPUART2 enabled)
        dtb_name = rt.dtb_override
    elif disp.attach_dtb and rt.lcd_attached:
        dtb_name = disp.attach_dtb
    elif cam.enabled and cam.dtb and rt.camera_frames_dir is not None:
        dtb_name = cam.dtb
    else:
        dtb_name = art.dtb
    dtb = _resolve_artifact(dtb_name, rt.asset_dir)
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
    elif boot.mode is BootMode.firmware_elf:
        # Bare-metal/RTOS firmware on a Cortex-M MCU: QEMU loads the ELF and the
        # core boots from the Armv8-M vector table (SP@0x0, reset@0x4). No dtb, no
        # -append. The firmware artifact wins; fall back to `kernel` for flexibility.
        # (A second core or a separate image, if any, rides in qemu.extra_args as a
        # board-specific -device loader, like the i.MX95 M33 loader.)
        fw = _resolve_artifact(art.firmware or art.kernel, rt.asset_dir)
        if fw:
            args += ["-kernel", fw]

    if rootfs:
        # SD/eMMC image for boards that root off a disk rather than initramfs.
        args += ["-drive", f"file={rootfs},format=raw,if=sd"]
    return args


def build_command(profile: Profile, rt: SessionRuntime) -> list[str]:
    """Build the full QEMU argv for a session. Pure function, no side effects."""
    q = profile.qemu
    # Priority: a wizard-built per-board binary (rt.qemu_binary) > $HOLOBENCH_QEMU
    # (container's one baked qemu) > the profile's path (with ${VARS} expanded).
    binary = rt.qemu_binary or os.environ.get("HOLOBENCH_QEMU") or os.path.expandvars(q.binary)
    argv: list[str] = [binary]

    # A fabric link may need machine properties (e.g. a CAN link wires the machine's
    # can-buses to a named bus: `-machine <type>,canbus0=cb,canbus1=cb`).
    machine = q.machine + (f",{rt.machine_extra}" if rt.machine_extra else "")
    argv += ["-machine", machine]
    if q.memory:  # null -> omit -m (SoC owns its RAM; e.g. Cortex-M MCUs)
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

    # Serial consoles: one chardev per declared UART, in order. Normally a unix
    # socket the browser console bridge connects to; in external-console mode a
    # labeled PTY instead, so a plain terminal (PuTTY -serial, screen, minicom)
    # attaches to the /dev/pts/N QEMU reports for that label.
    for port in profile.serial:
        if rt.external_console:
            # logfile= captures the full stream to disk even while no terminal is
            # attached — QEMU's pty backend DROPS output when the slave has no
            # reader, so without this the early boot is lost before the user runs
            # PuTTY. PuTTY/screen still attach live to the /dev/pts for this label.
            logf = rt.work_dir / f"{port.chardev}.log"
            argv += [
                "-chardev", f"pty,id={port.chardev},logfile={logf}",
                "-serial", f"chardev:{port.chardev}",
            ]
            continue
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

    # v3.0 fabric (UART): the board's spare link-UART, appended right after the
    # declared consoles so it lands on the next serial_hd() index — a stock
    # `-chardev socket` + `-serial chardev:<id>` bridging it to the peer board.
    if rt.uart_link_override:
        argv += rt.uart_link_override

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
    if rt.nic_override is not None:
        # v3.0 fabric: the board's modeled NICs attach to these backends in order
        # (socket/mcast segment), not slirp.
        for spec in rt.nic_override:
            argv += ["-nic", spec]
    else:
        for i in range(profile.net.user_nics):
            opts = "user"
            if i == 0 and tftp.enabled and rt.share_dir is not None:
                opts += f",tftp={rt.share_dir}"
            argv += ["-nic", opts]

    # External-console SSH: a stock user-net NIC with a host->guest :22 forward on
    # a virtio-net-device. Independent of the board's own modeled ENET (which may
    # not bind a Linux netdev) — it rides the virtio-mmio bus every board here has,
    # so `ssh -p <port> root@127.0.0.1` works. All upstream QEMU (Prime Directive).
    if rt.ssh_forward_port:
        argv += [
            "-netdev", f"user,id=hbssh,hostfwd=tcp::{rt.ssh_forward_port}-:22",
            "-device", "virtio-net-device,netdev=hbssh",
        ]

    # v3.0 fabric (USB): stock usbredir inter-board link args (a `-chardev socket`
    # + `-device usb-redir` on the host, or `-global <usbdev>.chardev=` on the
    # device). Appended verbatim; the coordinator built them from the usb: profile.
    if rt.usb_override:
        argv += rt.usb_override

    # v3.0 fabric (SPI): stock `-chardev socket` + `-device spi-link` inter-board
    # SPI bridge args, appended verbatim; the coordinator built them from the spi:
    # profile block.
    if rt.spi_link_override:
        argv += rt.spi_link_override

    # v3.0 fabric (CAN): stock `-object can-bus` + `-chardev socket` + `-object
    # can-host-chardev` inter-board CAN bridge args, appended verbatim (the machine
    # props ride on -machine via machine_extra). Built from the can: profile block.
    if rt.can_link_override:
        argv += rt.can_link_override

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

    # Virtual camera: feed the per-session frames dir to the board's ISI via the
    # standard host-frame-source property. Only when ARMED — rt.camera_frames_dir
    # is set by the session manager solely when *.raw frames are staged (the ISI
    # model fatally errors on an empty frames dir). Disarmed -> none of this is
    # emitted and the board boots normally; stage frames + reboot to arm.
    cam = profile.camera
    if cam.enabled and cam.isi_type and rt.camera_frames_dir is not None:
        argv += [
            "-global",
            f"driver={cam.isi_type},property=frames,value={rt.camera_frames_dir}",
        ]
        # Sensor device model: scaffolding so the capture media graph links
        # (link_validate / STREAMON fail without a source subdev). No pixels.
        if cam.qemu_device:
            argv += ["-device", cam.qemu_device]

    argv += _boot_args(profile, rt)
    argv += [_expand_asset_dir(a, rt.asset_dir) for a in q.extra_args]
    return argv


def _expand_asset_dir(arg: str, asset_dir: Optional[Path]) -> str:
    """Expand the `{asset_dir}` placeholder in an extra_args entry against the
    session's asset dir. Lets a board reference a restricted, operator-supplied
    artifact (e.g. the i.MX95 M33 System Manager elf) by its location in the
    mounted assets volume instead of a baked absolute path — so the distributable
    image carries NO NXP BSP binaries (compliance: see docs/DEPLOY.md). With no
    asset dir the placeholder resolves to the literal string, surfacing the misuse."""
    if "{asset_dir}" not in arg:
        return arg
    return arg.replace("{asset_dir}", str(asset_dir) if asset_dir else "{asset_dir}")


def command_str(argv: list[str]) -> str:
    """A copy-pasteable, shell-quoted rendering of an argv (for logs/CLI)."""
    return " ".join(shlex.quote(a) for a in argv)

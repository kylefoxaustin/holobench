# SPDX-License-Identifier: GPL-2.0-or-later
"""Pydantic models for Holobench board profiles.

A profile is the contract between Holobench and one emulated board (see
docs/BOARD_PROFILES.md). Every board-specific fact lives here as data; no
board logic ever belongs in code. These models validate a profile on load
and are the single source of truth for its shape.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    """Base: reject unknown keys so a typo in a profile fails loudly."""

    model_config = ConfigDict(extra="forbid")


class BootMode(str, Enum):
    uboot = "uboot"
    direct_kernel = "direct-kernel"
    flash = "flash"
    # Bare-metal / RTOS firmware on a Cortex-M (Armv8-M) MCU: QEMU loads an ELF and
    # boots from the vector table (initial SP@0x0, reset@0x4) — no kernel cmdline,
    # no dtb. For MCU boards (e.g. NXP MCXN947) running Zephyr / MCUXpresso ELFs.
    firmware_elf = "firmware-elf"


# --- QEMU launch -----------------------------------------------------------


class QemuSpec(_Strict):
    binary: str = "qemu-system-aarch64"
    machine: str  # the -M value. CONFIRM with the emulator repo.
    memory: str = "1G"
    smp: Optional[int | str] = None
    # Emitted as `-audio driver=<audio>`. Default "none" so QEMU never grabs the
    # host audio backend (the i.MX models beep otherwise). Set null to omit.
    audio: Optional[str] = "none"
    extra_args: list[str] = Field(default_factory=list)


# --- Boot artifacts --------------------------------------------------------


class BootArtifacts(_Strict):
    flash_bin: Optional[str] = None
    kernel: Optional[str] = None
    # Cortex-M firmware ELF for firmware-elf boot (Zephyr / MCUXpresso). Falls back
    # to `kernel` if unset, so either field works.
    firmware: Optional[str] = None
    dtb: Optional[str] = None
    initrd: Optional[str] = None
    rootfs: Optional[str] = None
    # Golden data/system disk for image-swap: Holobench attaches a per-session
    # qcow2 OVERLAY over it (writes isolated); "reinstall" drops the overlay to
    # restore golden. Resolved from the asset dir like the other artifacts.
    disk: Optional[str] = None


class BootSpec(_Strict):
    mode: BootMode = BootMode.direct_kernel
    artifacts: BootArtifacts = Field(default_factory=BootArtifacts)
    # Kernel command line for direct-kernel boot (-append), e.g.
    # "console=ttyLP0,115200 cpuidle.off=1 rdinit=/init".
    append: Optional[str] = None
    # Escape hatch; tokens: {flash_bin} {kernel} {dtb} {initrd} {rootfs} {session}
    command_template: Optional[str] = None


# --- Serial / consoles -----------------------------------------------------


class SerialPort(_Strict):
    name: str
    chardev: str
    role: str = "a-core"  # "a-core" | "m-core" | "debug"
    default: bool = False


# --- Display ---------------------------------------------------------------


class DisplaySpec(_Strict):
    enabled: bool = False
    device: Optional[str] = None
    vnc: bool = False
    # Optional: a dtb that attaches a panel so the DPU has a connector/mode and
    # actually scans out. The stock board is faithfully panel-less (DRM "Cannot
    # find any crtc or sizes"); when set, the UI offers an "Attach LCD" control
    # that reboots the board with this dtb. Generated from the board's base dtb by
    # the emulator's own panel-attach script — still a standard `-dtb` swap.
    attach_dtb: Optional[str] = None
    attach_label: Optional[str] = None  # human label, e.g. "1280×800 LVDS"


# --- Networking ------------------------------------------------------------


class NetSpec(_Strict):
    # Number of QEMU user-mode NICs to attach (slirp). The board's modeled NICs
    # auto-attach to these in order (e.g. 91 = FEC + ENET_QoS -> 2).
    user_nics: int = 1


# --- File injection --------------------------------------------------------


class NinePShare(_Strict):
    enabled: bool = False
    mount_tag: str = "holobench"
    # virtio transport: "virtio-9p-device" (virtio-mmio, works on 91/93) or
    # "virtio-9p-pci" (boards with an enumerable PCI bus).
    device: str = "virtio-9p-device"


class ToggleOnly(_Strict):
    enabled: bool = False


class ImageSwap(_Strict):
    enabled: bool = False
    target_drive: Optional[str] = None


class FileInjection(_Strict):
    nine_p: NinePShare = Field(default_factory=NinePShare)
    tftp: ToggleOnly = Field(default_factory=ToggleOnly)
    nfs: ToggleOnly = Field(default_factory=ToggleOnly)
    image_swap: ImageSwap = Field(default_factory=ImageSwap)


# --- Virtual camera --------------------------------------------------------


class CameraSpec(_Strict):
    """Feed host image frames through the board's ISI capture pipeline so the
    guest captures them via V4L2 (/dev/video0) instead of a real sensor.

    Holobench drives the emulator's standard ISI host-frame-source property:
    ``-global driver=<isi_type>,property=frames,value=<session frames dir>``.
    No model change — this is the stock property the imx9x ISI models expose.

    Every value here is a board fact (CONFIRM with the emulator repo, never
    guess): the QOM ``isi_type``, the frame ``width``/``height``/
    ``bytes_per_pixel`` the model expects (a frame whose size != W*H*bpp falls
    back to the model's gradient test pattern), and optionally a camera-enabled
    ``dtb`` whose sensor/CSI node surfaces the V4L2 node in the guest.
    """

    enabled: bool = False
    isi_type: Optional[str] = None       # -global driver=<type>, e.g. "imx95.isi"
    width: Optional[int] = None
    height: Optional[int] = None
    bytes_per_pixel: Optional[int] = None
    pixel_format: Optional[str] = None   # informational, e.g. "RGB888" (host convert)
    dtb: Optional[str] = None            # optional camera dtb override (sensor/CSI)
    # Sensor device model that must be present so the capture media graph links
    # (it contributes no pixels — pure scaffolding). Emitted verbatim as
    # `-device <qemu_device>`, e.g. "ov5640,bus=lpi2c1,address=0x3c". CONFIRM
    # the bus/address with the emulator repo.
    qemu_device: Optional[str] = None
    # frames read at device init vs re-globbed each frame tick. If False, staged
    # frames apply only at (re)launch; the UI says "reboot to apply".
    runtime_settable: bool = False
    # Board-specific in-guest capture recipe shown in the Camera panel. The
    # imx8-isi media links start DISABLED, so a bare `v4l2-ctl --stream-mmap`
    # fails link_validate (EPIPE) until media-ctl enables the links + sets the
    # pad formats to match this geometry. Ship the exact, emulator-validated
    # command block here (entity/pad names are board facts).
    capture_hint: Optional[str] = None
    # Static aarch64 capture helper (GPL-2.0, Kyle-authored, vendored from the
    # emulator repos) staged into the session 9p share so the guest runs it from
    # /mnt. Filename under vendor/camera/bin/, e.g. "imx95-isi-capture". A
    # standalone tool shipped alongside Holobench (not linked) — license unaffected.
    capture_binary: Optional[str] = None
    # Guest kernel modules (sensor drivers) the rootfs doesn't ship, staged into
    # the 9p share so the guest `insmod /mnt/<name>` to bind the sensor (without
    # it the media graph never forms / no /dev/media0). Asset-relative .ko names,
    # resolved like the dtb; MUST match the booted Image's vermagic (so symlink
    # them from the same kernel tree that builds the Image).
    guest_modules: list[str] = Field(default_factory=list)

    @property
    def frame_bytes(self) -> Optional[int]:
        if self.width and self.height and self.bytes_per_pixel:
            return self.width * self.height * self.bytes_per_pixel
        return None


# --- Power -----------------------------------------------------------------


class PowerSpec(_Strict):
    warm_reset: bool = True
    cold_cycle: bool = True
    pause: bool = True
    reinstall: bool = False


# --- Introspection ---------------------------------------------------------


class GdbStub(_Strict):
    enabled: bool = False


class Introspection(_Strict):
    qmp_events: bool = True
    memory_map: bool = True
    device_tree: bool = True
    gdbstub: GdbStub = Field(default_factory=GdbStub)
    snapshots: bool = False


# --- Reservation -----------------------------------------------------------


class Reservation(_Strict):
    default_minutes: int = 60
    max_minutes: int = 240


# --- Top level -------------------------------------------------------------


class Profile(_Strict):
    id: str
    display_name: str
    soc: str
    description: Optional[str] = None

    qemu: QemuSpec
    boot: BootSpec = Field(default_factory=BootSpec)
    serial: list[SerialPort] = Field(default_factory=list)
    display: DisplaySpec = Field(default_factory=DisplaySpec)
    net: NetSpec = Field(default_factory=NetSpec)
    file_injection: FileInjection = Field(default_factory=FileInjection)
    camera: CameraSpec = Field(default_factory=CameraSpec)
    power: PowerSpec = Field(default_factory=PowerSpec)
    introspection: Introspection = Field(default_factory=Introspection)
    reservation: Reservation = Field(default_factory=Reservation)

    @property
    def default_serial(self) -> Optional[SerialPort]:
        """The serial port to focus first; falls back to the first declared."""
        for port in self.serial:
            if port.default:
                return port
        return self.serial[0] if self.serial else None

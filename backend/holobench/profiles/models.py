# SPDX-License-Identifier: Apache-2.0
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

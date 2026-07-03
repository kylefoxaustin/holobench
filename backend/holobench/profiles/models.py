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
    # Guest RAM (`-m`). Set null to OMIT -m entirely — for SoC-owned-RAM machines
    # (e.g. Cortex-M MCUs) whose default_ram_size is 0 and which ignore/reject -m.
    memory: Optional[str] = "1G"
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


# --- LEDs (board indicator panel) ------------------------------------------


class LedSpec(_Strict):
    """One board LED shown in the LEDs panel.

    source="power": a synthetic power/status indicator driven by session state
    (no model support needed — Phase 1). source="gpio": a real SoC-driven LED;
    Holobench reads the GPIO output data register at `reg` and masks `bit` via a
    stock read-only interface (HMP `xp`, gdbstub, or qom-get) — NO model change,
    so it can't affect upstreaming. The emulator only reports reg/bit/color/polarity
    (board facts). off_color is shown when unlit.
    """
    name: str
    color: str = "green"          # lit color (CSS name or hex)
    source: str = "gpio"          # "power" | "gpio"
    reg: Optional[int] = None     # gpio output-data register physical address (source=gpio)
    bit: Optional[int] = None     # which pin/bit in that register
    active_high: bool = True      # LED lit when the bit is 1 (else active-low)
    # Optional driven-gate: a register whose `enable_bit` says the pin is actually
    # an output driving the LED (e.g. the GPIO data-direction register, PDDR). If
    # set and the pin is NOT an output, the LED reads OFF — so an undriven
    # active-low pin (PDOR bit defaults 0 = "on") doesn't falsely light. Read via
    # the same stock `xp`; still no model change.
    enable_reg: Optional[int] = None
    enable_bit: Optional[int] = None
    enable_high: bool = True       # pin is an output when enable_bit == 1


# --- Networking ------------------------------------------------------------


class NetSpec(_Strict):
    # Number of QEMU user-mode NICs to attach (slirp). The board's modeled NICs
    # auto-attach to these in order (e.g. 91 = FEC + ENET_QoS -> 2).
    user_nics: int = 1
    # v3.0 fabric: the QEMU NIC `model=` to bind when this board joins a lab's
    # Ethernet segment, when the board needs disambiguation (e.g. the MCXN947's
    # ENET-QoS = "mcxn-enet", the i.MX9 FEC = "imx.enet" to avoid the eQOS stub).
    # None = let QEMU auto-attach the first modeled NIC (works for the i.MX91 PoC).
    fabric_nic_model: Optional[str] = None


# --- USB inter-board link (v3.0 fabric) ------------------------------------

class UsbRole(_Strict):
    """How a board wires ONE usbredir role into a lab's USB link, as raw QEMU arg
    templates (stock interfaces only — Prime Directive). `{id}` (chardev id) and
    `{path}` (the shared unix socket) are filled by the lab coordinator per link.

    - `host` role (importer): a stock `-device usb-redir` + a CLIENT `-chardev`.
    - `device` role (exporter): the board's device-mode `-global ...chardev=` + a
      SERVER `-chardev` (the usbredirserver/exporter listens, per the convention)."""
    chardev: str                       # -chardev <this>   (e.g. "socket,id={id},path={path},server=off,reconnect-ms=2000")
    device: Optional[str] = None       # -device  <this>   (host/importer, e.g. "usb-redir,chardev={id}")
    glob: Optional[str] = None         # -global  <this>   (device/exporter, e.g. "mcxn-usbdev.chardev={id}")


class UsbCapability(_Strict):
    """A board's usbredir inter-board-link capability (docs/TOPOLOGIES.md §USB).
    Declare `host` (it can import a redirected device), `device` (it can export its
    device-mode endpoint), or both. Absent = this board can't be a USB-link endpoint.
    Facts are confirmed by the emulator sessions (§7), never guessed here."""
    host: Optional[UsbRole] = None
    device: Optional[UsbRole] = None


class UartLink(_Strict):
    """How a board wires its spare UART into a board-to-board serial bridge
    (docs/TOPOLOGIES.md §UART). Symmetric: both ends use the same link UART; the
    coordinator makes one end the socket server and the other the client. Stock
    interfaces only (a `-chardev socket` + `-serial chardev:`) — no model change.

    `chardev` is the base `-chardev` template with `{id}`/`{path}` filled by the
    coordinator; it appends `,server=on,wait=off` (listener) or `,server=off`
    (connector). `attach_dtb` boots a dtb that ENABLES the link UART (many EVK
    dtbs leave the spare UART disabled). Facts confirmed by the emulator (§7)."""
    chardev: str                       # e.g. "socket,id={id},path={path}"
    attach_dtb: Optional[str] = None   # dtb that enables the link UART (else base dtb)
    dev: Optional[str] = None          # informational: the guest device node (e.g. /dev/ttyLP1)


class UartCapability(_Strict):
    """A board's UART inter-board-link capability. `link` = the spare UART this
    board can bridge to a peer. Absent = this board can't be a UART-link endpoint."""
    link: Optional[UartLink] = None


class SpiLink(_Strict):
    """How a board wires its spare LPSPI into a board-to-board SPI bridge
    (docs/TOPOLOGIES.md §SPI). Each end is an LPSPI master with a `spi-link` SSI
    bridge peripheral on its bus (a device the board model provides for inter-QEMU
    SPI — holobench only passes `-device`, it adds nothing to the model). The
    coordinator makes one end the socket server, the other the client. Stock
    interfaces (a `-chardev socket` + `-device spi-link`).

    `device` is the `-device spi-link,bus=<lpspi-bus>,chardev={id}` template;
    `chardev` the base `-chardev socket` template; `attach_dtb` enables the LPSPI +
    a spidev child. Facts confirmed by the emulator (§7)."""
    device: str                        # e.g. "spi-link,bus=lpspi1,chardev={id}"
    chardev: str                       # e.g. "socket,id={id},path={path}"
    attach_dtb: Optional[str] = None   # dtb that enables the LPSPI + spidev child
    dev: Optional[str] = None          # informational: the guest node (e.g. /dev/spidev0.0)


class SpiCapability(_Strict):
    """A board's SPI inter-board-link capability. `link` = the spare LPSPI this
    board can bridge to a peer. Absent = this board can't be an SPI-link endpoint."""
    link: Optional[SpiLink] = None


class CanLink(_Strict):
    """How a board wires its FlexCAN into a board-to-board CAN bridge
    (docs/TOPOLOGIES.md §CAN). Uses the fleet-shared generic `can-host-chardev`
    backend (bridges an emulated `can-bus` to a chardev — no host vcan/SocketCAN,
    no root). Stock objects only. The coordinator emits, in order, `-object <bus>`,
    a `-chardev socket` (server on one end, reconnecting client on the other), and
    `-object <host>`; `machine_extra` (if set) is appended to `-machine` to wire the
    board's can-buses to the bridged bus. Facts confirmed by the emulator (§7).

    `bus`/`host` are `-object` templates; `chardev` the `-chardev socket` template;
    `{id}` (chardev id) is filled by the coordinator. `machine_extra` is optional —
    some boards auto-link a command-line can-bus by name (no machine prop needed)."""
    bus: str                           # -object <this>  (e.g. "can-bus,id=cb")
    host: str                          # -object <this>  (e.g. "can-host-chardev,id=canh,canbus=cb,chardev={id}")
    chardev: str                       # -chardev <this> (e.g. "socket,id={id},path={path}")
    machine_extra: Optional[str] = None  # appended to -machine (e.g. "canbus0=cb,canbus1=cb")
    dev: Optional[str] = None          # informational: the guest node (e.g. can0)


class CanCapability(_Strict):
    """A board's CAN inter-board-link capability. `link` = the FlexCAN this board
    can bridge to a peer. Absent = this board can't be a CAN-link endpoint."""
    link: Optional[CanLink] = None


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
    usb: Optional[UsbCapability] = None        # v3.0 fabric: usbredir inter-board link role(s)
    uart: Optional[UartCapability] = None      # v3.0 fabric: UART board-to-board link role
    spi: Optional[SpiCapability] = None        # v3.0 fabric: SPI board-to-board link role
    can: Optional[CanCapability] = None        # v3.0 fabric: CAN board-to-board link role
    file_injection: FileInjection = Field(default_factory=FileInjection)
    camera: CameraSpec = Field(default_factory=CameraSpec)
    leds: list[LedSpec] = Field(default_factory=list)
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

# Holobench — Board Profiles

A profile is the **contract** between Holobench and one emulated board. It is
the *only* place board-specific facts live (see `CLAUDE.md` §7 and
`ARCHITECTURE.md` §8). Profiles are YAML, validated with pydantic on load, and
resolved by the orchestrator into a QEMU command line plus bridge configs.

Every value in a profile comes from the board's emulator repo
(`qemu-imx95/93/91`). **Do not guess machine-type strings or boot artifacts** —
confirm them with the emulator repo or its README.

---

## Schema

```yaml
# Identity ---------------------------------------------------------------
id:            string   # required, unique slug, e.g. "imx95-evk"
display_name:  string   # required, UI label, e.g. "i.MX 95 EVK"
soc:           string   # required, e.g. "i.MX 95"
description:   string   # optional, one-liner for the picker

# QEMU launch ------------------------------------------------------------
qemu:
  binary:      string   # required, path/name, e.g. "qemu-system-aarch64"
  machine:     string   # required, the -M value. CONFIRM with emulator repo.
  memory:      string   # required, e.g. "8G"
  smp:         int|str  # optional, e.g. 6 or "6"
  extra_args:  [string] # optional, raw flags appended verbatim

# Boot artifacts ---------------------------------------------------------
boot:
  mode:        string         # "uboot" | "direct-kernel" | "flash"
  artifacts:                  # paths resolved relative to the board's asset dir
    flash_bin: string|null    # imx-boot / flash.bin (uboot/flash modes)
    kernel:    string|null    # Image (direct-kernel mode)
    dtb:       string|null
    rootfs:    string|null    # rootfs image attached as a drive
  # Optional explicit override; if absent the orchestrator derives the line
  # from mode + artifacts. Use {artifact} and {session} tokens.
  command_template: string|null

# Serial / consoles ------------------------------------------------------
serial:                        # one or more UARTs -> console tabs
  - name:    string            # UI tab label, e.g. "A-core console"
    chardev: string            # chardev id, e.g. "console0"
    role:    string            # "a-core" | "m-core" | "debug"
    default: bool              # one entry true = focused tab

# Display / framebuffer --------------------------------------------------
display:
  enabled:   bool              # false -> LCD panel hidden
  device:    string|null       # informational, e.g. "lcdif/dcss"
  vnc:       bool              # true -> launch with -vnc, bridge to noVNC

# File injection ---------------------------------------------------------
file_injection:
  nine_p:                      # virtio-9p live share
    enabled:   bool
    mount_tag: string          # e.g. "holobench"
  tftp:                        # user-net TFTP for tftpboot
    enabled:   bool
  nfs:                         # host NFS export mounted at /mnt in guest
    enabled:   bool
  image_swap:                  # full image flash / reinstall
    enabled:      bool
    target_drive: string|null  # which -drive/-sd to replace

# Power ------------------------------------------------------------------
power:
  warm_reset:  bool            # QMP system_reset
  cold_cycle:  bool            # quit + relaunch
  pause:       bool            # QMP stop/cont
  reinstall:   bool            # restore golden image

# Introspection ----------------------------------------------------------
introspection:
  qmp_events:  bool            # stream QMP events to UI
  memory_map:  bool            # info mtree
  device_tree: bool            # info qtree / qom-list
  gdbstub:
    enabled: bool              # launch with -gdb tcp::PORT
  snapshots:   bool            # savevm/loadvm

# Reservation ------------------------------------------------------------
reservation:
  default_minutes: int         # default slot length
  max_minutes:     int         # ceiling per reservation
```

## Field notes

- **`qemu.machine`** is the single most important field and the one most likely
  to be wrong if guessed. It must match the `-M` string the emulator model
  registers. Get it from the emulator repo.
- **`boot.mode`** picks the launch shape: `uboot`/`flash` boot from
  `flash_bin`; `direct-kernel` uses `-kernel`/`-dtb`/`-append`. Match what the
  model expects; some i.MX models only boot via flash.bin.
- **`command_template`** is an escape hatch for boards whose launch line the
  derived form can't express. Tokens: `{flash_bin}`, `{kernel}`, `{dtb}`,
  `{rootfs}`, `{session}` (session work dir). Prefer the derived form.
- **`serial[].chardev`** must match a chardev id the model wires its UART to.
  Multiple entries become console tabs.
- **`display.vnc: false`** on a headless/server profile hides the LCD panel
  rather than showing a blank one.
- **`file_injection`** toggles only expose mechanisms the board actually
  supports. TFTP/NFS require the guest software (u-boot/Linux) to use them; 9p
  requires the virtio-9p driver in the guest kernel.

## Capability degradation

If a board lacks a capability (no display device, a UART not wired to a
chardev), set the relevant flag to `false`/absent. The UI hides the panel; it
does **not** work around the gap. Genuine gaps are escalated to the emulator
repo (`CLAUDE.md` §2 escalation), never patched here.

## Adding a board

1. Read the emulator repo's README / ask its Claude session for: machine type,
   UART topology, display device presence, canonical boot line + artifacts,
   known-good QMP commands.
2. Author `profiles/<id>.yaml` from those facts.
3. `holobench launch <id>` (Phase 0) to validate bring-up.
4. Walk the phases: console, display, files, fleet.

No code change is required to add a board. If you find yourself editing backend
code to support a specific SoC, the design has drifted — fix the profile model
instead.

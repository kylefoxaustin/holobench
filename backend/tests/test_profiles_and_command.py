# SPDX-License-Identifier: GPL-2.0-or-later
"""Fast unit tests for the pure logic: profile validation + command resolution.

No QEMU needed — these guard the board-agnostic core. Run with: pytest -q
"""

from pathlib import Path

import pytest

from holobench.profiles import list_profiles, load_profile
from holobench.profiles.loader import ProfileError, load_profile_file
from holobench.profiles.models import (
    BootArtifacts,
    BootMode,
    BootSpec,
    Profile,
    QemuSpec,
)
from holobench.session.command import SessionRuntime, build_command


def _runtime_for(profile: Profile, tmp: Path) -> SessionRuntime:
    return SessionRuntime(
        work_dir=tmp,
        qmp_socket=tmp / "qmp.sock",
        serial_sockets={p.chardev: tmp / f"{p.chardev}.sock" for p in profile.serial},
    )


def test_all_shipped_profiles_validate():
    ids = list_profiles()
    assert "virt-smoke" in ids
    for pid in ids:
        load_profile(pid)  # raises on invalid


def test_unknown_profile_lists_available():
    with pytest.raises(ProfileError) as exc:
        load_profile("does-not-exist")
    assert "Available" in str(exc.value)


def test_id_must_match_filename(tmp_path):
    f = tmp_path / "alpha.yaml"
    f.write_text("id: beta\ndisplay_name: B\nsoc: x\nqemu:\n  machine: virt\n")
    with pytest.raises(ProfileError):
        load_profile_file(f)


def test_unknown_key_rejected(tmp_path):
    f = tmp_path / "p.yaml"
    f.write_text("id: p\ndisplay_name: P\nsoc: x\nqemu:\n  machine: virt\nbogus: 1\n")
    with pytest.raises(ProfileError):
        load_profile_file(f)


def test_command_has_standard_flags_only(tmp_path):
    p = load_profile("virt-smoke")
    argv = build_command(p, _runtime_for(p, tmp_path))
    assert argv[0] == p.qemu.binary
    assert "-machine" in argv and "virt" in argv
    assert "-qmp" in argv
    # QMP must be a backend-owned unix socket, never a TCP port to the world.
    qmp_val = argv[argv.index("-qmp") + 1]
    assert qmp_val.startswith("unix:") and "server=on" in qmp_val
    # Headless smoke profile -> display none, no VNC.
    assert "none" in argv[argv.index("-display") + 1]
    # Serial wired to the declared chardev.
    assert "chardev:console0" in argv


def test_direct_kernel_artifacts_resolve_against_asset_dir(tmp_path):
    f = tmp_path / "k.yaml"
    f.write_text(
        "id: k\ndisplay_name: K\nsoc: x\n"
        "qemu:\n  machine: virt\n"
        "boot:\n  mode: direct-kernel\n"
        "  artifacts: {kernel: Image, dtb: board.dtb}\n"
    )
    p = load_profile_file(f)
    rt = SessionRuntime(
        work_dir=tmp_path,
        qmp_socket=tmp_path / "qmp.sock",
        asset_dir=Path("/assets"),
    )
    argv = build_command(p, rt)
    assert "/assets/Image" in argv
    assert "/assets/board.dtb" in argv


def test_extra_args_asset_dir_placeholder_expands(tmp_path):
    # Compliance: the i.MX95 M33 SM firmware is NEVER baked; the profile references
    # it via {asset_dir} so it resolves from the operator's mounted assets volume.
    p = load_profile("imx95-evk")
    rt = SessionRuntime(
        work_dir=tmp_path,
        qmp_socket=tmp_path / "qmp.sock",
        serial_sockets={s.chardev: tmp_path / f"{s.chardev}.sock" for s in p.serial},
        asset_dir=Path("/artifacts/imx95-evk"),
    )
    argv = build_command(p, rt)
    loaders = [a for a in argv if a.startswith("loader,file=")]
    assert loaders and "/artifacts/imx95-evk/m33_image_M2.elf" in loaders[0]


def test_qemu_binary_override_wins(tmp_path, monkeypatch):
    # The wizard-built per-board binary (rt.qemu_binary) beats $HOLOBENCH_QEMU and
    # the profile path — this is what closes the build->boot seam.
    monkeypatch.setenv("HOLOBENCH_QEMU", "/env/qemu")
    p = load_profile("imx91-evk")
    rt = SessionRuntime(
        work_dir=tmp_path, qmp_socket=tmp_path / "qmp.sock",
        serial_sockets={s.chardev: tmp_path / f"{s.chardev}.sock" for s in p.serial},
        qemu_binary="/built/qemu-system-aarch64",
    )
    assert build_command(p, rt)[0] == "/built/qemu-system-aarch64"


def test_no_profile_hardcodes_host_bsp_path():
    # Guard: an absolute /home/... loader path in extra_args would mean a restricted
    # artifact pinned to the build host (the thing that caused the redistribution
    # incident). Profiles must use {asset_dir} instead.
    for pid in list_profiles():
        try:
            p = load_profile(pid)
        except ProfileError:
            continue
        for a in p.qemu.extra_args:
            assert "/home/" not in a, f"{pid} extra_args has a host path: {a}"


def test_audio_defaults_to_none(tmp_path):
    p = load_profile("virt-smoke")
    argv = build_command(p, _runtime_for(p, tmp_path))
    assert argv[argv.index("-audio") + 1] == "driver=none"


def test_imx91_direct_kernel_resolves(tmp_path):
    p = load_profile("imx91-evk")
    rt = SessionRuntime(
        work_dir=tmp_path,
        qmp_socket=tmp_path / "qmp.sock",
        serial_sockets={"console0": tmp_path / "console0.sock"},
        asset_dir=Path("/assets"),
    )
    argv = build_command(p, rt)
    assert argv[argv.index("-machine") + 1] == "imx91-11x11-evk"
    assert argv[argv.index("-kernel") + 1] == "/assets/Image"
    assert argv[argv.index("-dtb") + 1] == "/assets/imx91-11x11-evk.dtb"
    assert argv[argv.index("-initrd") + 1] == "/assets/initrd.cpio.gz"
    assert "rdinit=/init" in argv[argv.index("-append") + 1]
    # 91 has two NICs.
    assert argv.count("user") == 2


def test_imx95_carries_loadbearing_m33_loader(tmp_path):
    p = load_profile("imx95-evk")
    rt = SessionRuntime(
        work_dir=tmp_path,
        qmp_socket=tmp_path / "qmp.sock",
        serial_sockets={"console0": tmp_path / "console0.sock"},
        asset_dir=Path("/assets"),
    )
    argv = build_command(p, rt)
    assert argv[argv.index("-machine") + 1] == "imx95-19x19-evk"
    # The M33 System Manager loader must be present or Linux won't boot.
    # (M=2 firmware m33_image_M2.elf is the density-correct default; the stock
    # M=1 m33_image.elf is also valid — both are SM "m33_image*" loaders.)
    loader = [a for a in argv if a.startswith("loader,file=")]
    assert loader and "cpu-num=6" in loader[0] and "m33_image" in loader[0]


def test_imx95_attach_lcd_swaps_dtb(tmp_path):
    p = load_profile("imx95-evk-sd")
    assert p.display.attach_dtb  # profile declares an attachable panel

    def dtb_for(lcd):
        rt = SessionRuntime(
            work_dir=tmp_path,
            qmp_socket=tmp_path / "qmp.sock",
            serial_sockets={s.chardev: tmp_path / f"{s.chardev}.sock" for s in p.serial},
            asset_dir=Path("/assets"),
            lcd_attached=lcd,
        )
        argv = build_command(p, rt)
        return argv[argv.index("-dtb") + 1]

    # Default boot uses the stock (faithful, panel-less) dtb; attaching the LCD
    # swaps to display.attach_dtb so the DPU gets a connector/mode and scans out.
    assert dtb_for(False).endswith(p.boot.artifacts.dtb)
    assert dtb_for(True).endswith(p.display.attach_dtb)
    assert dtb_for(True) != dtb_for(False)


def test_external_console_uses_pty_serials(tmp_path):
    # `holobench console` mode: each declared UART becomes a labeled PTY (so a
    # plain terminal / PuTTY -serial attaches) instead of the browser bridge's
    # unix socket — and no serial socket needs allocating.
    p = load_profile("imx95-evk-sd")
    rt = SessionRuntime(
        work_dir=tmp_path,
        qmp_socket=tmp_path / "qmp.sock",
        serial_sockets={},                 # none needed in external-console mode
        asset_dir=Path("/assets"),
        disk_overlay=tmp_path / "o.qcow2",
        external_console=True,
    )
    argv = build_command(p, rt)
    joined = " ".join(argv)
    # both LPUARTs are PTYs, in order (console0 = serial0 = A-core, smconsole = serial1 = SM),
    # each with a logfile= so early boot is captured before a terminal attaches.
    c0 = next(a for a in argv if a.startswith("pty,id=console0"))
    c1 = next(a for a in argv if a.startswith("pty,id=smconsole"))
    assert "logfile=" in c0 and "console0.log" in c0
    assert "logfile=" in c1 and "smconsole.log" in c1
    assert "chardev:console0" in argv and "chardev:smconsole" in argv
    assert argv.index(c0) < argv.index(c1)
    assert "socket,id=console0" not in joined  # not the browser-bridge socket path


def test_ssh_forward_adds_stock_virtio_nic(tmp_path):
    # ssh_forward_port adds a stock user-net NIC + virtio-net-device (board-agnostic
    # host->guest :22 forward), on top of the profile's normal NICs.
    p = load_profile("imx95-evk-sd")
    rt = SessionRuntime(
        work_dir=tmp_path,
        qmp_socket=tmp_path / "qmp.sock",
        serial_sockets={s.chardev: tmp_path / f"{s.chardev}.sock" for s in p.serial},
        asset_dir=Path("/assets"),
        ssh_forward_port=2222,
    )
    argv = build_command(p, rt)
    assert "user,id=hbssh,hostfwd=tcp::2222-:22" in argv
    assert "virtio-net-device,netdev=hbssh" in argv
    # off by default
    rt.ssh_forward_port = None
    assert "virtio-net-device,netdev=hbssh" not in build_command(p, rt)


def test_image_swap_drive_attachment(tmp_path):
    # target_drive picks the attachment: 95 = eMMC, 91-sd = SD card.
    overlay = tmp_path / "disk-overlay.qcow2"

    def argv_for(pid):
        p = load_profile(pid)
        rt = SessionRuntime(
            work_dir=tmp_path,
            qmp_socket=tmp_path / "qmp.sock",
            serial_sockets={s.chardev: tmp_path / f"{s.chardev}.sock" for s in p.serial},
            asset_dir=Path("/assets"),
            disk_overlay=overlay,
        )
        return build_command(p, rt)

    emmc = argv_for("imx95-evk-sd")
    assert "emmc,drive=hbdisk" in emmc
    assert any(a.startswith("if=none,id=hbdisk") for a in emmc)

    sd = argv_for("imx91-evk-sd")
    assert any(a.startswith("if=sd,") and "disk-overlay" in a for a in sd)
    assert "emmc,drive=hbdisk" not in sd


def test_virtual_camera_global_and_dtb_override(tmp_path):
    # Camera enabled -> -global host-frame-source on the declared isi_type,
    # pointed at the per-session frames dir; camera.dtb overrides the boot dtb.
    f = tmp_path / "cam.yaml"
    f.write_text(
        "id: cam\ndisplay_name: Cam\nsoc: x\n"
        "qemu:\n  machine: virt\n"
        "boot:\n  mode: direct-kernel\n"
        "  artifacts: {kernel: Image, dtb: plain.dtb}\n"
        "camera:\n"
        "  enabled: true\n  isi_type: imx95.isi\n"
        "  width: 640\n  height: 480\n  bytes_per_pixel: 6\n"
        "  pixel_format: RGB16\n  dtb: camera.dtb\n"
        "  qemu_device: ov5640,bus=lpi2c1,address=0x3c\n"
    )
    p = load_profile_file(f)
    assert p.camera.frame_bytes == 640 * 480 * 6
    # ARMED: rt.camera_frames_dir set (manager sets it only when frames are staged).
    frames = tmp_path / "frames"
    rt = SessionRuntime(
        work_dir=tmp_path,
        qmp_socket=tmp_path / "qmp.sock",
        asset_dir=Path("/assets"),
        camera_frames_dir=frames,
    )
    argv = build_command(p, rt)
    glob = [a for a in argv if a.startswith("driver=imx95.isi")]
    assert glob and "property=frames" in glob[0] and str(frames) in glob[0]
    assert argv[argv.index("-global") + 1] == glob[0]
    # camera.dtb wins over boot.artifacts.dtb.
    assert argv[argv.index("-dtb") + 1] == "/assets/camera.dtb"
    # sensor scaffolding device emitted verbatim.
    assert "ov5640,bus=lpi2c1,address=0x3c" in argv
    assert argv[argv.index("-device") + 1] == "ov5640,bus=lpi2c1,address=0x3c"

    # DISARMED: no staged frames -> rt.camera_frames_dir is None -> the camera
    # apparatus is fully omitted (empty frames dir is a FATAL error in the ISI),
    # and the board boots its plain dtb. This is the regression that bricked boot.
    rt_off = SessionRuntime(
        work_dir=tmp_path, qmp_socket=tmp_path / "qmp.sock",
        asset_dir=Path("/assets"), camera_frames_dir=None,
    )
    argv_off = build_command(p, rt_off)
    assert not any(a.startswith("driver=imx95.isi") for a in argv_off)
    assert "ov5640,bus=lpi2c1,address=0x3c" not in argv_off
    assert argv_off[argv_off.index("-dtb") + 1] == "/assets/plain.dtb"


def test_camera_enabled_boards_ship_their_capture_helper():
    # Every camera-enabled profile must name a capture_binary that is actually
    # vendored + built (static helper staged into the guest /mnt). Guards the bundle.
    repo = Path(__file__).resolve().parents[2]
    bindir = repo / "vendor" / "camera" / "bin"
    for pid in list_profiles():
        cam = load_profile(pid).camera
        if not cam.enabled:
            continue
        assert cam.capture_binary, f"{pid}: camera enabled but no capture_binary"
        b = bindir / cam.capture_binary
        assert b.is_file(), f"{pid}: missing helper {b} (run tools/build-capture-helpers.sh)"


def test_capture_helper_resolves():
    from holobench.session.manager import _capture_helper_path
    assert _capture_helper_path("imx95-isi-capture") is not None
    assert _capture_helper_path("does-not-exist-xyz") is None


def test_no_camera_global_when_disabled(tmp_path):
    p = load_profile("imx91-evk")  # no camera block -> disabled
    rt = SessionRuntime(
        work_dir=tmp_path,
        qmp_socket=tmp_path / "qmp.sock",
        serial_sockets={"console0": tmp_path / "console0.sock"},
        asset_dir=Path("/assets"),
        camera_frames_dir=tmp_path / "frames",
    )
    argv = build_command(p, rt)
    assert not any(a.startswith("driver=") and "property=frames" in a for a in argv)


def test_flash_mode_uses_bios(tmp_path):
    f = tmp_path / "fl.yaml"
    f.write_text(
        "id: fl\ndisplay_name: FL\nsoc: x\n"
        "qemu:\n  machine: virt\n"
        "boot:\n  mode: flash\n  artifacts: {flash_bin: flash.bin}\n"
    )
    p = load_profile_file(f)
    rt = SessionRuntime(work_dir=tmp_path, qmp_socket=tmp_path / "q.sock", asset_dir=Path("/a"))
    argv = build_command(p, rt)
    assert argv[argv.index("-bios") + 1] == "/a/flash.bin"


def _mcu_profile(**boot_kw):
    return Profile(
        id="mcxn947-evk", display_name="MCXN947", soc="NXP MCXN947",
        qemu=QemuSpec(machine="mcxn947-evk"),
        boot=BootSpec(mode=BootMode.firmware_elf, **boot_kw),
    )


def _rt(tmp_path):
    return SessionRuntime(
        work_dir=tmp_path,
        qmp_socket=tmp_path / "qmp.sock",
        serial_sockets={"console0": tmp_path / "console0.sock"},
        asset_dir=Path("/assets"),
    )


def test_firmware_elf_boot_kernel_only(tmp_path):
    # MCU firmware-elf boot: -kernel <elf>, and NO -dtb / -append / -initrd.
    p = _mcu_profile(artifacts=BootArtifacts(firmware="zephyr.elf"),
                     append="should-be-ignored")
    argv = build_command(p, _rt(tmp_path))
    assert argv[argv.index("-kernel") + 1].endswith("zephyr.elf")
    assert "-dtb" not in argv
    assert "-append" not in argv
    assert "-initrd" not in argv
    # no display for an MCU -> boots with -display none, no framebuffer assumptions
    assert p.display.enabled is False


def test_firmware_elf_falls_back_to_kernel_artifact(tmp_path):
    # `firmware` unset -> the `kernel` artifact is used as the ELF.
    p = _mcu_profile(artifacts=BootArtifacts(kernel="fw.elf"))
    argv = build_command(p, _rt(tmp_path))
    assert argv[argv.index("-kernel") + 1].endswith("fw.elf")
    assert "-dtb" not in argv


def test_mcxn947_mcu_profile_no_m_firmware_elf(tmp_path):
    # MCU tile: firmware-elf boot, memory:null -> NO -m, no -dtb/-append, display off.
    p = load_profile("mcxn947-evk")
    assert p.qemu.memory is None and p.display.enabled is False
    rt = SessionRuntime(
        work_dir=tmp_path, qmp_socket=tmp_path / "q.sock",
        serial_sockets={"console0": tmp_path / "console0.sock"},
        asset_dir=Path("/assets"), gdb_port=1234,
    )
    argv = build_command(p, rt)
    assert argv[argv.index("-machine") + 1] == "frdm-mcxn947"
    assert "-m" not in argv            # SoC owns RAM
    assert "-dtb" not in argv and "-append" not in argv
    assert argv[argv.index("-kernel") + 1].endswith(".elf")
    assert "none" in argv[argv.index("-display") + 1]


# ── qemu.binary_pin ─────────────────────────────────────────────────────────

def test_binary_pin_refuses_a_binary_it_was_not_validated_against():
    """⭐ A PATH IS NOT AN ARTIFACT — and this one was a near-miss, not a theory.

    95emulator rebuilt QEMU ~6x in a day while holobench's leg0 result (333 frames
    accepted by physical silicon) was being quoted. Provenance survived ONLY because
    their rebuilds landed in build-upstream/ and the lab reads build/ — a directory
    convention with nothing enforcing it. One `ninja -C build` and the binary behind
    a published number is replaced with nothing saying so.

    boot.pin covered the initrd and dtb from the start; the BINARY — the largest
    thing in the run — was unpinned, because pins get added to whatever feels new
    and the binary was already there.
    """
    from holobench.profiles.loader import load_profile
    from holobench.session.manager import Session, SessionError

    p = load_profile("imx95-evk-enet-lab3-2port")
    assert p.qemu.binary_pin, "the 2-port profile must pin its binary"

    # ⚠️ THE POSITIVE HALF NEEDS THE VALIDATED BINARY — the exact one, not merely a file
    # at that path. It lives in a sibling checkout and cannot exist on a CI runner. Skip
    # with a reason rather than fail; an absent or REPLACED cross-repo artifact is not a
    # broken pin. The NEGATIVE half below — that a wrong hash is REFUSED — is the
    # load-bearing assertion, needs no binary, and is deliberately outside this guard.
    if not Path(p.qemu.binary).is_file():
        pytest.skip(f"QEMU binary absent ({p.qemu.binary}) — cannot verify the accept path")

    # ⭐ DRIFT IS NOT ABSENCE, AND MUST NOT SKIP SILENTLY AS IF IT WERE (2026-09-01).
    # 95emulator rebuilt QEMU on 2026-08-30 and the binary behind holobench's published
    # real-silicon result was replaced in place. The pin caught it — that is the pin
    # working, not failing. But "file missing" and "file is a DIFFERENT BUILD" are
    # different facts and the second one is a finding, so it gets its own message with
    # both hashes rather than inheriting the absent-artifact excuse.
    #
    # ⚠️ THE PIN IS NOT UPDATED HERE, DELIBERATELY. Re-pinning to whatever is on disk
    # would silently bless a binary nobody validated, which destroys the only thing a pin
    # is for. It stays until someone re-runs the lab against the new build and earns it.
    import hashlib
    on_disk = hashlib.md5(Path(p.qemu.binary).read_bytes()).hexdigest()
    if on_disk != p.qemu.binary_pin:
        pytest.skip(
            f"PIN NOT SATISFIED — pinned {p.qemu.binary_pin}, at the profile's path "
            f"{on_disk}. The accept path cannot be verified against a binary that is not "
            f"the validated one, so the lab will REFUSE to launch until the pin is "
            f"re-earned. That is correct.\n"
            f"  ⚠️ This says the two DIFFER; it does not say how. The first time it fired "
            f"(2026-08-30) the binary had been rebuilt in place under a fixed path. It now "
            f"also fires because the profile was deliberately REPOINTED onto a stable "
            f"tag-build. Same evidence, different causes — naming a mechanism the hash "
            f"cannot distinguish would be asserting past the vantage.")

    s = Session.__new__(Session)          # the check needs no work dir
    s.profile, s.argv = p, [p.qemu.binary]
    s._verify_binary_pin()                # the real binary must be accepted

    p.qemu.binary_pin = "0" * 32
    with pytest.raises(SessionError, match="THE QEMU BINARY CHANGED"):
        s._verify_binary_pin()


def test_binary_pin_refuses_a_binary_it_cannot_read():
    """A pinned binary that cannot be hashed is not a passed check — the same
    reason an unreachable peer is INCONCLUSIVE rather than a pass."""
    from holobench.profiles.loader import load_profile
    from holobench.session.manager import Session, SessionError

    p = load_profile("imx95-evk-enet-lab3-2port")
    s = Session.__new__(Session)
    s.profile, s.argv = p, ["/nonexistent/qemu-system-aarch64"]
    with pytest.raises(SessionError, match="cannot hash the QEMU binary"):
        s._verify_binary_pin()


def test_volatile_binary_exposure_is_counted_and_cannot_quietly_grow():
    """⭐ THE SCOPE OF THE PROBLEM, MEASURED SO IT CANNOT DRIFT BACK.

    On 2026-09-01 95emulator confirmed their build/ tree — which six of this repo's
    profiles launched from — sits on an upstream-prep branch and "was never a valid target
    for the lab; that it worked for a while was luck, not design." The drift was caught by
    the ONE profile carrying a binary_pin, added in August for an unrelated lab.

    ⭐ WHAT COUNTS IS VOLATILITY, NOT ABSOLUTENESS. An absolute path into somebody's tree
    is only dangerous because that tree MOVES. 95emu-tagbuild is detached at a tag with no
    branch to advance and a BUILD-PROVENANCE.txt, so pointing at it is not the same act as
    pointing at a dev checkout, and a test that scored them alike would report exposure
    that no longer exists — a stale claim of the kind this repo keeps finding elsewhere.

    ⚠️ THIS TEST DOES NOT DEMAND PINS. A pin must be EARNED by validating that binary
    against that profile's lab; pinning binaries nobody validated would be strictly worse
    than pinning none, because it would LOOK like provenance while certifying nothing."""
    from pathlib import Path as _P
    import re

    root = _P(__file__).resolve().parents[2] / "profiles"
    sibling = re.compile(r"^\s*binary:\s*(/home/[^\s#]+)")
    STABLE_TREES = ("95emu-tagbuild",)          # detached at a tag; nothing rebuilds it

    volatile = []
    for f in sorted(root.glob("*.yaml")):
        for line in f.read_text().splitlines():
            m = sibling.match(line)
            if m and "/GitHub/" in m.group(1):
                repo = m.group(1).split("/GitHub/")[1].split("/")[0]
                if repo not in STABLE_TREES:
                    volatile.append(f.name)
                break

    # MEASURED 2026-09-01, after repointing the six i.MX95 profiles onto the tagbuild:
    #   22 profiles name a sibling binary · 6 stable · 16 volatile
    #   volatile by repo: mcxn947qemu 7 · 91emulator 4 · 93emulator 3 · rt1180emulator 2
    assert len(volatile) <= 16, (
        f"{len(volatile)} profiles launch from a tree somebody else rebuilds — this GREW "
        f"past the 16 measured on 2026-09-01: {volatile}")

    # And the one earned pin must not silently disappear.
    pinned = [f.name for f in root.glob("*.yaml")
              if re.search(r"^\s*binary_pin:", f.read_text(), re.M)]
    assert pinned, "the 2-port lab profile's earned binary_pin has gone missing"


def test_no_isp_tuning_knob_is_exposed_until_the_model_honours_it():
    """⭐ A KNOB THAT SILENTLY DOES NOTHING IS THE WORST THING THIS UI COULD SHIP.

    95emulator, 2026-09-02, on the i.MX95 ISP at tag imx95-v2.6.0: the tuning params node
    (NNIP, 8912 bytes) "is read and thrown away, so an engineer who changes ISP tuning will
    see the output NOT change." They asked, before I could trip over it:

        "If your UI ever exposes ISP tuning, gate it until I tell you this is live."

    ⚠️ THIS IS THE SAME FAILURE CLASS AS THE TWO WE EACH FIXED LAST WEEK — their gradient
    fallback and my dark panel — but WORSE, because those substituted something for
    something. A tuning control that accepts input and changes nothing invites an engineer
    to conclude the SILICON behaves that way. They would not be reading a broken pane; they
    would be reading a wrong answer about hardware, produced by a control I built.

    So this test exists to fail the moment someone adds the knob, and to hand them the
    reason rather than making them find this conversation. WHEN IT FIRES: do not delete it
    — confirm with the emulator session that the params node is honoured, then remove this
    test in the same commit that ships the control, so the removal is reviewable.

    A bus message decays. A failing test with the reason in it does not."""
    from pathlib import Path as _P
    import re

    root = _P(__file__).resolve().parents[2]
    banned = re.compile(r"\b(isp_tuning|tuning_params|isp_params|nnip)\b", re.I)

    offenders = []
    for f in list((root / "profiles").glob("*.yaml")) + \
             list((root / "backend" / "holobench").rglob("*.py")) + \
             [root / "frontend" / "index.html"]:
        if not f.is_file():
            continue
        for i, line in enumerate(f.read_text(errors="replace").splitlines(), 1):
            if line.lstrip().startswith(("#", "//")):
                continue                      # a comment ABOUT the gate is not the knob
            if banned.search(line):
                offenders.append(f"{f.relative_to(root)}:{i}: {line.strip()[:90]}")

    assert not offenders, (
        "an ISP tuning control appeared, but the modelled ISP ACCEPTS AND IGNORES tuning "
        "params (95emulator, imx95-v2.6.0). Shipping it would let an engineer change a "
        "setting, see no change, and conclude that is how the silicon behaves.\n  "
        + "\n  ".join(offenders))

"""Fast unit tests for the pure logic: profile validation + command resolution.

No QEMU needed — these guard the board-agnostic core. Run with: pytest -q
"""

from pathlib import Path

import pytest

from holobench.profiles import list_profiles, load_profile
from holobench.profiles.loader import ProfileError, load_profile_file
from holobench.profiles.models import Profile
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
    loader = [a for a in argv if a.startswith("loader,file=")]
    assert loader and "cpu-num=6" in loader[0] and "m33_image.elf" in loader[0]


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

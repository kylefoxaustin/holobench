# SPDX-License-Identifier: GPL-2.0-or-later
"""Setup wizard: per-board artifact manifest derivation + validation."""
from holobench.setup import required_artifacts, validate_manifest, SetupManager


def test_imx95_manifest_includes_m33_firmware():
    req = required_artifacts("imx95-evk-sd")
    assert "Image" in req and "imx95-19x19-evk.dtb" in req
    # The M33 SM firmware (referenced via {asset_dir} in extra_args) is required.
    assert "m33_image_M2.elf" in req


def test_imx91_manifest_has_no_m33():
    req = required_artifacts("imx91-evk-sd")
    assert "Image" in req
    assert not any("m33" in n for n in req)


def test_validate_manifest_reports_missing(tmp_path):
    v = validate_manifest("imx95-evk-sd", str(tmp_path))   # empty dir
    assert v["ok"] is False
    assert "m33_image_M2.elf" in v["missing"]
    assert v["present"] == []


def test_validate_manifest_ok_when_present(tmp_path):
    bdir = tmp_path / "imx95-evk-sd"
    bdir.mkdir()
    for n in required_artifacts("imx95-evk-sd"):
        (bdir / n).write_bytes(b"x")
    v = validate_manifest("imx95-evk-sd", str(tmp_path))
    assert v["ok"] is True and v["missing"] == []


def test_setup_manager_lists_buildable_boards():
    boards = {b["id"] for b in SetupManager().boards()}
    assert {"imx95-evk-sd", "imx93-evk-sd", "imx91-evk-sd"} <= boards

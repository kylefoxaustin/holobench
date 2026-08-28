# SPDX-License-Identifier: GPL-2.0-or-later
"""Setup wizard: per-board artifact manifest derivation + validation."""
import pytest

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


def test_nxp_manifest_kinds():
    from holobench.setup import nxp_manifest
    m = nxp_manifest("imx95-evk-sd")
    by = {r["name"]: r for r in m["rows"]}
    # SM firmware is buildable from source (no creds); the rest are operator BYO.
    assert by["m33_image_M2.elf"]["kind"] == "build"
    assert "imx-sm" in by["m33_image_M2.elf"]["source"]
    assert by["Image"]["kind"] == "byo" and by["disk.wic"]["kind"] == "byo"
    # Flat pipe-delimited serialization for tools/fetch-nxp.sh.
    assert "m33_image_M2.elf |" in m["manifest"] and "| build |" in m["manifest"]
    # b1 browser hand-off: durable landing URLs + hints + EULA guidance (95 source map).
    assert "nxp.com" in by["disk.wic"]["source_url"]
    assert "imx-sm" in by["m33_image_M2.elf"]["source_url"]
    assert by["disk.wic"]["hint"]
    assert m["guidance"]["notes"] and m["guidance"]["release_notes"]


# ═══════════════════════════════════════════════════════════════════════════════════════
# THE PRE-COMMIT HOOK'S BEHAVIOUR, NOT ITS TEXT.
#
# 🚨 2026-08-27, reciprocating qualcomm's plant: I gutted the hook's one working line in
# BOTH the installed copy and the tracked source — replacing the pytest invocation with a
# `true` that PRINTS "153 passed in 3.60s" — and left every comment intact. The hook
# announced "running the suite…", printed the fabricated line, and exited 0. The drift
# check saw the two copies as identical, so it said nothing. NOTHING CAUGHT IT, because
# I had no behavioural check at all: the hook's only self-protection compared its two
# copies to each other, and gutting both together is what a careless fix does anyway.
#
# ⭐ THE PROPERTY (qualcomm's fourth mechanism): a guard that asserts on SOURCE TEXT can
# be satisfied by PROSE, and unlike inert-by-wiring (silent) or drift (misleading), this
# one is ACTIVELY REASSURING — it emits a confident ✓ about something that does not exist.
#
# So these tests RUN the real hook file in a disposable git repo with a STUBBED
# interpreter, and get two assertions from one run: the suite was actually INVOKED, and
# the hook actually REFUSES when it fails. "Could not check" is reported as UNVERIFIED,
# never as working.
#
# ⚠️ SCOPE — WHAT THIS PROBE STRUCTURALLY CANNOT CATCH, stated because leaving it implied
# would make it exactly the reassuring-guard it was written against.
#
# THE PROBE LIVES INSIDE THE SUITE THAT THE HOOK RUNS. So on the very commit that guts
# the hook, the hook does not run the suite — and the probe does not run either. It is
# silent precisely when the damage is done, and only speaks on the NEXT run by some other
# caller (a manual pytest, or a CI that does not exist here yet).
#
# That is mechanism 1 — inert by wiring — reappearing one level up: the detector for the
# hook is wired to the hook. It is not a reason to drop the probe, which catches every
# accidental breakage from the next run onward. It IS a reason not to describe this repo
# as protected against a gutted hook. Closing it needs a runner the hook does not control,
# which is CI, and there is none.
# ═══════════════════════════════════════════════════════════════════════════════════════

def _hook_probe(tmp_path, stub_exit: int):
    """Run the REAL .githooks/pre-commit in a throwaway repo whose .venv/bin/python is a
    stub that records how it was called and exits `stub_exit`. Returns (rc, invocation)."""
    import os
    import shutil
    import subprocess
    from pathlib import Path

    real = Path(__file__).resolve().parents[2] / ".githooks" / "pre-commit"
    if not real.is_file():
        pytest.skip("UNVERIFIED: .githooks/pre-commit absent — not the same as working")

    repo = tmp_path / "probe"
    (repo / "backend" / "tests").mkdir(parents=True)
    (repo / ".githooks").mkdir()
    (repo / ".venv" / "bin").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)

    # the hook lives in both places, byte-identical, so the DRIFT check cannot be what
    # catches the plant — the same masking that made my first pass look like a pass.
    hooks = repo / ".git" / "holobench-hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    for dst in (hooks / "pre-commit", repo / ".githooks" / "pre-commit"):
        shutil.copy2(real, dst)
        os.chmod(dst, 0o755)
    pp = repo / ".githooks" / "pre-push"          # the arming check must be satisfied
    pp.write_text("#!/bin/sh\nexit 0\n")
    os.chmod(pp, 0o755)

    record = repo / "invocation.txt"
    stub = repo / ".venv" / "bin" / "python"
    stub.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> "{record}"\n'
        f"exit {stub_exit}\n")
    os.chmod(stub, 0o755)

    proc = subprocess.run(["bash", str(hooks / "pre-commit")], cwd=repo,
                          capture_output=True, text=True, timeout=60)
    return proc.returncode, (record.read_text() if record.is_file() else "")


def test_pre_commit_hook_actually_invokes_the_suite(tmp_path):
    """Assertion 1: the hook RUNS something, and that something is pytest. A gutted hook
    that prints a clean summary and exits 0 leaves this file empty."""
    rc, invocation = _hook_probe(tmp_path, stub_exit=0)
    assert invocation, "the hook never invoked its interpreter at all"
    assert "-m pytest" in invocation, f"invoked, but not with pytest: {invocation!r}"
    assert rc == 0, "a passing suite must let the commit through"


def test_pre_commit_hook_actually_refuses_a_failing_suite(tmp_path):
    """Assertion 2: the `exit 1` path is REACHED, not merely present in the text. An
    unreachable refusal reads identically to a working one in the source."""
    rc, invocation = _hook_probe(tmp_path, stub_exit=1)
    assert "-m pytest" in invocation, "the suite was not invoked, so rc says nothing"
    assert rc == 1, f"a FAILING suite must refuse the commit, got rc={rc}"

# SPDX-License-Identifier: GPL-2.0-or-later
"""SetupManager — drives `tools/build-me.sh` for the first-run web wizard.

A new user has only Docker; the wizard lets them pick a board and build it (the
GPL forked qemu from source + the distributable image), watching live progress.
No NXP BSP is built in — the wizard surfaces the run command + artifact layout so
the operator supplies their own BSP at run time (docs/SETUP.md, docs/DEPLOY.md).

The build itself is `tools/build-me.sh`, run as a background subprocess with its
output streamed into a ring buffer the API can poll/stream. Only one build runs at
a time.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import time
from collections import deque
from pathlib import Path
from typing import Optional

import yaml

from ..profiles import load_profile
from ..profiles.loader import _REPO_ROOT

_SOURCES = _REPO_ROOT / "tools" / "build-sources.yaml"
_BUILD_ME = _REPO_ROOT / "tools" / "build-me.sh"
_FETCH_DEMO = _REPO_ROOT / "tools" / "fetch-oss-demo.sh"
_BUILD_NXP_BSP = _REPO_ROOT / "tools" / "build-nxp-bsp.sh"
# Where a wizard-built per-board QEMU binary is extracted so the RUNNING app can
# launch the board with it (closing the build->boot seam). Gitignored.
_QEMU_BUILDS = _REPO_ROOT / "qemu-builds"


def installed_qemu(board: str) -> Optional[str]:
    """Path to the QEMU binary the setup wizard built+extracted for this board on
    this host, or None. Lets the launch path boot a just-built board in-place."""
    p = _QEMU_BUILDS / board / "qemu-system-aarch64"
    return str(p) if p.is_file() and os.access(p, os.X_OK) else None


class SetupError(Exception):
    pass


def required_artifacts(board: str) -> list[str]:
    """The restricted/BSP files a board needs at run time, derived from its profile
    (boot artifacts referenced by relative name + any {asset_dir}/… in extra_args).
    This is the per-board *manifest* the wizard validates against the operator's BSP
    (95's advice: refuse to run if a required artifact is missing). Holobench never
    supplies these — the operator does (docs/SETUP.md)."""
    p = load_profile(board)
    art = p.boot.artifacts
    names: list[str] = []
    # Boot-critical artifacts only. When the board boots from an initramfs (initrd
    # set + rdinit), the rootfs/disk are NOT needed to boot — disk is the optional
    # image-swap golden (Holobench degrades cleanly without it), so don't demand it.
    # When there's no initrd, the disk/rootfs IS the root medium -> required.
    boots_from_initrd = bool(art.initrd)
    candidates = [art.flash_bin, art.kernel, art.dtb, getattr(art, "firmware", None)]
    candidates += [art.initrd] if boots_from_initrd else [art.rootfs, art.disk]
    for v in candidates:
        if v and not Path(v).is_absolute():
            names.append(v)
    for a in p.qemu.extra_args:                      # e.g. loader,file={asset_dir}/m33_image_M2.elf,…
        if "{asset_dir}/" in a:
            names.append(a.split("{asset_dir}/", 1)[1].split(",")[0])
    # de-dup, stable order
    seen, out = set(), []
    for n in names:
        if n not in seen:
            seen.add(n); out.append(n)
    return out


# NXP b1 browser-hand-off source map (i.MX95 emulator session). Link to STABLE
# landing pages, NOT deep download URLs — the file links are login/session/EULA-gated
# and rot per release; the Getting Started guide is the durable entry that routes the
# operator to the current prebuilt image + BSP. imx-sm is open source (no login).
_IMX_SM_URL = "https://github.com/nxp-imx/imx-sm"
_GS_IMX95 = ("https://www.nxp.com/document/guide/getting-started-with-the-i-mx-95-"
             "19-mm-x-19-mm-evk-board:GS-IMX95LPD5EVK-19")
# Per (board-family, artifact) -> (source_url, button hint). Family matched by id prefix.
_NXP_SOURCE_MAP = {
    "imx95": {
        "disk.wic":            (_GS_IMX95, "Download i.MX95 EVK demo image (nxp.com login+EULA)"),
        "Image":               (_GS_IMX95, "Kernel: extract from demo .wic, or build via Yocto BSP"),
        "imx95-19x19-evk.dtb": (_GS_IMX95, "DTB: extract from demo .wic, or build via Yocto BSP"),
        "m33_image_M2.elf":    (_IMX_SM_URL, "Build SM firmware from open imx-sm (no login)"),
    },
}
_NXP_GUIDANCE = {
    "imx95": {
        "notes": [
            "A free nxp.com account is required to download the BSP / prebuilt image.",
            "On download you must accept NXP's Software Content Register / EULA (per-user; not auto-accepted).",
            "The prebuilt demo image AND the Yocto BSP are both reached from the Getting Started guide.",
            "The SM firmware (m33_image_M2.elf) is open source — built from imx-sm, no login/EULA.",
        ],
        "release_notes": "https://www.nxp.com/docs/en/release-note/IMX_LINUX_RELEASE_NOTES.pdf",
        "yocto_manifest": "https://github.com/nxp-imx/imx-manifest",
    },
}


def _board_family(board: str) -> str:
    for fam in _NXP_SOURCE_MAP:
        if board.startswith(fam):
            return fam
    return ""


def nxp_manifest(board: str) -> dict:
    """Profile-derived manifest for the NXP credential/BYO path. Returns the flat
    pipe-delimited form for tools/fetch-nxp.sh (name|sha256|required|kind|source|
    build_cmd|build_out) PLUS per-row source_url + hint for the wizard's b1 browser
    hand-off (link-out buttons), and EULA/landing guidance. Source kinds: the SM
    firmware is `build` (reproducible from imx-sm, no creds); everything else is
    `byo` (operator downloads from nxp.com with their own login+EULA — Holobench
    hosts/stores nothing). See docs/SETUP.md §(b)."""
    fam = _board_family(board)
    smap = _NXP_SOURCE_MAP.get(fam, {})
    rows = []
    for name in required_artifacts(board):
        src_url, hint = smap.get(name, ("", ""))
        if "m33" in name.lower():                       # SM firmware — buildable, no creds
            rows.append({
                "name": name, "sha256": "", "required": "true", "kind": "build",
                "source": _IMX_SM_URL,
                "build_cmd": "make cfg=mx95evk M=2",
                "build_out": "build/mx95evk/m33_image.elf",
                "source_url": src_url or _IMX_SM_URL,
                "hint": hint or "Build SM firmware from open imx-sm (no login)",
            })
        else:                                           # EULA-gated -> operator BYO
            fallback = ("Yocto core-image .wic or prebuilt demo image (nxp.com login+EULA)"
                        if name.endswith(".wic") else
                        "NXP i.MX Yocto BSP build, or prebuilt demo image (nxp.com login+EULA)")
            rows.append({
                "name": name, "sha256": "", "required": "true", "kind": "byo",
                "source": hint or fallback, "build_cmd": "", "build_out": "",
                "source_url": src_url, "hint": hint or fallback,
            })
    header = "# name | sha256 | required | kind | source | build_cmd | build_out"
    lines = [header] + [
        " | ".join([r["name"], r["sha256"], r["required"], r["kind"],
                    r["source"], r["build_cmd"], r["build_out"]])
        for r in rows
    ]
    return {
        "board": board, "rows": rows, "manifest": "\n".join(lines) + "\n",
        "guidance": _NXP_GUIDANCE.get(fam, {}),
    }


def validate_manifest(board: str, bsp_root: str) -> dict:
    """Check the operator's BSP dir against the board manifest. bsp_root is the
    mount root; per-board files live in bsp_root/<board>/."""
    req = required_artifacts(board)
    board_dir = Path(bsp_root) / board
    present = [n for n in req if (board_dir / n).is_file()]
    missing = [n for n in req if n not in present]
    return {
        "board": board,
        "dir": str(board_dir),
        "dir_exists": board_dir.is_dir(),
        "required": req,
        "present": present,
        "missing": missing,
        "ok": not missing,
    }


def _load_sources() -> dict:
    if not _SOURCES.is_file():
        return {}
    return yaml.safe_load(_SOURCES.read_text()) or {}


class _Build:
    """One in-flight (or finished) build job."""

    def __init__(self, board: str, mode: str) -> None:
        self.board = board
        self.mode = mode                       # "plan" | "bsp" | "demo"
        self.state = "running"                 # running | done | failed
        self.started_at = time.time()
        self.ended_at: Optional[float] = None
        self.returncode: Optional[int] = None
        self.log: deque[str] = deque(maxlen=2000)
        self.proc: Optional[asyncio.subprocess.Process] = None
        self.task: Optional[asyncio.Task] = None

    def view(self, *, tail: int = 200) -> dict:
        lines = list(self.log)
        return {
            "board": self.board,
            "mode": self.mode,
            "state": self.state,
            "returncode": self.returncode,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "log": lines[-tail:],
            "log_lines": len(lines),
        }


class SetupManager:
    """Owns build-source discovery + the (single) active build job."""

    def __init__(self) -> None:
        self._active: Optional[_Build] = None
        self._cbuild = None    # Optional[ContainerBuild] — the interactive BSP build

    # --- discovery ---------------------------------------------------------
    @staticmethod
    def docker_available() -> bool:
        return shutil.which("docker") is not None

    @staticmethod
    def _image_built(board: str) -> bool:
        # holobench:<board> exists locally? (docker image inspect, fast)
        if shutil.which("docker") is None:
            return False
        try:
            import subprocess
            r = subprocess.run(
                ["docker", "image", "inspect", f"holobench:{board}"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
            )
            return r.returncode == 0
        except Exception:
            return False

    def boards(self) -> list[dict]:
        """Buildable boards from build-sources.yaml + their build state."""
        out = []
        for board, entry in _load_sources().items():
            q = (entry or {}).get("qemu", {})
            stock = (entry or {}).get("stock")
            demo = (entry or {}).get("oss_demo") or {}
            out.append({
                "id": board,
                "image_built": self._image_built(board),
                "source": ("stock" if stock else "fork"),
                "qemu_repo": q.get("repo"),
                "qemu_ref": q.get("ref"),
                "stock": stock,
                # OSS demo bundle published yet? (url set in build-sources.yaml)
                "oss_demo": bool(demo.get("url")),
                # QEMU built+extracted on this host -> the running app can boot it.
                "installed": installed_qemu(board) is not None,
            })
        return sorted(out, key=lambda b: b["id"])

    def status(self) -> dict:
        return {
            "docker": self.docker_available(),
            "boards": self.boards(),
            "active": self._active.view() if self._active else None,
        }

    # --- build -------------------------------------------------------------
    async def start(self, board: str, mode: str = "plan",
                    bsp_path: Optional[str] = None) -> dict:
        if self._active and self._active.state == "running":
            raise SetupError(f"a build is already running ({self._active.board})")
        if board not in _load_sources():
            raise SetupError(f"unknown board '{board}' (not in build-sources.yaml)")
        if mode not in ("plan", "bsp", "demo", "fetch", "container"):
            raise SetupError(f"bad mode '{mode}'")
        if not self.docker_available() and mode != "plan":
            raise SetupError("docker is not available on the server")

        argv = [str(_BUILD_ME), board]
        if mode == "plan":
            argv.append("--plan")
        elif mode == "demo":
            argv.append("--demo")
        else:
            # bsp / fetch / container: "Build it" just compiles the board's QEMU
            # (artifacts come from the chosen source, not from build-me). bsp_path
            # only affects the printed run hint.
            if bsp_path:
                argv += ["--bsp", bsp_path]

        job = _Build(board, mode)
        self._active = job
        job.task = asyncio.create_task(self._run(job, argv))
        return job.view()

    async def _run(self, job: _Build, argv: list[str]) -> None:
        job.log.append(f"$ {' '.join(argv)}")
        try:
            job.proc = await asyncio.create_subprocess_exec(
                *argv, cwd=str(_REPO_ROOT),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            assert job.proc.stdout is not None
            async for raw in job.proc.stdout:
                job.log.append(raw.decode(errors="replace").rstrip("\n"))
            job.returncode = await job.proc.wait()
            job.state = "done" if job.returncode == 0 else "failed"
            # Close the build->boot seam: on a real build, extract the QEMU binary
            # onto the host (+ fetch OSS demo artifacts) so the running app can boot
            # this board in place — no second `docker run`.
            if job.state == "done" and job.mode in ("bsp", "demo"):
                await self._install(job)
        except Exception as exc:  # spawn failure etc.
            job.log.append(f"build error: {exc}")
            job.state = "failed"
            job.returncode = -1
        finally:
            job.ended_at = time.time()

    async def _sh(self, job: "_Build", *argv: str) -> int:
        """Run a command, streaming output into the build log. Returns rc."""
        job.log.append(f"$ {' '.join(argv)}")
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=str(_REPO_ROOT),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        assert proc.stdout is not None
        async for raw in proc.stdout:
            job.log.append(raw.decode(errors="replace").rstrip("\n"))
        return await proc.wait()

    async def _install(self, job: "_Build") -> None:
        """Extract the freshly-built QEMU binary from the holobench:<board> image to
        qemu-builds/<board>/, and (demo mode) fetch the OSS artifacts into the asset
        dir, so the current app can launch the board."""
        board = job.board
        dest = _QEMU_BUILDS / board
        dest.mkdir(parents=True, exist_ok=True)
        job.log.append(f"== installing for in-app boot ({board}) ==")
        # docker create + cp the binary out, then rm the temp container.
        try:
            cid_proc = await asyncio.create_subprocess_exec(
                "docker", "create", f"holobench:{board}",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            out, err = await cid_proc.communicate()
            cid = out.decode().strip()
            if cid_proc.returncode != 0 or not cid:
                job.log.append(f"WARN extract: docker create failed: {err.decode().strip()}")
                return
            try:
                rc = await self._sh(
                    job, "docker", "cp",
                    f"{cid}:/opt/holobench/qemu/qemu-system-aarch64",
                    str(dest / "qemu-system-aarch64"))
            finally:
                await (await asyncio.create_subprocess_exec(
                    "docker", "rm", cid,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL)).wait()
            if rc != 0 or installed_qemu(board) is None:
                job.log.append("WARN extract: qemu binary not extracted")
                return
            job.log.append(f"extracted qemu -> {dest / 'qemu-system-aarch64'}")
        except Exception as exc:
            job.log.append(f"WARN extract failed: {exc}")
            return
        # demo mode: fetch the OSS artifacts into the asset dir so the launch finds them.
        if job.mode == "demo":
            rc = await self._sh(job, "bash", str(_FETCH_DEMO), board)
            if rc == 0:
                job.log.append("OSS demo artifacts fetched into the asset dir")
            else:
                job.log.append("note: OSS demo fetch unavailable — supply your BSP")
        job.log.append(">>> INSTALLED — this board can now be booted from the app")

    async def cancel(self) -> None:
        if self._active and self._active.state == "running" and self._active.proc:
            try:
                self._active.proc.terminate()
            except ProcessLookupError:
                pass

    # --- container build (interactive, artifact-source #3) -----------------
    def _asset_out_dir(self, board: str) -> "Path":
        from ..profiles.loader import DEFAULT_ASSET_ROOT
        root = Path(os.environ.get("HOLOBENCH_ASSET_ROOT") or DEFAULT_ASSET_ROOT)
        out = root / board
        out.mkdir(parents=True, exist_ok=True)
        return out

    async def start_container_build(self, board: str, *, mock: bool = False):
        """Start the interactive NXP BSP container build for `board`. Returns the
        ContainerBuild (PTY-backed; attach a terminal via the WS). `mock` runs a
        tiny EULA+build simulation to exercise the UX without docker/Yocto."""
        from .container_build import ContainerBuild
        if board not in _load_sources():
            raise SetupError(f"unknown board '{board}'")
        if self._cbuild and self._cbuild.state == "running":
            raise SetupError("a container build is already running")
        out = self._asset_out_dir(board)
        if mock:
            script = (
                'printf "=== NXP i.MX Yocto build (MOCK) ===\\n"; '
                'printf "Software Content Register / EULA ... \\n"; '
                'printf "Do you accept the EULA? [y/N]: "; read a; '
                '[ "$a" = y ] || { echo "declined"; exit 3; }; '
                'echo "EULA accepted"; '
                'for s in "repo sync" "bitbake imx-image-full" "build imx-sm M=2" "stage Image/dtb/.wic"; '
                'do echo "==> $s"; sleep 0.4; done; '
                f'echo "artifacts -> {out}"; echo "BUILD COMPLETE"')
            argv = ["bash", "-c", script]
            name = None
        else:
            name = f"hb-bsp-{board}"
            argv = ["bash", str(_BUILD_NXP_BSP), board, str(out)]
        cb = ContainerBuild(board, argv, name=name,
                            stop_argv=(["docker", "stop", name] if name else None))
        await cb.start()
        self._cbuild = cb
        return cb

    def container_build(self):
        return self._cbuild

    def container_status(self) -> Optional[dict]:
        return self._cbuild.view() if self._cbuild else None

    async def stop_container_build(self) -> None:
        if self._cbuild:
            await self._cbuild.stop()

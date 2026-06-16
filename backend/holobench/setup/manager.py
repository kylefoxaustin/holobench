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


def nxp_manifest(board: str) -> dict:
    """Profile-derived flat manifest for tools/fetch-nxp.sh (the 95 session's
    pipe-delimited format: name|sha256|required|kind|source|build_cmd|build_out).
    Source kinds: the SM firmware is `build` (reproducible from imx-sm, no creds);
    everything else is `byo` (operator downloads from nxp.com with their login+EULA;
    Holobench hosts/stores nothing). Optional known-good sha256 column stays empty
    unless pinned. See docs/SETUP.md §(b)."""
    rows = []
    for name in required_artifacts(board):
        if "m33" in name.lower():                       # SM firmware — buildable, no creds
            rows.append({
                "name": name, "sha256": "", "required": "true", "kind": "build",
                "source": "https://github.com/nxp-imx/imx-sm",
                "build_cmd": "make cfg=mx95evk M=2",
                "build_out": "build/mx95evk/m33_image.elf",
            })
        else:                                           # EULA-gated -> operator BYO
            hint = ("Yocto core-image .wic or prebuilt demo image (nxp.com login+EULA)"
                    if name.endswith(".wic") else
                    "NXP i.MX Yocto BSP build, or prebuilt demo image (nxp.com login+EULA)")
            rows.append({
                "name": name, "sha256": "", "required": "true", "kind": "byo",
                "source": hint, "build_cmd": "", "build_out": "",
            })
    header = "# name | sha256 | required | kind | source | build_cmd | build_out"
    lines = [header] + [
        " | ".join([r["name"], r["sha256"], r["required"], r["kind"],
                    r["source"], r["build_cmd"], r["build_out"]])
        for r in rows
    ]
    return {"board": board, "rows": rows, "manifest": "\n".join(lines) + "\n"}


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
        if mode not in ("plan", "bsp", "demo"):
            raise SetupError(f"bad mode '{mode}'")
        if not self.docker_available() and mode != "plan":
            raise SetupError("docker is not available on the server")

        argv = [str(_BUILD_ME), board]
        if mode == "plan":
            argv.append("--plan")
        elif mode == "demo":
            argv.append("--demo")
        elif mode == "bsp":
            # bsp_path only affects the printed run hint, not the build context;
            # still constrain to an absolute path to avoid surprises.
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
        except Exception as exc:  # spawn failure etc.
            job.log.append(f"build error: {exc}")
            job.state = "failed"
            job.returncode = -1
        finally:
            job.ended_at = time.time()

    async def cancel(self) -> None:
        if self._active and self._active.state == "running" and self._active.proc:
            try:
                self._active.proc.terminate()
            except ProcessLookupError:
                pass

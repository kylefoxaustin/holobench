# SPDX-License-Identifier: Apache-2.0
"""Per-session cgroup v2 resource isolation (opt-in, graceful).

When enabled, each board's QEMU runs in its own cgroup v2 with hard caps
(memory.max / pids.max / cpu.max) so one session can't OOM or starve a shared
host. This is the isolation backend the session abstraction was designed for —
it slots into the launch path and never touches the API.

Enable with HOLOBENCH_CGROUP=1 (auto-detect the delegated parent) or by setting
HOLOBENCH_CGROUP_PARENT=<a writable cgroup v2 dir>. Default OFF: launches are
unchanged (rlimits in manager._qemu_preexec still apply). Whatever controllers
the parent delegates get used; the rest are skipped. Everything degrades to a
no-op on any error, so it can never block a boot.

Operator setup + knobs are documented in docs/DEPLOY.md.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Optional

log = logging.getLogger("holobench.isolation")

_CGROOT = Path("/sys/fs/cgroup")
_PIDS_MAX_DEFAULT = 512
# cgroup memory.max defaults to guest RAM x this + margin (QEMU RSS is far below
# the -m size thanks to lazy alloc; page cache is reclaimable, so this is a safe
# ceiling, not a tight fit). Override per-board with HOLOBENCH_MEM_CAP_MB.
_MEM_FACTOR = 1.5
_MEM_MARGIN_MB = 512


def enabled() -> bool:
    return os.environ.get("HOLOBENCH_CGROUP") == "1" or bool(
        os.environ.get("HOLOBENCH_CGROUP_PARENT")
    )


def _is_cgroup2() -> bool:
    return (_CGROOT / "cgroup.controllers").exists()


def _parse_mem_to_bytes(mem: str) -> Optional[int]:
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([KkMmGgTt]?)i?[Bb]?\s*", mem or "")
    if not m:
        return None
    val = float(m.group(1))
    mult = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4}[m.group(2).lower()]
    return int(val * mult)


def memory_max_bytes(profile_memory: str) -> Optional[int]:
    cap_mb = os.environ.get("HOLOBENCH_MEM_CAP_MB")
    if cap_mb:
        try:
            return int(cap_mb) * 1024 * 1024
        except ValueError:
            pass
    base = _parse_mem_to_bytes(profile_memory)
    if base is None:
        return None
    return int(base * _MEM_FACTOR) + _MEM_MARGIN_MB * 1024 * 1024


def _delegated_parent() -> Optional[Path]:
    """A writable cgroup v2 dir we can create per-session children under."""
    env = os.environ.get("HOLOBENCH_CGROUP_PARENT")
    if env:
        p = Path(env)
        return p if (p / "cgroup.controllers").exists() and os.access(p, os.W_OK) else None
    # Auto: find our user@<uid>.service ancestor (systemd delegates it to us).
    try:
        rel = Path("/proc/self/cgroup").read_text().strip().split("::", 1)[1]
    except (OSError, IndexError):
        return None
    cur = _CGROOT / rel.lstrip("/")
    target = f"user@{os.getuid()}.service"
    for anc in [cur, *cur.parents]:
        if anc == _CGROOT:
            break
        if anc.name == target and os.access(anc, os.W_OK):
            ctrls = (anc / "cgroup.controllers")
            if ctrls.exists() and "memory" in ctrls.read_text().split():
                return anc
    return None


def _write(path: Path, value: str) -> bool:
    try:
        path.write_text(value)
        return True
    except OSError:
        return False


class SessionCgroup:
    """A per-session cgroup; cleanup() removes it. None of these raise."""

    def __init__(self, path: Path) -> None:
        self.path = path

    @property
    def procs_file(self) -> str:
        return str(self.path / "cgroup.procs")

    @classmethod
    def create(
        cls, session_id: str, *, memory_max: Optional[int], pids_max: int, cpu_cores: Optional[float]
    ) -> Optional["SessionCgroup"]:
        if not (enabled() and _is_cgroup2()):
            return None
        parent = _delegated_parent()
        if parent is None:
            log.warning("HOLOBENCH_CGROUP set but no writable delegated cgroup parent found; skipping")
            return None
        try:
            # Intermediate 'holobench' cgroup, controllers enabled for its children.
            hb = parent / "holobench"
            hb.mkdir(exist_ok=True)
            avail = (hb / "cgroup.controllers").read_text().split()
            want = " ".join(f"+{c}" for c in ("memory", "pids", "cpu") if c in avail)
            if want:
                _write(hb / "cgroup.subtree_control", want)
            cg = hb / session_id
            cg.mkdir(exist_ok=True)
        except OSError as exc:
            log.warning("could not create session cgroup: %s", exc)
            return None

        controllers = set((cg / "cgroup.controllers").read_text().split()) if (cg / "cgroup.controllers").exists() else set()
        applied = []
        if memory_max and "memory" in controllers and _write(cg / "memory.max", str(memory_max)):
            applied.append(f"memory.max={memory_max // (1024*1024)}M")
        if "pids" in controllers and _write(cg / "pids.max", str(pids_max)):
            applied.append(f"pids.max={pids_max}")
        if cpu_cores and "cpu" in controllers:
            quota = int(cpu_cores * 100000)
            if _write(cg / "cpu.max", f"{quota} 100000"):
                applied.append(f"cpu={cpu_cores}core")
        log.info("session cgroup %s: %s", cg, ", ".join(applied) or "(no controllers applied)")
        return cls(cg)

    def cleanup(self) -> None:
        try:
            self.path.rmdir()  # empty once QEMU has exited
        except OSError:
            pass

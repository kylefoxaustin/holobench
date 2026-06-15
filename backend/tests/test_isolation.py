# SPDX-License-Identifier: GPL-2.0-or-later
"""Unit tests for the per-session cgroup isolation logic (no real cgroup needed)."""

import os

from holobench.session import isolation as iso


def test_parse_mem_to_bytes():
    assert iso._parse_mem_to_bytes("4G") == 4 * 1024**3
    assert iso._parse_mem_to_bytes("512M") == 512 * 1024**2
    assert iso._parse_mem_to_bytes("2048") == 2048
    assert iso._parse_mem_to_bytes("1.5G") == int(1.5 * 1024**3)
    assert iso._parse_mem_to_bytes("garbage") is None


def test_memory_max_from_profile(monkeypatch):
    monkeypatch.delenv("HOLOBENCH_MEM_CAP_MB", raising=False)
    # 4G x 1.5 + 512M margin
    assert iso.memory_max_bytes("4G") == int(4 * 1024**3 * 1.5) + 512 * 1024**2


def test_memory_max_env_override(monkeypatch):
    monkeypatch.setenv("HOLOBENCH_MEM_CAP_MB", "3000")
    assert iso.memory_max_bytes("4G") == 3000 * 1024 * 1024


def test_enabled_flag(monkeypatch):
    monkeypatch.delenv("HOLOBENCH_CGROUP", raising=False)
    monkeypatch.delenv("HOLOBENCH_CGROUP_PARENT", raising=False)
    assert iso.enabled() is False
    monkeypatch.setenv("HOLOBENCH_CGROUP", "1")
    assert iso.enabled() is True


def test_create_noop_when_disabled(monkeypatch):
    monkeypatch.delenv("HOLOBENCH_CGROUP", raising=False)
    monkeypatch.delenv("HOLOBENCH_CGROUP_PARENT", raising=False)
    cg = iso.SessionCgroup.create("sess-x", memory_max=1 << 30, pids_max=128, cpu_cores=2)
    assert cg is None  # disabled -> no cgroup, launch proceeds normally

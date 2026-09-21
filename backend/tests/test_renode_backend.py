# SPDX-License-Identifier: GPL-2.0-or-later
"""The Renode backend's guards.

⚠️ THIS FILE EXISTS BECAUSE THE RENODE BOARD WAS EXEMPT FROM EVERY GUARD IN THE SUITE.
The two QEMU-only invariants skip it by backend (correctly — it has no argv), and nothing
replaced them, so a whole backend rode along in a green suite with zero coverage. The boot
that proved it worked was a human running a terminal once.

⭐ THE FAKE MONITOR BELOW IS BUILT FROM MEASURED BYTES, NOT FROM AN IDEALISED MODEL.
Every framing quirk it reproduces was read off a live Renode 1.17.0 socket on 2026-09-21:
the telnet IAC preamble, the command echo, the "\\r\\r\\n"/"\\n\\r" mixture, the "(machine) "
prompt, and prose errors on an otherwise success-shaped response. A fake written from what
the protocol OUGHT to be would pass while the real one failed — which is the failure mode
this whole repo keeps finding, so it is the one thing this file must not do.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest

from holobench.profiles.loader import list_profiles, load_profile
from holobench.session.command import SessionRuntime, build_renode_script, CommandError
from holobench.session.manager import Session, UnsupportedVerb
from holobench.session.renode import (
    RenodeCaps,
    RenodeError,
    RenodeMonitor,
    strip_iac,
    _PROMPT,
)

RENODE_PROFILES = [p for p in ("imxrt1180-renode",)]


# ── the IAC stripper ────────────────────────────────────────────────────────────
def test_strip_iac_removes_the_measured_preamble():
    # The exact bytes Renode 1.17.0 sends on connect.
    raw = b"\xff\xfd\x00\xff\xfd\x1f\xff\xfb\x01\xff\xfb\x03\xff\xfc\x22enode, version"
    assert strip_iac(raw) == b"enode, version"


def test_strip_iac_handles_escaped_ff_subneg_and_dangling():
    assert strip_iac(b"a\xff\xffb") == b"a\xffb"            # IAC IAC -> literal 0xFF
    assert strip_iac(b"a\xff\xfa\x01\x02\xff\xf0b") == b"ab"  # subnegotiation
    assert strip_iac(b"ab\xff") == b"ab"                     # dangling IAC at a read edge
    assert strip_iac(b"plain text") == b"plain text"


def test_strip_iac_does_not_eat_high_bytes_that_are_not_iac():
    # 0xFE is WONT only AFTER an IAC; alone it is payload and must survive.
    assert strip_iac(b"\xfe\xfd\xfc") == b"\xfe\xfd\xfc"


# ── the prompt regex ────────────────────────────────────────────────────────────
def test_prompt_matches_both_measured_forms():
    assert _PROMPT.search("False\n(rt1180) ").group(1) == "rt1180"
    assert _PROMPT.search("(monitor) ").group(1) == "monitor"


def test_prompt_is_not_fooled_by_peripherals_output():
    """⭐ THE REGRESSION THIS EXISTS FOR. `peripherals` prints a class in parens on nearly
    every line — "sysbus (SystemBus)", "cpu (CortexM)". An early framing test that merely
    asked 'does the buffer end with )?' truncates that response mid-tree and returns a
    short, plausible, WRONG answer with no error anywhere."""
    for line in ("  sysbus (SystemBus)\n", "  cpu (CortexM)\n", "(SystemBus)",
                 "  ├── anadig (IMXRT1180_Anadig)\n"):
        assert not _PROMPT.search(line), line
    # and it must not match a prompt-shaped thing that is not at the end
    assert not _PROMPT.search("(rt1180) \nmore output follows")


# ── a fake Monitor speaking the measured protocol ───────────────────────────────
class FakeMonitor:
    """Reproduces the framing measured off Renode 1.17.0."""

    def __init__(self, machine: str = "rt1180", responses: dict[str, str] | None = None,
                 chunk: int = 17):
        self.machine = machine
        self.chunk = chunk
        self.responses = responses or {}
        self.received: list[str] = []
        self._server: asyncio.AbstractServer | None = None
        self.port = 0

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._client, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self.port

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    def _prompt(self) -> bytes:
        return f"({self.machine}) ".encode()

    async def _client(self, r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        # the measured preamble, IAC and all, with the banner's missing first character
        w.write(b"\xff\xfd\x00\xff\xfd\x1f\xff\xfb\x01\xff\xfb\x03\xff\xfc\x22"
                b"enode, version 1.17.0\n\r\n\r" + self._prompt())
        await w.drain()
        while True:
            line = await r.readline()
            if not line:
                return
            cmd = line.decode().strip()
            self.received.append(cmd)
            if cmd == "quit":
                w.write(b"quit\n\rRenode is quitting\r\n\r")
                await w.drain()
                w.close()
                return
            if cmd.startswith("mach set"):
                self.machine = cmd.split('"')[1] if '"' in cmd else self.machine
            body = self.responses.get(cmd)
            if body is None:
                body = f"No such command or device: {cmd.split(' ')[0]}"
            # echo, then body, then prompt — with the measured line-ending mixture
            payload = (cmd.encode() + b"\n\r"
                       + body.encode().replace(b"\n", b"\r\n\r")
                       + b"\r\r\n" + self._prompt())
            # ⭐ WRITTEN IN SMALL CHUNKS ON PURPOSE, AND THIS IS LOAD-BEARING. A fake that
            # writes the whole response in one go can never put a read boundary in the
            # middle of the output — so a framing bug that ends the response early at an
            # inner ")" would be invisible and the truncation test would be INERT. It was,
            # until a planted regex proved the test could not fail. Chunking reproduces
            # the real socket, where a 56-line device tree arrives in pieces.
            for i in range(0, len(payload), self.chunk):
                w.write(payload[i:i + self.chunk])
                await w.drain()
                await asyncio.sleep(0)


async def _connected(fake: FakeMonitor) -> RenodeMonitor:
    m = RenodeMonitor(port=fake.port, machine_name=fake.machine)
    await m.connect(timeout=5)
    return m


def test_monitor_strips_echo_and_prompt():
    asyncio.run(_test_monitor_strips_echo_and_prompt())


async def _test_monitor_strips_echo_and_prompt():
    fake = FakeMonitor(responses={"machine IsPaused": "False"})
    await fake.start()
    m = await _connected(fake)
    assert await m.execute("machine IsPaused") == "False"
    assert fake.received == ["machine IsPaused"]
    await m.close(); await fake.stop()


def test_monitor_raises_on_renode_prose_errors():
    asyncio.run(_test_monitor_raises_on_renode_prose_errors())


async def _test_monitor_raises_on_renode_prose_errors():
    """⚠️ RENODE HAS NO ERROR CHANNEL. A refusal is prose on a success-shaped response,
    so a client that does not match the text returns '' and the caller reads it as an
    empty success."""
    fake = FakeMonitor()
    await fake.start()
    m = await _connected(fake)
    with pytest.raises(RenodeError, match="No such command or device"):
        await m.execute("bogus_verb")
    await m.close(); await fake.stop()


def test_monitor_does_not_truncate_a_multiline_tree():
    asyncio.run(_test_monitor_does_not_truncate_a_multiline_tree())


async def _test_monitor_does_not_truncate_a_multiline_tree():
    """The peripherals regression, end to end through the real framing."""
    tree = "\n".join(["Available peripherals:", "", "  sysbus (SystemBus)",
                      "  ├── cpu (CortexM)", "  └── lpuart1 (IMXRT_LPUART)"])
    # ⚠️ chunk=1 IS THE TEST. At 17 bytes a read boundary only lands after an inner ")"
    # by luck, and a planted loose-prompt regex slipped straight through this test while
    # the unit test caught it. Byte-at-a-time GUARANTEES the buffer is inspected at the
    # exact moment it ends in "(SystemBus)" — the state a naive prompt match mistakes for
    # a finished response.
    fake = FakeMonitor(responses={"peripherals": tree}, chunk=1)
    await fake.start()
    m = await _connected(fake)
    got = await m.device_tree()
    assert "lpuart1 (IMXRT_LPUART)" in got, "response truncated at an inner paren"
    assert got.count("\n") >= 4
    await m.close(); await fake.stop()


def test_is_paused_refuses_a_non_boolean_answer():
    asyncio.run(_test_is_paused_refuses_a_non_boolean_answer())


async def _test_is_paused_refuses_a_non_boolean_answer():
    fake = FakeMonitor(responses={"machine IsPaused": "Maybe"})
    await fake.start()
    m = await _connected(fake)
    with pytest.raises(RenodeError, match="not True/False"):
        await m.is_paused()
    await m.close(); await fake.stop()


def test_query_status_reports_qmp_shape():
    asyncio.run(_test_query_status_reports_qmp_shape())


async def _test_query_status_reports_qmp_shape():
    fake = FakeMonitor(responses={"machine IsPaused": "True"})
    await fake.start()
    m = await _connected(fake)
    st = await m.query_status()
    assert st == {"running": False, "status": "paused", "singlestep": False}
    await m.close(); await fake.stop()


def test_uptime_refuses_when_virtual_time_is_absent():
    asyncio.run(_test_uptime_refuses_when_virtual_time_is_absent())


async def _test_uptime_refuses_when_virtual_time_is_absent():
    fake = FakeMonitor(responses={"currentTime": "Current real time: 00:00:05"})
    await fake.start()
    m = await _connected(fake)
    with pytest.raises(RenodeError, match="no virtual time"):
        await m.uptime()
    await m.close(); await fake.stop()


def test_no_prompt_raises_rather_than_hanging():
    asyncio.run(_test_no_prompt_raises_rather_than_hanging())


async def _test_no_prompt_raises_rather_than_hanging():
    """A Monitor that never prompts must time out with the bytes it did get, not block."""
    async def silent(r, w):
        w.write(b"some output but no prompt")
        await w.drain()
        await asyncio.sleep(30)
    srv = await asyncio.start_server(silent, "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]
    m = RenodeMonitor(port=port)
    with pytest.raises(RenodeError, match="no prompt"):
        await m.connect(timeout=1.5)
    srv.close(); await srv.wait_closed()


def test_snapshot_load_is_refused_with_the_console_reason():
    asyncio.run(_test_snapshot_load_is_refused_with_the_console_reason())


async def _test_snapshot_load_is_refused_with_the_console_reason():
    """⭐ THE REFUSAL IS THE FEATURE. `Load` works in Renode; what it does is kill the
    socket terminal (MEASURED: ConnectionRefusedError after, accepted before). If someone
    later flips this on, this test must be the thing that makes them explain why."""
    fake = FakeMonitor()
    await fake.start()
    m = await _connected(fake)
    with pytest.raises(RenodeError, match="kills the guest console"):
        await m.load_snapshot("/tmp/whatever.bin")
    assert RenodeCaps().snapshot_load is False
    await m.close(); await fake.stop()


# ── script rendering ────────────────────────────────────────────────────────────
def _rt(tmp_path: Path, port: int | None) -> SessionRuntime:
    rt = SessionRuntime(work_dir=tmp_path, qmp_socket=tmp_path / "q.sock")
    rt.renode_console_port = port
    return rt


@pytest.mark.parametrize("pid", RENODE_PROFILES)
def test_script_orders_the_two_hooks_around_mach_create(tmp_path, pid):
    """⚠️ THE ORDERING BUG THAT ONLY RUNNING IT FOUND. `sysbus Redirect` before
    `mach create` gets "No such command or device: sysbus" — sysbus addresses a machine
    that does not exist until LoadPlatformDescription runs."""
    p = load_profile(pid)
    lines = build_renode_script(p, _rt(tmp_path, 12345)).splitlines()
    i_create = lines.index(next(l for l in lines if l.startswith("mach create")))
    i_plat = lines.index(next(l for l in lines if "LoadPlatformDescription" in l))
    for pre in p.renode.pre_commands:
        assert lines.index(pre) < i_create, f"pre_command {pre!r} must precede mach create"
    for post in p.renode.post_platform:
        assert lines.index(post) > i_plat, f"post_platform {post!r} must follow the platform"


@pytest.mark.parametrize("pid", RENODE_PROFILES)
def test_console_terminal_is_never_telnet_mode(tmp_path, pid):
    """⚠️ telnetMode DEFAULTS TO TRUE and would prefix the guest stream with IAC bytes,
    destroying the byte-exactness that makes a QEMU-vs-Renode comparison mean anything."""
    script = build_renode_script(load_profile(pid), _rt(tmp_path, 12345))
    term = [l for l in script.splitlines() if "CreateServerSocketTerminal" in l]
    assert term, "no socket terminal rendered"
    for l in term:
        assert l.rstrip().endswith(" false"), f"telnetMode not disabled: {l!r}"


@pytest.mark.parametrize("pid", RENODE_PROFILES)
def test_autostart_is_off_for_sessions_and_on_for_humans(tmp_path, pid):
    p = load_profile(pid)
    assert build_renode_script(p, _rt(tmp_path, 1234)).splitlines()[-1] == "start"
    assert "start" not in build_renode_script(
        p, _rt(tmp_path, 1234), autostart=False).splitlines()


@pytest.mark.parametrize("pid", RENODE_PROFILES)
def test_a_missing_console_port_is_announced_not_dropped(tmp_path, pid):
    """⚠️ A bare `if port:` silently removed two lines, and `holobench command` — whose
    job is showing what will run — printed a console-less script with no hint."""
    script = build_renode_script(load_profile(pid), _rt(tmp_path, None))
    assert "CreateServerSocketTerminal" not in script
    assert "no console" in script and "allocates a" in script


def test_session_renders_the_script_with_autostart_disabled(monkeypatch, tmp_path):
    """⭐ STRUCTURAL, AND HONESTLY LABELLED AS SUCH. Renode buffers, so no behavioural test
    can tell autostart=True from autostart=False. This asserts the call is made the way
    _launch_renode documents — the only claim that is actually checkable here."""
    import holobench.session.manager as mgr
    seen: dict = {}
    real = mgr.build_renode_script

    def spy(profile, rt, **kw):
        seen.update(kw)
        return real(profile, rt, **kw)

    monkeypatch.setattr(mgr, "build_renode_script", spy)
    s = Session(load_profile("imxrt1180-renode"), base_dir=tmp_path)
    s.work_dir.mkdir(parents=True, exist_ok=True)
    # Spawn will fail on a bogus binary; we only care that the render happened first.
    s.profile.renode.binary = "/nonexistent/renode"
    try:
        asyncio.run(s.launch(qmp_timeout=1))
    except Exception:
        pass
    assert seen.get("autostart") is False, (
        "Session must render with autostart=False; got " + repr(seen))


def test_qom_endpoint_does_not_dress_a_renode_tree_as_qmp_children(monkeypatch):
    """⚠️ THE API WRAPPER TOLD TWO LIES AT ONCE. It returned
    {"path": "/machine", "children": <the renode dict>} — claiming the tree was scoped to
    /machine (Renode ignores the argument entirely) and that "children" was a child list a
    client could iterate (it is one flat text blob). Found by reading a LIVE response, not
    by a test, which is why there is now a test — and it exercises the endpoint rather than
    grepping its source, because the first version of this test grepped and was simply
    wrong about where the source lived."""
    import importlib
    # ⚠️ import_module, NOT `import holobench.api.app as mod`. The package's __init__
    # re-exports the FastAPI instance as `app`, so the attribute path `holobench.api.app`
    # resolves to the OBJECT and attribute lookups on it fail with a confusing
    # "'FastAPI' object has no attribute ...". import_module returns the module.
    mod = importlib.import_module("holobench.api.app")

    class _Stub:
        async def qom_list(self, path="/machine"):
            return {"backend": "renode", "path": None, "tree": "Available peripherals:\n"}

    class _StubQemu:
        async def qom_list(self, path="/machine"):
            return ["child[0]", "child[1]"]

    monkeypatch.setattr(mod, "_get_session", lambda sid: _Stub())
    got = asyncio.run(mod.introspect_qom("sid"))
    assert got["backend"] == "renode" and got["path"] is None
    assert "children" not in got, f"renode tree wrapped in QMP's shape: {got}"

    # and the QEMU shape must be untouched
    monkeypatch.setattr(mod, "_get_session", lambda sid: _StubQemu())
    got = asyncio.run(mod.introspect_qom("sid"))
    assert got == {"path": "/machine", "children": ["child[0]", "child[1]"]}


def test_build_command_refuses_a_renode_profile_by_name(tmp_path):
    p = load_profile("imxrt1180-renode")
    from holobench.session.command import build_command
    with pytest.raises(CommandError, match="renode"):
        build_command(p, _rt(tmp_path, None))


# ── profile model invariants ────────────────────────────────────────────────────
def test_every_profile_declares_exactly_one_backend():
    checked = 0
    for entry in list_profiles():
        pid = entry.id if hasattr(entry, "id") else entry
        p = load_profile(pid)
        checked += 1
        assert p.backend in ("qemu", "renode")
        assert (p.qemu is not None) == (p.backend == "qemu")
        assert (p.renode is not None) == (p.backend == "renode")
    assert checked >= 20, f"only {checked} profiles seen"


def test_a_profile_with_both_or_neither_backend_is_refused():
    from holobench.profiles.models import Profile
    import pydantic
    base = {"id": "x", "display_name": "x", "soc": "x", "description": "x",
            "serial": [{"name": "c", "chardev": "console0", "default": True}]}
    with pytest.raises(pydantic.ValidationError):
        Profile(**base)                                   # neither
    with pytest.raises(pydantic.ValidationError):
        Profile(**base, qemu={"binary": "/q", "machine": "m"},
                renode={"platform": "/p.repl"})           # both


# ── capabilities / refusal ──────────────────────────────────────────────────────
def test_renode_session_refuses_qemu_only_verbs_before_launch():
    """⭐ CAPABILITIES MUST BE HONEST WITHOUT A RUNNING BOARD, because the UI asks before
    it renders. And a capability that says yes must not then fail: snapshot_save is True
    for Renode and must NOT route through hmp(), which Renode refuses."""
    s = Session(load_profile("imxrt1180-renode"))
    caps = s.capabilities()
    assert caps["screendump"] is False and caps["hmp"] is False
    assert caps["snapshot_load"] is False
    assert caps["uptime"] is True and caps["snapshot_save"] is True
    assert s.backend == "renode"
    for verb in ("screendump", "hmp", "snapshot_load"):
        with pytest.raises(UnsupportedVerb, match=verb):
            s._require(verb)


def test_qemu_session_keeps_its_own_capabilities():
    s = Session(load_profile("imx95-evk"))
    caps = s.capabilities()
    assert s.backend == "qemu"
    assert caps["hmp"] is True and caps["events"] is True
    assert caps["uptime"] is False          # QEMU has no QMP equivalent — say so


def test_renode_session_allocates_tcp_not_unix_transports():
    s = Session(load_profile("imxrt1180-renode"))
    assert s._renode_monitor_port and s.runtime.renode_console_port
    assert s._renode_monitor_port != s.runtime.renode_console_port
    for sock in s.runtime.serial_sockets.values():
        assert str(sock).startswith("127.0.0.1:"), sock


# ── the real thing, when it is installed ────────────────────────────────────────
_RENODE = load_profile("imxrt1180-renode").renode.binary
_HAVE_RENODE = Path(_RENODE).exists() or shutil.which("renode") is not None
_ARTIFACTS = all(Path(x).exists() for x in (
    load_profile("imxrt1180-renode").renode.platform,
    load_profile("imxrt1180-renode").renode.firmware or "/nonexistent"))


@pytest.mark.skipif(not (_HAVE_RENODE and _ARTIFACTS),
                    reason="Renode and/or the RT1180 artifacts are not on this machine")
def test_e2e_renode_board_boots_and_the_console_catches_the_first_byte():
    asyncio.run(_test_e2e_renode_board_boots_and_the_console_catches_the_first_byte())


async def _test_e2e_renode_board_boots_and_the_console_catches_the_first_byte():
    """The board boots, the console carries the guest's bytes, and quit() leaves no corpse.

    ⚠️ THIS TEST DOES NOT PROVE THE autostart=False ORDERING, and an earlier version of
    this docstring claimed it did. Planting `autostart=True` left the suite GREEN — because
    Renode's socket terminal buffers, so the banner survives a late attach either way. The
    ordering is asserted structurally instead, in
    test_session_renders_the_script_with_autostart_disabled."""
    s = Session(load_profile("imxrt1180-renode"))
    try:
        await s.launch(qmp_timeout=60)
        assert (await s.query_status())["running"] is True
        await asyncio.sleep(2.5)
        log = s.console_log()
        assert log is not None and log.exists()
        assert b"hello world" in log.read_bytes(), "guest output never reached the tap"
        assert "virtual" in await s.uptime()
        await s.pause()
        assert (await s.query_status())["status"] == "paused"
        await s.resume()
        snap = await s.snapshot_save("pytest")
        assert Path(snap).stat().st_size > 0
    finally:
        pid = s.pid
        await s.quit()
        s.cleanup()
        if pid:
            assert subprocess.run(["kill", "-0", str(pid)],
                                  capture_output=True).returncode != 0, \
                f"Renode pid {pid} survived quit()"

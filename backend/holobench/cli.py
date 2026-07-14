# SPDX-License-Identifier: GPL-2.0-or-later
"""Holobench CLI.

Phase 0 surface: list/show profiles, preview the resolved QEMU command line,
and `launch` a board to prove QMP control end-to-end (query-status +
system_reset + quit). No web UI yet — that arrives in later phases.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Optional

from .labs import LabCoordinator, LabError, list_labs, load_lab
from .profiles import ProfileError, list_profiles, load_profile
from .profiles.loader import default_asset_dir
from .session import SessionError, build_command, command_str
from .session import control
from .session.command import SessionRuntime
from .session.manager import Session, SessionManager


def _print_err(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)


def _resolve_assets(profile_id: str, explicit: Optional[str]) -> Optional[Path]:
    """Asset dir for boot artifacts: explicit --assets, else assets/<id>/."""
    if explicit:
        return Path(explicit)
    return default_asset_dir(profile_id)


def cmd_profiles(_args: argparse.Namespace) -> int:
    ids = list_profiles()
    if not ids:
        print("(no profiles found)")
        return 0
    for pid in ids:
        try:
            p = load_profile(pid)
            print(f"{pid:16}  {p.display_name:20}  machine={p.qemu.machine}")
        except ProfileError as exc:
            print(f"{pid:16}  <invalid: {exc.__class__.__name__}>")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    try:
        p = load_profile(args.id)
    except ProfileError as exc:
        _print_err(str(exc))
        return 1
    print(json.dumps(p.model_dump(mode="json"), indent=2))
    return 0


def cmd_command(args: argparse.Namespace) -> int:
    """Preview the resolved argv without launching anything."""
    try:
        p = load_profile(args.id)
    except ProfileError as exc:
        _print_err(str(exc))
        return 1
    work = Path("/tmp/holobench") / f"{p.id}-preview"
    fi = p.file_injection
    rt = SessionRuntime(
        work_dir=work,
        qmp_socket=work / "qmp.sock",
        serial_sockets={port.chardev: work / f"{port.chardev}.sock" for port in p.serial},
        asset_dir=_resolve_assets(p.id, args.assets),
        share_dir=work / "share" if (fi.nine_p.enabled or fi.tftp.enabled) else None,
    )
    print(command_str(build_command(p, rt)))
    return 0


async def _launch(args: argparse.Namespace) -> int:
    try:
        profile = load_profile(args.id)
    except ProfileError as exc:
        _print_err(str(exc))
        return 1

    asset_dir = _resolve_assets(profile.id, args.assets)
    # --keep promises the board OUTLIVES the CLI, so it must NOT be reaped with the parent.
    session = Session(profile, asset_dir=asset_dir, reap_with_parent=not args.keep)
    if asset_dir:
        print(f"assets:    {asset_dir}")
    print(f"launching: {profile.display_name}  (session {session.id})")
    print(f"command:   {command_str(build_command(profile, session.runtime))}")

    try:
        await session.launch()
    except SessionError as exc:
        _print_err(str(exc))
        # Surface the tail of the QEMU log to explain the failure.
        log = session.work_dir / "qemu.log"
        if log.exists():
            tail = log.read_text(errors="replace").splitlines()[-15:]
            if tail:
                print("--- qemu.log (tail) ---", file=sys.stderr)
                print("\n".join(tail), file=sys.stderr)
        return 1

    print(f"running:   pid={session.pid}  qmp={session.runtime.qmp_socket}")

    rc = 0
    try:
        status = await session.query_status()
        print(f"query-status: {json.dumps(status)}")

        if not args.no_reset:
            print("system_reset ...")
            await session.system_reset()
            await asyncio.sleep(0.3)
            status = await session.query_status()
            print(f"query-status: {json.dumps(status)}  (after reset)")

        if args.hold:
            clog = session.console_log()
            # Tap the console socket so --hold actually CAPTURES the boot. QEMU's
            # serial chardev is a listening socket with no reader by default, so its
            # output is dropped (the app's WS bridge is what normally taps it) — the
            # CLI has to run its own SerialTap or the log stays empty.
            tap = None
            dp = profile.default_serial
            if clog and dp:
                from .bridges.console import SerialTap
                sock = session.work_dir / f"{dp.chardev}.sock"
                tap = SerialTap(sock, clog)
                try:
                    await tap.start(connect_timeout=10.0)
                except Exception as exc:  # never fail the launch over the tap
                    print(f"(console tap unavailable: {exc})", file=sys.stderr)
                    tap = None
            if clog:
                print(f"console:   {clog}")
            print(f"holding for {args.hold}s (Ctrl-C to stop early) ...")
            try:
                await asyncio.sleep(args.hold)
            except asyncio.CancelledError:
                pass
            if tap is not None:
                await tap.stop()
            if clog and clog.exists() and not args.quiet_console:
                tail = clog.read_text(errors="replace").splitlines()[-args.console_lines:]
                print(f"--- console tail ({len(tail)} lines) ---")
                print("\n".join(tail))
                print("--- end console ---")
    except SessionError as exc:
        _print_err(str(exc))
        rc = 1
    finally:
        if args.keep:
            print(f"--keep: leaving session running. work dir: {session.work_dir}")
        else:
            print("quitting ...")
            await session.quit()
            session.cleanup()
            print("done.")
    return rc


def cmd_launch(args: argparse.Namespace) -> int:
    try:
        return asyncio.run(_launch(args))
    except KeyboardInterrupt:
        return 130


def cmd_console(args: argparse.Namespace) -> int:
    """Launch a board with host-terminal access — each UART as a PTY (attach
    PuTTY -serial / screen / minicom) plus an SSH port-forward, the way a
    developer consoles into a real EVK. Reuses the normal board command line
    (all standard QEMU interfaces); only swaps the serial backend to PTYs and
    adds a stock SSH-forward NIC. Holds until Ctrl-C, then tears down."""
    import re
    import shutil
    import subprocess
    import time

    try:
        p = load_profile(args.id)
    except ProfileError as exc:
        _print_err(str(exc))
        return 1

    asset_dir = _resolve_assets(p.id, args.assets)
    work = Path("/tmp/holobench") / f"{p.id}-console"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)

    fi = p.file_injection
    share_dir = None
    if fi.nine_p.enabled or fi.tftp.enabled:
        share_dir = work / "share"
        share_dir.mkdir(exist_ok=True)

    # COW overlay over the golden disk (the golden is never touched) for disk boards.
    disk_overlay = None
    art = p.boot.artifacts
    if fi.image_swap.enabled and art.disk:
        golden = Path(art.disk)
        if not golden.is_absolute() and asset_dir is not None:
            golden = asset_dir / art.disk
        if not golden.exists():
            _print_err(f"golden disk not found: {golden}")
            return 1
        disk_overlay = work / "console-overlay.qcow2"
        try:
            subprocess.run(
                ["qemu-img", "create", "-f", "qcow2", "-b", str(golden),
                 "-F", "raw", str(disk_overlay)],
                check=True, capture_output=True, text=True,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            _print_err(f"could not create disk overlay: {exc}")
            return 1

    ssh_port = None if args.no_ssh else args.ssh_port
    rt = SessionRuntime(
        work_dir=work,
        qmp_socket=work / "qmp.sock",
        serial_sockets={},
        asset_dir=asset_dir,
        share_dir=share_dir,
        disk_overlay=disk_overlay,
        external_console=True,
        ssh_forward_port=ssh_port,
    )
    try:
        argv = build_command(p, rt)
    except Exception as exc:  # noqa: BLE001 - surface any command-build failure
        _print_err(str(exc))
        return 1

    log_path = work / "qemu.log"
    print(f"launching {p.display_name} for external console access ...", flush=True)
    with open(log_path, "wb") as log:
        proc = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT)

    # Map each declared UART's chardev label -> the /dev/pts QEMU assigned it.
    want = [port.chardev for port in p.serial]
    pts: dict[str, str] = {}
    rx = re.compile(r"char device redirected to (\S+) \(label (\S+)\)")
    deadline = time.time() + 15
    while time.time() < deadline and len(pts) < len(want):
        if proc.poll() is not None:
            _print_err(f"QEMU exited early (rc={proc.returncode}); see {log_path}")
            return 1
        try:
            text = log_path.read_text(errors="ignore")
        except OSError:
            text = ""
        for dev, label in rx.findall(text):
            if label in want:
                pts[label] = dev
        if len(pts) < len(want):
            time.sleep(0.3)

    by_id = {port.chardev: port for port in p.serial}
    print()
    print(f"=== {p.display_name} — console access  (session dir: {work}) ===")
    for label in want:
        dev = pts.get(label, "<pty not reported — see qemu.log>")
        port = by_id[label]
        role = port.name + (f"  [{port.role}]" if port.role else "")
        star = "  (default console)" if port.default else ""
        print(f"  serial  {role}{star}")
        print(f"          PuTTY : putty -serial {dev} -sercfg 115200,8,n,1,N")
        print(f"          screen: screen {dev} 115200")
        print(f"          log   : {work / (label + '.log')}  (full boot, even before you attach)")
    if ssh_port:
        print(f"  ssh     host port {ssh_port} -> guest :22")
        print(f"          PuTTY : putty -ssh -P {ssh_port} root@127.0.0.1")
        print(f"          ssh   : ssh -p {ssh_port} root@127.0.0.1")
        print("          (one-time in guest if fresh image: passwd root; ssh-keygen -A;")
        print("           systemctl start sshd.socket)")
    print()
    print("Ctrl-C to shut down and clean up." if args.seconds <= 0
          else f"Auto-stops after {args.seconds:g}s (or Ctrl-C).")
    sys.stdout.flush()  # the block must reach the user before we block on wait()

    try:
        proc.wait(timeout=args.seconds if args.seconds > 0 else None)
    except subprocess.TimeoutExpired:
        pass
    except KeyboardInterrupt:
        print("\nshutting down ...")
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        if disk_overlay is not None:
            disk_overlay.unlink(missing_ok=True)
    return 0


# --- session-scoped verbs (act on a running session by id, no daemon) -------


def cmd_ps(_args: argparse.Namespace) -> int:
    sessions = control.list_sessions()
    if not sessions:
        print("(no running sessions)")
        return 0
    for sid in sessions:
        print(sid)
    return 0


def _run_session_verb(args: argparse.Namespace, coro_fn, label: str) -> int:
    async def go() -> int:
        try:
            result = await coro_fn(args.session)
        except control.ControlError as exc:
            _print_err(str(exc))
            return 1
        if result is not None:
            print(json.dumps(result))
        else:
            print(f"{label}: {args.session}")
        return 0

    try:
        return asyncio.run(go())
    except KeyboardInterrupt:
        return 130


def cmd_status(args: argparse.Namespace) -> int:
    return _run_session_verb(args, control.status, "status")


def cmd_reset(args: argparse.Namespace) -> int:
    return _run_session_verb(args, control.reset, "reset (system_reset)")


def cmd_pause(args: argparse.Namespace) -> int:
    return _run_session_verb(args, control.pause, "paused")


def cmd_resume(args: argparse.Namespace) -> int:
    return _run_session_verb(args, control.resume, "resumed")


def cmd_stop(args: argparse.Namespace) -> int:
    return _run_session_verb(args, control.stop, "stopped (quit)")


def cmd_labs(_args: argparse.Namespace) -> int:
    ids = list_labs()
    if not ids:
        print("(no labs found)")
        return 0
    for lid in ids:
        try:
            lab = load_lab(lid)
            # link types present, in a stable order (eth/usb/uart/spi/can)
            order = ["eth", "usb", "uart", "spi", "can"]
            kinds = sorted({l.type for l in lab.links}, key=lambda t: order.index(t) if t in order else 99)
            links = f"  [{'+'.join(kinds)}]" if kinds else ""
            print(f"{lid:16}  {lab.display_name:46}  {len(lab.nodes)} nodes{links}")
        except LabError as exc:
            print(f"{lid:16}  <invalid: {exc}>")
    return 0


def cmd_lab_show(args: argparse.Namespace) -> int:
    try:
        lab = load_lab(args.id)
    except LabError as exc:
        _print_err(str(exc))
        return 1
    print(json.dumps(lab.model_dump(), indent=2))
    return 0


async def _lab_launch(args: argparse.Namespace) -> int:
    try:
        lab = load_lab(args.id)
    except LabError as exc:
        _print_err(str(exc))
        return 1
    mgr = SessionManager()
    coord = LabCoordinator(mgr)
    print(f"launching lab: {lab.display_name}  ({len(lab.nodes)} nodes)")
    auto_ip = not getattr(args, "no_auto_ip", False)
    if lab.is_staggered:
        print(f"schedule: this lab tests TIME — horizon {lab.horizon_s:.0f}s "
              f"(arrivals staggered; a node departs early)")
    try:
        running = await coord.launch(
            lab, auto_ip=auto_ip,
            on_event=lambda m: print(m, flush=True),   # arrivals/departures, live
        )
    except LabError as exc:
        _print_err(str(exc))
        return 1
    print(f"lab state: {running.state.value}")
    for node in lab.nodes:
        sid = running.node_sessions.get(node.name)
        if sid:
            s = mgr.get(sid)
            ip = running.node_ips.get(node.name)
            iptag = f"  ip={ip}" if ip else ""
            print(f"  {node.name:8} {node.profile:16} pid={s.pid} {s.state.value}{iptag}")
        else:
            print(f"  {node.name:8} {node.profile:16} FAILED: {running.node_errors.get(node.name)}")
    if running.node_ips:
        print("auto-IP: eth nodes are pre-configured on their segment (kernel ip=), ready to ping.")
    elif any(l.type == "eth" for l in lab.links):
        print("no auto-IP (--no-auto-ip): eth nodes come up with link-up but NO address —")
        print("  assign one in-console, e.g.  ip addr add 10.0.0.1/24 dev eth0 && ip link set eth0 up")
    rc = 0
    # A staggered lab observed for less than its horizon has not been RUN — it has been
    # INTERRUPTED. The scheduled departure is the last event and the whole point, so
    # default the hold to the remaining horizon rather than quietly exiting before it.
    hold = args.hold
    if not hold and lab.is_staggered and running.t0 is not None:
        remaining = lab.horizon_s - (asyncio.get_running_loop().time() - running.t0)
        hold = max(0, int(remaining)) + 30
        print(f"(no --hold given; holding {hold}s to reach this lab's horizon — "
              f"the scheduled departure is the point)")
    try:
        if hold:
            print(f"holding for {hold}s (Ctrl-C to stop early) ...")
            try:
                await asyncio.sleep(hold)
            except asyncio.CancelledError:
                pass
    finally:
        if args.keep:
            print("--keep: leaving lab running.")
        else:
            print("stopping lab ...")
            await coord.stop(lab.id)
            print("done.")
    return rc


def cmd_lab_launch(args: argparse.Namespace) -> int:
    try:
        return asyncio.run(_lab_launch(args))
    except KeyboardInterrupt:
        return 130


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    print(f"Holobench serving on http://{args.host}:{args.port}  (Ctrl-C to stop)")
    uvicorn.run("holobench.api.app:app", host=args.host, port=args.port, log_level="info")
    return 0


def cmd_user(args: argparse.Namespace) -> int:
    import getpass

    from .auth import UserStore
    from .auth.store import default_users_path

    store = UserStore()
    if args.user_cmd == "list":
        users = store.list()
        if not users:
            print(f"(no users — open mode; file: {default_users_path()})")
            return 0
        for u in users:
            print(f"{u.username:20} {u.role}")
        return 0
    if args.user_cmd == "add":
        pw = args.password or getpass.getpass(f"password for {args.name}: ")
        if not pw:
            _print_err("empty password")
            return 1
        store.add(args.name, pw, role="admin" if args.admin else "user")
        print(f"added {args.name} ({'admin' if args.admin else 'user'}) -> {store.path}")
        print("auth is now ENFORCED. Set HOLOBENCH_SECRET for stable logins across restarts.")
        return 0
    if args.user_cmd == "remove":
        print("removed" if store.remove(args.name) else f"no such user '{args.name}'")
        return 0
    _print_err("usage: holobench user {add|list|remove}")
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="holobench", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("profiles", help="list available board profiles")
    p.set_defaults(func=cmd_profiles)

    p = sub.add_parser("show", help="print a validated profile as JSON")
    p.add_argument("id")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("command", help="preview the resolved QEMU command line")
    p.add_argument("id")
    p.add_argument("--assets", help="asset dir for boot artifacts")
    p.set_defaults(func=cmd_command)

    p = sub.add_parser("launch", help="launch a board and prove QMP control")
    p.add_argument("id")
    p.add_argument("--assets", help="asset dir for boot artifacts")
    p.add_argument("--no-reset", action="store_true", help="skip the system_reset step")
    p.add_argument("--hold", type=float, default=0.0, help="stay running N seconds")
    p.add_argument("--console-lines", type=int, default=40, help="console tail lines after --hold")
    p.add_argument("--quiet-console", action="store_true", help="do not print console tail")
    p.add_argument("--keep", action="store_true", help="do not quit/cleanup at the end")
    p.set_defaults(func=cmd_launch)

    p = sub.add_parser("console",
                       help="launch a board with host-terminal access (PuTTY/screen serial + SSH)")
    p.add_argument("id")
    p.add_argument("--assets", help="asset dir for boot artifacts")
    p.add_argument("--ssh-port", type=int, default=2222,
                   help="host port forwarded to guest :22 (default 2222)")
    p.add_argument("--no-ssh", action="store_true", help="omit the SSH port-forward NIC")
    p.add_argument("--seconds", type=float, default=0.0,
                   help="auto-stop after N seconds (0 = hold until Ctrl-C)")
    p.set_defaults(func=cmd_console)

    p = sub.add_parser("labs", help="list available multi-board labs (v3.0 topologies)")
    p.set_defaults(func=cmd_labs)

    p = sub.add_parser("lab", help="launch/inspect a multi-board lab (v3.0 topology)")
    lsub = p.add_subparsers(dest="lab_cmd", required=True)
    lsub.add_parser("list", help="list available labs").set_defaults(func=cmd_labs)
    ls = lsub.add_parser("show", help="print a validated lab spec as JSON")
    ls.add_argument("id")
    ls.set_defaults(func=cmd_lab_show)
    ll = lsub.add_parser("launch", help="launch all nodes of a lab and wire the fabric")
    ll.add_argument("id")
    ll.add_argument("--hold", type=float, default=0.0, help="keep the lab up N seconds")
    ll.add_argument("--keep", action="store_true", help="do not stop the lab at the end")
    ll.add_argument("--no-auto-ip", action="store_true",
                    help="don't auto-assign eth IPs — boards come up link-only, you set them in-console")
    ll.set_defaults(func=cmd_lab_launch)

    p = sub.add_parser("serve", help="run the Holobench web backend (REST + console WS + UI)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8080)
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("user", help="manage farm users (auth)")
    usub = p.add_subparsers(dest="user_cmd", required=True)
    pa = usub.add_parser("add", help="add a user (enables enforced auth)")
    pa.add_argument("name")
    pa.add_argument("--admin", action="store_true", help="make this user an admin")
    pa.add_argument("--password", help="set password non-interactively (else prompt)")
    pa.set_defaults(func=cmd_user)
    usub.add_parser("list", help="list users").set_defaults(func=cmd_user)
    pr = usub.add_parser("remove", help="remove a user")
    pr.add_argument("name")
    pr.set_defaults(func=cmd_user)

    p = sub.add_parser("ps", help="list running sessions")
    p.set_defaults(func=cmd_ps)

    for name, fn, helptext in (
        ("status", cmd_status, "query-status of a running session"),
        ("reset", cmd_reset, "warm reset a session (QMP system_reset)"),
        ("pause", cmd_pause, "pause a session (QMP stop)"),
        ("resume", cmd_resume, "resume a session (QMP cont)"),
        ("stop", cmd_stop, "tear down a session (QMP quit)"),
    ):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("session", help="session id or unambiguous prefix")
        p.set_defaults(func=fn)

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

# SPDX-License-Identifier: Apache-2.0
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

from .profiles import ProfileError, list_profiles, load_profile
from .profiles.loader import default_asset_dir
from .session import SessionError, build_command, command_str
from .session import control
from .session.command import SessionRuntime
from .session.manager import Session


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
    session = Session(profile, asset_dir=asset_dir)
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
            if clog:
                print(f"console:   {clog}")
            print(f"holding for {args.hold}s (Ctrl-C to stop early) ...")
            try:
                await asyncio.sleep(args.hold)
            except asyncio.CancelledError:
                pass
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

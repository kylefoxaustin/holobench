# SPDX-License-Identifier: GPL-2.0-or-later
"""Holobench backend daemon — FastAPI app.

This is the long-lived process that owns the fleet (a SessionManager), exposes a
mediated REST + WebSocket API to the browser, and — critically — keeps the serial
taps alive for the lifetime of each session (unlike the one-shot CLI).

Security boundary (CLAUDE.md §6 / ARCHITECTURE.md §7): the browser only ever sees
profiles, session metadata, a fixed set of scoped control verbs, and terminal
byte streams. The QMP socket never leaves this process; there is no client path
to an arbitrary monitor command.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from urllib.parse import urlparse
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import (
    FastAPI,
    HTTPException,
    Request,
    Response,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..auth import AuthService
from ..profiles import ProfileError, list_profiles, load_profile
from ..profiles.loader import default_asset_dir
from ..labs import LabCoordinator, LabError, list_labs, load_lab
from ..setup import (SetupError, SetupManager, required_artifacts,
                     validate_manifest, nxp_manifest)
from ..session.manager import Session, SessionError, SessionManager

_FRONTEND_DIR = Path(__file__).resolve().parents[3] / "frontend"

# Quota/scheduler limits (0 = unlimited). Per-user concurrent sessions + global.
_MAX_SESSIONS_PER_USER = int(os.environ.get("HOLOBENCH_MAX_PER_USER", "0"))
_MAX_SESSIONS_TOTAL = int(os.environ.get("HOLOBENCH_MAX_SESSIONS", "0"))
# A client-supplied asset path would flow straight into the QEMU argv
# (-kernel/-dtb/-drive) -> arbitrary host-file read. Ignore it over the API by
# default; the server resolves assets from the trusted profile id. Only honor a
# request `assets` path if an operator explicitly opts in (trusted/CLI use).
_ALLOW_CLIENT_ASSETS = os.environ.get("HOLOBENCH_ALLOW_CLIENT_ASSETS") == "1"

# --- login brute-force throttle (in-memory, per ip+username) ---------------
_LOGIN_MAX_FAILS = int(os.environ.get("HOLOBENCH_LOGIN_MAX_FAILS", "5"))
_LOGIN_WINDOW_S = int(os.environ.get("HOLOBENCH_LOGIN_WINDOW_S", "60"))
_login_fails: dict[str, list[float]] = {}


def _login_throttled(key: str) -> bool:
    now = time.time()
    hits = [t for t in _login_fails.get(key, []) if now - t < _LOGIN_WINDOW_S]
    _login_fails[key] = hits
    return len(hits) >= _LOGIN_MAX_FAILS


def _login_record_fail(key: str) -> None:
    _login_fails.setdefault(key, []).append(time.time())


# --- audit log -------------------------------------------------------------
_audit_log = logging.getLogger("holobench.audit")
_AUDIT_FILE = os.environ.get("HOLOBENCH_AUDIT_LOG")


def _audit(event: str, user: str = "-", **fields) -> None:
    """Record a security/lifecycle event (who did what). Always logs; also
    appends a JSON line to $HOLOBENCH_AUDIT_LOG when set."""
    rec = {"ts": int(time.time()), "event": event, "user": user, **fields}
    line = json.dumps(rec, separators=(",", ":"), sort_keys=True)
    _audit_log.info("%s", line)
    if _AUDIT_FILE:
        try:
            with open(_AUDIT_FILE, "a") as f:
                f.write(line + "\n")
        except OSError:
            pass


def _origin_ok(headers) -> bool:
    """Reject cross-origin (CSWSH) browser connections. Non-browser clients send
    no Origin and pass. Override the allowlist with HOLOBENCH_ALLOWED_ORIGINS."""
    origin = headers.get("origin")
    if not origin:
        return True  # curl / native ws clients
    allowed = os.environ.get("HOLOBENCH_ALLOWED_ORIGINS")
    if allowed:
        return origin in {o.strip() for o in allowed.split(",") if o.strip()}
    return urlparse(origin).netloc == headers.get("host")

# Paths under /api that don't require authentication.
_OPEN_PATHS = {"/api/login", "/api/public-config", "/api/register"}

# Self-registration: when on, anyone may create a (role "user") account from the
# login screen. OFF by default (admins make users). Independent of onboarding: the
# FIRST account on a fresh instance can always be registered (becomes admin), so a
# first-timer can stand up auth from the UI with zero config.
_ALLOW_REGISTRATION = os.environ.get("HOLOBENCH_ALLOW_REGISTRATION") == "1"

# Optional demo credentials surfaced on the login screen so a first-time user can
# try the instance without hunting for a password. Format: "username:password".
# Unset (the default) = nothing shown — keep it unset for any real deployment.
_DEMO_LOGIN = os.environ.get("HOLOBENCH_DEMO_LOGIN") or None
_SESSION_PATH_RE = re.compile(r"^/api/sessions/([^/]+)(?:/|$)")


def _bootstrap_admin(auth: AuthService) -> None:
    """Seed an admin from env at startup so a container can turn auth ON without an
    exec + restart (the store is read once, here). HOLOBENCH_ADMIN_USER +
    HOLOBENCH_ADMIN_PASSWORD upsert that admin -> auth enabled -> login + Admin
    panel appear. No-op if either is unset (stays open-mode)."""
    u = os.environ.get("HOLOBENCH_ADMIN_USER")
    p = os.environ.get("HOLOBENCH_ADMIN_PASSWORD")
    if u and p:
        auth.store.add(u, p, role="admin")
        logging.getLogger("holobench").info(
            "bootstrapped admin user '%s' from env (auth enforced)", u)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.manager = SessionManager()
    app.state.labs = LabCoordinator(app.state.manager)
    app.state.setup = SetupManager()
    auth = AuthService()
    _bootstrap_admin(auth)
    # Re-init so the signing key matches the now-configured store (persistent
    # secret) rather than the ephemeral open-mode key created before the seed.
    if auth.enabled and not os.environ.get("HOLOBENCH_SECRET"):
        auth = AuthService(store=auth.store)
    app.state.auth = auth
    reaper = asyncio.create_task(app.state.manager.run_reaper())
    try:
        yield
    finally:
        reaper.cancel()
        try:
            await reaper
        except asyncio.CancelledError:
            pass
        await app.state.labs.shutdown_all()
        await app.state.manager.shutdown_all()


app = FastAPI(title="Holobench", version="0.0.0", lifespan=lifespan)
# Module-level ref so route handlers / websockets can reach the manager.
app_ref = app


def _request_token(request: Request) -> Optional[str]:
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    return request.cookies.get("hb_token") or request.query_params.get("token")


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """Auth + ownership gate for /api/* (the static UI stays open so it can load).

    Open mode (no users configured) lets everything through as a synthetic admin.
    Once users exist, /api/* requires a valid token, and /api/sessions/<id>/*
    additionally requires that the session belongs to the caller (admins bypass).
    """
    path = request.url.path
    if path.startswith("/api") and path not in _OPEN_PATHS:
        auth: AuthService = request.app.state.auth
        user = auth.resolve(_request_token(request))
        if user is None:
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
        request.state.user = user
        m = _SESSION_PATH_RE.match(path)
        if m and not user.is_admin:
            sess = request.app.state.manager.peek(m.group(1))
            if sess is not None and sess.owner not in (None, user.username):
                return JSONResponse({"detail": "not your session"}, status_code=403)
    return await call_next(request)


# --- models ----------------------------------------------------------------


class LaunchRequest(BaseModel):
    profile_id: str
    assets: Optional[str] = None
    minutes: Optional[int] = None  # reservation length; <=0 = infinite (no expiry)


class LaunchLabRequest(BaseModel):
    lab_id: str
    minutes: Optional[int] = None  # reservation applied to every node; <=0 = infinite


class SetupBuildRequest(BaseModel):
    board: str
    mode: str = "plan"            # "plan" | "bsp" | "demo"
    bsp_path: Optional[str] = None


class SnapshotRequest(BaseModel):
    name: str


class LoginRequest(BaseModel):
    username: str
    password: str


class UserCreateRequest(BaseModel):
    username: str
    password: str
    role: str = "user"


def _session_view(s: Session) -> dict:
    return {
        "id": s.id,
        "profile_id": s.profile.id,
        "display_name": s.profile.display_name,
        "soc": s.profile.soc,
        "state": s.state.value,
        "pid": s.pid,
        "owner": s.owner,
        "lab": ({"id": s.lab_id, "node": s.lab_node} if s.lab_id else None),
        "serial": [
            {"name": p.name, "chardev": p.chardev, "role": p.role, "default": p.default}
            for p in s.profile.serial
        ],
        "display": {
            "enabled": s.profile.display.enabled,
            "vnc": s.profile.display.vnc,
            # An attachable panel exists for this board -> the UI offers "Attach LCD".
            "attachable": bool(s.profile.display.attach_dtb),
            "attach_label": s.profile.display.attach_label,
            "lcd_attached": s.lcd_attached,
        },
        "file_injection": {
            "nine_p": s.profile.file_injection.nine_p.enabled,
            "tftp": s.profile.file_injection.tftp.enabled,
            "mount_tag": s.profile.file_injection.nine_p.mount_tag,
            "image_swap": (
                s.profile.file_injection.image_swap.enabled
                and s.runtime.disk_overlay is not None
            ),
        },
        "camera": {
            "enabled": s.profile.camera.enabled,
            "isi_type": s.profile.camera.isi_type,
            "width": s.profile.camera.width,
            "height": s.profile.camera.height,
            "bytes_per_pixel": s.profile.camera.bytes_per_pixel,
            "pixel_format": s.profile.camera.pixel_format,
            "frame_bytes": s.profile.camera.frame_bytes,
            "runtime_settable": s.profile.camera.runtime_settable,
            "capture_hint": s.profile.camera.capture_hint,
        },
        "introspection": {
            "memory_map": s.profile.introspection.memory_map,
            "device_tree": s.profile.introspection.device_tree,
            "qmp_events": s.profile.introspection.qmp_events,
            "gdbstub": s.profile.introspection.gdbstub.enabled,
            "snapshots": s.profile.introspection.snapshots,
        },
        "gdb": {"enabled": s.gdb_port is not None, "port": s.gdb_port},
        "reservation": {
            "remaining_seconds": s.remaining_seconds,   # None == infinite
            "expires_at": s.expires_at,                  # None == infinite
            "infinite": s.infinite,
            "default_minutes": s.profile.reservation.default_minutes,
            "max_minutes": s.profile.reservation.max_minutes,
        },
    }


def _get_session(session_id: str) -> Session:
    try:
        return app_ref.state.manager.get(session_id)
    except SessionError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


# --- auth ------------------------------------------------------------------


@app.get("/api/public-config")
def public_config(request: Request) -> dict:
    """Unauthenticated config the login screen needs before a token exists.

    `demo_login` (when HOLOBENCH_DEMO_LOGIN is set) lets the UI show — and
    one-click fill — try-it credentials. It only ever exposes what the operator
    explicitly opted into advertising; it is NOT the user store.
    """
    auth: AuthService = request.app.state.auth
    demo = None
    if _DEMO_LOGIN and ":" in _DEMO_LOGIN:
        u, _, p = _DEMO_LOGIN.partition(":")
        if u and p:
            demo = {"username": u, "password": p}
    # needs_onboarding: no users yet -> the UI offers "create your admin account"
    # (first registrant becomes admin). registration_open: ongoing self-signup
    # (role "user") is enabled. Either makes the UI show a register form.
    needs_onboarding = not auth.store.configured
    return {
        "auth_enabled": auth.enabled,
        "demo_login": demo,
        "needs_onboarding": needs_onboarding,
        "registration_open": _ALLOW_REGISTRATION,
        "can_register": needs_onboarding or _ALLOW_REGISTRATION,
    }


@app.post("/api/register")
def register(req: LoginRequest, request: Request) -> dict:
    """Self-service account creation. The FIRST account on a fresh instance always
    succeeds and becomes admin (onboarding). After that, requires
    HOLOBENCH_ALLOW_REGISTRATION=1 and creates a role 'user'. Auto-logs in."""
    auth: AuthService = request.app.state.auth
    if not req.username or not req.password:
        raise HTTPException(status_code=400, detail="username and password required")
    if len(req.password) < 6:
        raise HTTPException(status_code=400, detail="password must be at least 6 characters")
    onboarding = not auth.store.configured
    if not onboarding and not _ALLOW_REGISTRATION:
        raise HTTPException(status_code=403, detail="registration is closed — ask an admin to add you")
    if auth.store.get(req.username) is not None:
        raise HTTPException(status_code=409, detail="username already taken")
    role = "admin" if onboarding else "user"
    auth.store.add(req.username, req.password, role=role)
    if onboarding and not os.environ.get("HOLOBENCH_SECRET"):
        # First user just enabled auth; switch off the ephemeral open-mode key to a
        # persistent one so this token (and future logins) survive a restart.
        auth.secret = auth._load_or_create_persistent_secret()
    _audit("register", req.username, role=role, onboarding=onboarding)
    token = auth.login(req.username, req.password)
    user = auth.store.get(req.username)
    return {"token": token, "user": {"username": user.username, "role": user.role}}


@app.post("/api/login")
def login(req: LoginRequest, request: Request) -> dict:
    auth: AuthService = request.app.state.auth
    if not auth.enabled:
        # Open mode: no users configured, login is a no-op admin.
        return {"token": None, "user": {"username": "local", "role": "admin"}}
    ip = request.client.host if request.client else "?"
    key = f"{ip}:{req.username}"
    if _login_throttled(key):
        _audit("login_throttled", req.username, ip=ip)
        raise HTTPException(
            status_code=429,
            detail=f"too many failed logins; wait {_LOGIN_WINDOW_S}s",
        )
    token = auth.login(req.username, req.password)
    if not token:
        _login_record_fail(key)
        _audit("login_fail", req.username, ip=ip)
        raise HTTPException(status_code=401, detail="invalid credentials")
    _login_fails.pop(key, None)  # reset on success
    _audit("login", req.username, ip=ip)
    user = auth.store.get(req.username)
    return {"token": token, "user": {"username": user.username, "role": user.role}}


@app.get("/api/me")
def me(request: Request) -> dict:
    auth: AuthService = request.app.state.auth
    user = request.state.user
    return {
        "username": user.username,
        "role": user.role,
        "auth_enabled": auth.enabled,
    }


# --- user management (admin only) ------------------------------------------


def _require_admin(request: Request):
    user = request.state.user
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="admin only")
    return user


@app.get("/api/users")
def list_users(request: Request) -> list[dict]:
    _require_admin(request)
    store = request.app.state.auth.store
    return [{"username": u.username, "role": u.role} for u in store.list()]


@app.post("/api/users")
def add_user(req: UserCreateRequest, request: Request) -> dict:
    admin = _require_admin(request)
    if req.role not in ("user", "admin"):
        raise HTTPException(status_code=400, detail="role must be 'user' or 'admin'")
    if not req.username or not req.password:
        raise HTTPException(status_code=400, detail="username and password required")
    store = request.app.state.auth.store
    existed = store.get(req.username) is not None
    u = store.add(req.username, req.password, role=req.role)
    _audit("user_add" if not existed else "user_update", admin.username,
           target=u.username, role=u.role)
    return {"username": u.username, "role": u.role}


@app.delete("/api/users/{username}")
def remove_user(username: str, request: Request) -> dict:
    admin = _require_admin(request)
    if username == admin.username:
        raise HTTPException(status_code=400, detail="refusing to remove your own account")
    store = request.app.state.auth.store
    if not store.remove(username):
        raise HTTPException(status_code=404, detail="no such user")
    _audit("user_remove", admin.username, target=username)
    return {"username": username, "deleted": True}


# --- admin fleet view (per-session resource usage) -------------------------

_CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
_PAGE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096
# pid -> (total_jiffies, monotonic_ts) from the previous poll, so CPU% is the
# average over the admin panel's refresh interval (no blocking sample in-request).
_cpu_prev: dict[int, tuple[int, float]] = {}


def _pid_cpu_rss(pid: Optional[int]) -> tuple[Optional[float], Optional[float]]:
    """(cpu_pct since last poll, rss_mb) for a pid, or (None, None) if gone."""
    if not pid:
        return None, None
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        jiffies = int(fields[11]) + int(fields[12])  # utime + stime (after the ')')
        rss_mb = int(fields[21]) * _PAGE / 1024 / 1024
    except (OSError, IndexError, ValueError):
        _cpu_prev.pop(pid, None)
        return None, None
    now = time.monotonic()
    prev = _cpu_prev.get(pid)
    _cpu_prev[pid] = (jiffies, now)
    cpu = None
    if prev and now > prev[1]:
        cpu = round(100.0 * (jiffies - prev[0]) / _CLK_TCK / (now - prev[1]), 1)
    return cpu, round(rss_mb, 1)


def _session_disk_mb(session: Session) -> float:
    """Host disk used by a session's work dir (mostly the qcow2 overlay + logs)."""
    total = 0
    wd = session.work_dir
    if wd and wd.exists():
        for p in wd.rglob("*"):
            try:
                if p.is_file():
                    total += p.stat().st_size
            except OSError:
                pass
    return round(total / 1024 / 1024, 1)


def _session_idle_s(session: Session) -> Optional[float]:
    """Seconds since the board last produced console output (a stalled log ~=
    nobody is using it). None if no log yet."""
    log = session.console_log()
    try:
        if log and log.exists():
            return round(time.time() - log.stat().st_mtime, 1)
    except OSError:
        pass
    return None


@app.get("/api/admin/sessions")
def admin_sessions(request: Request) -> dict:
    """Every running board across all users, with per-session resource usage —
    so an admin can spot hogs / orphaned / idle boards and kill them.

    cpu_pct is top-style PER-CORE (100% = one full host core; a multi-vCPU board
    can exceed 100%). host_cores lets the UI also show the share of the whole
    machine (cpu_pct / host_cores)."""
    _require_admin(request)
    rows = []
    for s in app_ref.state.manager.list():
        cpu, rss = _pid_cpu_rss(s.pid)
        rows.append({
            "id": s.id,
            "owner": s.owner,
            "profile_id": s.profile.id,
            "display_name": s.profile.display_name,
            "state": s.state.value,
            "pid": s.pid,
            "uptime_s": round(time.time() - s.created_at, 1),
            "cpu_pct": cpu,
            "rss_mb": rss,
            "disk_mb": _session_disk_mb(s),
            "idle_s": _session_idle_s(s),
            "reservation": {
                "infinite": s.infinite,
                "remaining_s": None if s.infinite else s.remaining_seconds,
            },
        })
    rows.sort(key=lambda r: (r["owner"] or "", -(r["cpu_pct"] or 0)))
    return {"host_cores": os.cpu_count() or 1, "sessions": rows}


# --- profiles --------------------------------------------------------------


@app.get("/api/profiles")
def get_profiles() -> list[dict]:
    out = []
    for pid in list_profiles():
        try:
            p = load_profile(pid)
        except ProfileError:
            continue
        out.append(
            {
                "id": p.id,
                "display_name": p.display_name,
                "soc": p.soc,
                "description": p.description,
                "machine": p.qemu.machine,
                "assets_ready": default_asset_dir(p.id) is not None,
            }
        )
    return out


# --- labs (v3.0 multi-board topologies) ------------------------------------


def _lab_catalog_entry(lab_id: str) -> Optional[dict]:
    """A lab spec as catalog metadata (no launch). None if it doesn't load."""
    try:
        lab = load_lab(lab_id)
    except LabError:
        return None
    eth = [l for l in lab.links if l.type == "eth"]
    usb = [l for l in lab.links if l.type == "usb"]
    return {
        "id": lab.id,
        "display_name": lab.display_name,
        "description": lab.description,
        "nodes": [{"name": n.name, "profile": n.profile} for n in lab.nodes],
        "links": [
            ({"type": "eth", "segment": l.segment, "members": l.members}
             if l.type == "eth" else
             {"type": "usb", "host": l.host, "device": l.device})
            for l in lab.links
        ],
        "node_count": len(lab.nodes),
        "eth_segments": len(eth),
        # USB links aren't launchable yet (gated on model usbredir support).
        "launchable": not usb,
        "gated_reason": ("declares USB links (usbredir support not yet confirmed "
                         "by the board models)") if usb else None,
    }


@app.get("/api/labs")
def get_labs(request: Request) -> dict:
    """Catalog of declared labs + the currently-running ones."""
    catalog = [e for e in (_lab_catalog_entry(i) for i in list_labs()) if e]
    running = [rl.view() for rl in app_ref.state.labs.list()]
    return {"catalog": catalog, "running": running}


@app.post("/api/labs")
async def launch_lab(req: LaunchLabRequest, request: Request) -> dict:
    user = request.state.user
    try:
        lab = load_lab(req.lab_id)
    except LabError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    owner = user.username if request.app.state.auth.enabled else None
    minutes = req.minutes
    try:
        running = await app_ref.state.labs.launch(lab, owner=owner, minutes=minutes)
    except LabError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    _audit("lab_launch", user.username, lab=lab.id,
           nodes=len(lab.nodes), state=running.state.value)
    return running.view()


@app.get("/api/labs/{lab_id}")
def get_lab(lab_id: str, request: Request) -> dict:
    """A running lab's live status, or the spec (with a running:false flag) if it
    isn't launched."""
    running = app_ref.state.labs.peek(lab_id)
    if running is not None:
        return {"running": True, **running.view()}
    entry = _lab_catalog_entry(lab_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"no lab '{lab_id}'")
    return {"running": False, **entry}


@app.delete("/api/labs/{lab_id}")
async def stop_lab(lab_id: str, request: Request) -> dict:
    user = request.state.user
    try:
        await app_ref.state.labs.stop(lab_id)
    except LabError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    _audit("lab_stop", user.username, lab=lab_id)
    return {"stopped": lab_id}


# --- setup wizard ("build me a board") -------------------------------------


@app.get("/api/setup")
def get_setup(request: Request) -> dict:
    """First-run build status: docker availability, buildable boards (from
    tools/build-sources.yaml) + whether each image is built, and any active build."""
    _require_admin(request)
    return app_ref.state.setup.status()


@app.post("/api/setup/build")
async def setup_build(req: SetupBuildRequest, request: Request) -> dict:
    """Kick off `build-me.sh <board>` (build the forked qemu from source + the
    distributable image). Admin-only — it runs server-side and uses Docker."""
    user = _require_admin(request)
    try:
        view = await app_ref.state.setup.start(req.board, req.mode, req.bsp_path)
    except SetupError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    _audit("setup_build", user.username, board=req.board, mode=req.mode)
    return view


@app.get("/api/setup/manifest")
def setup_manifest(board: str, request: Request, bsp: Optional[str] = None) -> dict:
    """The per-board artifact manifest (derived from the profile). If `bsp` (the
    operator's mount root) is given, validate it: which required files are present
    vs missing. The wizard refuses to launch until ok=true."""
    _require_admin(request)
    try:
        if bsp:
            return validate_manifest(board, bsp)
        return {"board": board, "required": required_artifacts(board)}
    except ProfileError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.get("/api/setup/nxp-manifest")
def setup_nxp_manifest(board: str, request: Request) -> dict:
    """Profile-derived flat manifest for tools/fetch-nxp.sh (the operator-host
    helper that builds the SM firmware + hands off the EULA-gated NXP files). For
    the wizard's 'fetch from NXP' path on BYO-BSP boards (e.g. i.MX95)."""
    _require_admin(request)
    try:
        return nxp_manifest(board)
    except ProfileError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.post("/api/setup/cancel")
async def setup_cancel(request: Request) -> dict:
    _require_admin(request)
    await app_ref.state.setup.cancel()
    return app_ref.state.setup.status()


# --- sessions --------------------------------------------------------------


@app.get("/api/sessions")
def get_sessions(request: Request) -> list[dict]:
    user = request.state.user
    sessions = app_ref.state.manager.list()
    if not user.is_admin:
        sessions = [s for s in sessions if s.owner in (None, user.username)]
    return [_session_view(s) for s in sessions]


@app.post("/api/sessions")
async def launch_session(req: LaunchRequest, request: Request) -> dict:
    user = request.state.user
    mgr = app_ref.state.manager
    # Quotas (0 = unlimited).
    if _MAX_SESSIONS_TOTAL and len(mgr.list()) >= _MAX_SESSIONS_TOTAL:
        raise HTTPException(status_code=429, detail="board farm at capacity")
    if _MAX_SESSIONS_PER_USER:
        mine = sum(1 for s in mgr.list() if s.owner == user.username)
        if mine >= _MAX_SESSIONS_PER_USER:
            raise HTTPException(
                status_code=429,
                detail=f"per-user session limit reached ({_MAX_SESSIONS_PER_USER})",
            )
    try:
        profile = load_profile(req.profile_id)
    except ProfileError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    # Security: never let a client point QEMU at an arbitrary host path. The
    # asset dir is resolved from the (validated) profile id unless an operator
    # explicitly opted into trusting client asset paths.
    if req.assets and _ALLOW_CLIENT_ASSETS:
        asset_dir = Path(req.assets)
    else:
        asset_dir = default_asset_dir(profile.id)
    owner = user.username if request.app.state.auth.enabled else None
    # Resolve reservation length. minutes<=0 = infinite; only an unbounded profile
    # (max_minutes<=0) or an admin may grant infinite — others clamp to max_minutes.
    maxm = profile.reservation.max_minutes
    if req.minutes is None:
        minutes = None  # use the profile default
    elif req.minutes <= 0:
        minutes = 0 if (maxm <= 0 or user.is_admin) else maxm
    else:
        minutes = req.minutes if maxm <= 0 else min(req.minutes, maxm)
    try:
        session = await mgr.launch(profile, asset_dir=asset_dir, owner=owner, minutes=minutes)
    except SessionError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    _audit("session_launch", user.username, session=session.id,
           profile=req.profile_id, infinite=session.infinite)
    return _session_view(session)


@app.get("/api/sessions/{session_id}")
def get_session(session_id: str) -> dict:
    try:
        return _session_view(app_ref.state.manager.get(session_id))
    except SessionError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


_VERBS = {
    "reset": "system_reset",
    "pause": "stop",
    "resume": "cont",
}


@app.post("/api/sessions/{session_id}/actions/{action}")
async def session_action(session_id: str, action: str, request: Request) -> dict:
    mgr = app_ref.state.manager
    try:
        session = mgr.get(session_id)
    except SessionError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    _who = getattr(request.state, "user", None)
    _audit(f"session_{action}", _who.username if _who else "-", session=session_id)
    try:
        if action == "reset":
            await session.system_reset()
        elif action == "pause":
            await session.pause()
        elif action == "resume":
            await session.resume()
        elif action == "stop":
            await mgr.destroy(session_id)
            return {"id": session_id, "state": "stopped"}
        elif action == "reinstall":
            new = await mgr.reinstall(session_id)
            return _session_view(new)
        elif action in ("attach_lcd", "detach_lcd"):
            new = await mgr.set_lcd(session_id, on=(action == "attach_lcd"))
            return _session_view(new)
        else:
            raise HTTPException(status_code=400, detail=f"unknown action '{action}'")
    except SessionError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return _session_view(session)


@app.post("/api/sessions/{session_id}/extend")
def extend_session(session_id: str, minutes: int = 30) -> dict:
    s = _get_session(session_id)
    s.extend(minutes)
    return _session_view(s)


# --- file injection (virtio-9p share) --------------------------------------

MAX_UPLOAD_BYTES = 512 * 1024 * 1024  # 512 MiB per-file ceiling (Image-sized)
# Per-session total upload quota across the 9p share + camera frames dir
# (0 = unlimited). Bounds host-disk fill via uploads on a shared deployment.
_UPLOAD_QUOTA_MB = int(os.environ.get("HOLOBENCH_UPLOAD_QUOTA_MB", "0"))


def _dir_bytes(d: Optional[Path]) -> int:
    if not d or not d.exists():
        return 0
    return sum(p.stat().st_size for p in d.iterdir() if p.is_file())


def _upload_budget(session: Session) -> Optional[int]:
    """Remaining upload bytes for this session (None = unlimited)."""
    if not _UPLOAD_QUOTA_MB:
        return None
    used = _dir_bytes(session.share_dir) + _dir_bytes(session.camera_frames_dir)
    return max(0, _UPLOAD_QUOTA_MB * 1024 * 1024 - used)


def _require_share(session: Session) -> Path:
    fi = session.profile.file_injection
    if session.share_dir is None or not (fi.nine_p.enabled or fi.tftp.enabled):
        raise HTTPException(status_code=404, detail="this board has no file injection")
    return session.share_dir


def _safe_name(name: str) -> str:
    """Reject path traversal; keep only a bare filename."""
    base = Path(name).name  # strips any directory components
    if not base or base in (".", "..") or "/" in base or "\\" in base:
        raise HTTPException(status_code=400, detail=f"invalid filename '{name}'")
    return base


@app.get("/api/sessions/{session_id}/files")
def list_files(session_id: str) -> dict:
    mgr = app_ref.state.manager
    try:
        session = mgr.get(session_id)
    except SessionError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    share = _require_share(session)
    files = [
        {"name": p.name, "size": p.stat().st_size}
        for p in sorted(share.iterdir())
        if p.is_file()
    ]
    return {"mount_tag": session.profile.file_injection.nine_p.mount_tag, "files": files}


@app.post("/api/sessions/{session_id}/files")
async def upload_file(session_id: str, file: UploadFile) -> dict:
    mgr = app_ref.state.manager
    try:
        session = mgr.get(session_id)
    except SessionError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    share = _require_share(session)
    name = _safe_name(file.filename or "")

    dest = (share / name).resolve()
    if share.resolve() not in dest.parents:
        raise HTTPException(status_code=400, detail="path escapes the share dir")

    budget = _upload_budget(session)
    if budget == 0:
        raise HTTPException(status_code=413, detail="session upload quota exhausted")
    cap = MAX_UPLOAD_BYTES if budget is None else min(MAX_UPLOAD_BYTES, budget)
    written = 0
    try:
        with dest.open("wb") as out:
            while chunk := await file.read(1024 * 1024):
                written += len(chunk)
                if written > cap:
                    out.close()
                    dest.unlink(missing_ok=True)
                    detail = (
                        "file exceeds size limit" if cap == MAX_UPLOAD_BYTES
                        else "session upload quota exceeded"
                    )
                    raise HTTPException(status_code=413, detail=detail)
                out.write(chunk)
    finally:
        await file.close()
    return {"name": name, "size": written}


@app.delete("/api/sessions/{session_id}/files/{name}")
def delete_file(session_id: str, name: str) -> dict:
    mgr = app_ref.state.manager
    try:
        session = mgr.get(session_id)
    except SessionError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    share = _require_share(session)
    dest = (share / _safe_name(name)).resolve()
    if share.resolve() not in dest.parents or not dest.is_file():
        raise HTTPException(status_code=404, detail="no such file")
    dest.unlink()
    return {"name": dest.name, "deleted": True}


# --- virtual camera (ISI host-frame-source) --------------------------------


def _require_camera(session: Session) -> Path:
    """The per-session frames dir, or 404 if this board has no virtual camera."""
    cam = session.profile.camera
    if not cam.enabled or session.camera_frames_dir is None:
        raise HTTPException(status_code=404, detail="this board has no virtual camera")
    return session.camera_frames_dir


@app.get("/api/sessions/{session_id}/camera/frames")
def list_camera_frames(session_id: str) -> dict:
    session = _get_session(session_id)
    frames_dir = _require_camera(session)
    frames = [
        {"name": p.name, "size": p.stat().st_size}
        for p in sorted(frames_dir.iterdir())
        if p.is_file()
    ] if frames_dir.exists() else []
    cam = session.profile.camera
    return {
        "frames": frames,
        "frame_bytes": cam.frame_bytes,
        "width": cam.width,
        "height": cam.height,
        "bytes_per_pixel": cam.bytes_per_pixel,
        "pixel_format": cam.pixel_format,
        "runtime_settable": cam.runtime_settable,
    }


@app.post("/api/sessions/{session_id}/camera/frames")
async def upload_camera_frame(session_id: str, file: UploadFile) -> dict:
    session = _get_session(session_id)
    frames_dir = _require_camera(session)
    frames_dir.mkdir(parents=True, exist_ok=True)
    cam = session.profile.camera

    # Frames are read in sorted order as *.raw; force the extension so the model
    # picks them up and the user controls order via filename (001.raw, 002.raw…).
    name = _safe_name(file.filename or "")
    if not name.endswith(".raw"):
        name = name + ".raw"
    dest = (frames_dir / name).resolve()
    if frames_dir.resolve() not in dest.parents:
        raise HTTPException(status_code=400, detail="path escapes the frames dir")

    budget = _upload_budget(session)
    if budget == 0:
        raise HTTPException(status_code=413, detail="session upload quota exhausted")
    cap = MAX_UPLOAD_BYTES if budget is None else min(MAX_UPLOAD_BYTES, budget)
    written = 0
    try:
        with dest.open("wb") as out:
            while chunk := await file.read(1024 * 1024):
                written += len(chunk)
                if written > cap:
                    out.close()
                    dest.unlink(missing_ok=True)
                    detail = (
                        "frame exceeds size limit" if cap == MAX_UPLOAD_BYTES
                        else "session upload quota exceeded"
                    )
                    raise HTTPException(status_code=413, detail=detail)
                out.write(chunk)
    finally:
        await file.close()

    # A frame whose size != W*H*bpp makes the model fall back to its gradient
    # test pattern — reject it loudly so the user fixes the geometry, not silently.
    expected = cam.frame_bytes
    if expected is not None and written != expected:
        dest.unlink(missing_ok=True)
        raise HTTPException(
            status_code=422,
            detail=(
                f"frame is {written} bytes but this board expects exactly "
                f"{expected} ({cam.width}x{cam.height}x{cam.bytes_per_pixel}"
                f"{', ' + cam.pixel_format if cam.pixel_format else ''}); "
                "a mismatched frame would only show the gradient test pattern"
            ),
        )
    return {"name": name, "size": written, "applies": "live" if cam.runtime_settable else "on next (re)boot"}


@app.delete("/api/sessions/{session_id}/camera/frames/{name}")
def delete_camera_frame(session_id: str, name: str) -> dict:
    session = _get_session(session_id)
    frames_dir = _require_camera(session)
    dest = (frames_dir / _safe_name(name)).resolve()
    if frames_dir.resolve() not in dest.parents or not dest.is_file():
        raise HTTPException(status_code=404, detail="no such frame")
    dest.unlink()
    return {"name": dest.name, "deleted": True}


# --- framebuffer (LCD panel) -----------------------------------------------


@app.get("/api/sessions/{session_id}/screen.png")
async def session_screen(session_id: str) -> Response:
    """Current framebuffer as PNG (QMP screendump of the LCDIF/DPU)."""
    mgr = app_ref.state.manager
    try:
        session = mgr.get(session_id)
    except SessionError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    if not session.profile.display.enabled:
        raise HTTPException(status_code=404, detail="board has no display")
    path = session.work_dir / "screen.png"
    try:
        await session.screendump(path, fmt="png")
        data = path.read_bytes()
    except (SessionError, OSError) as exc:
        raise HTTPException(status_code=503, detail=f"screendump failed: {exc}")
    return Response(
        content=data,
        media_type="image/png",
        headers={"Cache-Control": "no-store"},
    )


# --- introspection (beat-the-hardware panels; read-only) -------------------


@app.get("/api/sessions/{session_id}/introspect/mtree")
async def introspect_mtree(session_id: str) -> dict:
    s = _get_session(session_id)
    try:
        return {"text": await s.hmp("info mtree")}
    except SessionError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@app.get("/api/sessions/{session_id}/introspect/qtree")
async def introspect_qtree(session_id: str) -> dict:
    s = _get_session(session_id)
    try:
        return {"text": await s.hmp("info qtree")}
    except SessionError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@app.get("/api/sessions/{session_id}/introspect/qom")
async def introspect_qom(session_id: str, path: str = "/machine") -> dict:
    s = _get_session(session_id)
    try:
        return {"path": path, "children": await s.qom_list(path)}
    except SessionError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@app.get("/api/sessions/{session_id}/events")
def session_events(session_id: str) -> dict:
    s = _get_session(session_id)
    return {"events": s.recent_events()}


_XP_RE = re.compile(r":\s*0x([0-9a-fA-F]+)")


async def _read_word(s: Session, addr: int) -> Optional[int]:
    """Read a 32-bit word from guest *physical* memory via the stock read-only HMP
    `xp` (examine physical) — no CPU halt, no model change. Used to sample a GPIO
    output-data register for the LEDs panel. Returns None if unreadable."""
    try:
        out = await s.hmp(f"xp/1wx 0x{addr:x}")
        m = _XP_RE.search(out or "")
        return int(m.group(1), 16) if m else None
    except SessionError:
        return None


@app.get("/api/sessions/{session_id}/leds")
async def session_leds(session_id: str) -> dict:
    """Board LED panel. Always includes a synthetic Power/status LED (session state
    — Phase 1). Profile-declared `gpio` LEDs (Phase 2) are read from the guest's own
    GPIO output register via a stock read-only interface — no model changes."""
    s = _get_session(session_id)
    leds = [{
        "name": "Power", "color": "#22c55e", "source": "power",
        "on": s.state.value == "running",
    }]
    for spec in s.profile.leds:
        on = None
        if spec.source == "gpio" and spec.reg is not None and spec.bit is not None:
            driven = True
            if spec.enable_reg is not None and spec.enable_bit is not None:
                en = await _read_word(s, spec.enable_reg)
                if en is not None:
                    en_bit = bool(en & (1 << spec.enable_bit))
                    driven = en_bit if spec.enable_high else not en_bit
            if not driven:
                on = False  # pin not configured as output → LED not driven
            else:
                val = await _read_word(s, spec.reg)
                if val is not None:
                    bit = bool(val & (1 << spec.bit))
                    on = bit if spec.active_high else not bit
        leds.append({"name": spec.name, "color": spec.color, "source": spec.source, "on": on})
    return {"leds": leds}


# --- snapshots (savevm/loadvm) ---------------------------------------------


def _require_snapshots(session: Session) -> None:
    if not session.profile.introspection.snapshots:
        raise HTTPException(status_code=404, detail="this board has no snapshots")


def _safe_snap_name(name: str) -> str:
    # Flows into an HMP `savevm <name>` — allow only a safe charset, no spaces
    # or shell/monitor metacharacters.
    if not name or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", name):
        raise HTTPException(status_code=400, detail="invalid snapshot name")
    return name


@app.get("/api/sessions/{session_id}/snapshots")
async def list_snapshots(session_id: str) -> dict:
    s = _get_session(session_id)
    _require_snapshots(s)
    return {"snapshots": await s.snapshot_list()}


@app.post("/api/sessions/{session_id}/snapshots")
async def save_snapshot(session_id: str, req: SnapshotRequest) -> dict:
    s = _get_session(session_id)
    _require_snapshots(s)
    await s.snapshot_save(_safe_snap_name(req.name))
    return {"snapshots": await s.snapshot_list()}


@app.post("/api/sessions/{session_id}/snapshots/{name}/load")
async def load_snapshot(session_id: str, name: str) -> dict:
    s = _get_session(session_id)
    _require_snapshots(s)
    await s.snapshot_load(_safe_snap_name(name))
    return {"loaded": name}


@app.delete("/api/sessions/{session_id}/snapshots/{name}")
async def delete_snapshot(session_id: str, name: str) -> dict:
    s = _get_session(session_id)
    _require_snapshots(s)
    await s.snapshot_delete(_safe_snap_name(name))
    return {"snapshots": await s.snapshot_list()}


# --- console websocket -----------------------------------------------------


@app.websocket("/api/sessions/{session_id}/console")
async def console_ws(
    websocket: WebSocket,
    session_id: str,
    chardev: Optional[str] = None,
    token: Optional[str] = None,
):
    # Reject cross-origin browser connections (CSWSH) before doing anything.
    if not _origin_ok(websocket.headers):
        await websocket.close(code=4403, reason="bad origin")
        return
    auth: AuthService = websocket.app.state.auth
    user = auth.resolve(token or websocket.cookies.get("hb_token"))
    if user is None:
        await websocket.close(code=4401, reason="unauthorized")
        return
    mgr = websocket.app.state.manager
    try:
        session = mgr.get(session_id)
    except SessionError:
        await websocket.close(code=4404, reason="no such session")
        return
    if not user.is_admin and session.owner not in (None, user.username):
        await websocket.close(code=4403, reason="not your session")
        return
    await websocket.accept()

    # Lazily attach the serial tap on connect (ref-counted); detached on the last
    # disconnect when HOLOBENCH_LAZY_SERIAL is on. Otherwise returns the always-on tap.
    tap = await session.ensure_tap(chardev)
    if tap is None:
        await websocket.close(code=4404, reason="no console for this session")
        return

    queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=2048)

    def on_bytes(data: bytes) -> None:
        try:
            queue.put_nowait(data)
        except asyncio.QueueFull:
            pass  # drop on backpressure rather than stall the tap

    # Send scrollback, then subscribe for live output.
    snap = tap.snapshot()
    if snap:
        await websocket.send_bytes(snap)
    tap.subscribe(on_bytes)

    async def pump_out() -> None:
        while True:
            data = await queue.get()
            await websocket.send_bytes(data)

    async def pump_in() -> None:
        while True:
            msg = await websocket.receive()
            if msg["type"] == "websocket.disconnect":
                raise WebSocketDisconnect()
            data = msg.get("bytes")
            if data is None and msg.get("text") is not None:
                data = msg["text"].encode()
            if data:
                await tap.send(data)

    out_task = asyncio.create_task(pump_out())
    in_task = asyncio.create_task(pump_in())
    try:
        await asyncio.wait({out_task, in_task}, return_when=asyncio.FIRST_COMPLETED)
    except WebSocketDisconnect:
        pass
    finally:
        tap.unsubscribe(on_bytes)
        out_task.cancel()
        in_task.cancel()
        await session.release_tap(chardev)


# --- static frontend (mounted last so /api/* wins) -------------------------

if _FRONTEND_DIR.is_dir():

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(_FRONTEND_DIR / "index.html")

    app.mount("/", StaticFiles(directory=_FRONTEND_DIR), name="frontend")

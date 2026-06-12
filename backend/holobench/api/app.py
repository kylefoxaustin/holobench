# SPDX-License-Identifier: Apache-2.0
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
import os
import re
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

# Paths under /api that don't require authentication.
_OPEN_PATHS = {"/api/login"}
_SESSION_PATH_RE = re.compile(r"^/api/sessions/([^/]+)(?:/|$)")


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.manager = SessionManager()
    app.state.auth = AuthService()
    reaper = asyncio.create_task(app.state.manager.run_reaper())
    try:
        yield
    finally:
        reaper.cancel()
        try:
            await reaper
        except asyncio.CancelledError:
            pass
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


class SnapshotRequest(BaseModel):
    name: str


class LoginRequest(BaseModel):
    username: str
    password: str


def _session_view(s: Session) -> dict:
    return {
        "id": s.id,
        "profile_id": s.profile.id,
        "display_name": s.profile.display_name,
        "soc": s.profile.soc,
        "state": s.state.value,
        "pid": s.pid,
        "owner": s.owner,
        "serial": [
            {"name": p.name, "chardev": p.chardev, "role": p.role, "default": p.default}
            for p in s.profile.serial
        ],
        "display": {"enabled": s.profile.display.enabled, "vnc": s.profile.display.vnc},
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
            "remaining_seconds": s.remaining_seconds,
            "expires_at": s.expires_at,
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


@app.post("/api/login")
def login(req: LoginRequest, request: Request) -> dict:
    auth: AuthService = request.app.state.auth
    if not auth.enabled:
        # Open mode: no users configured, login is a no-op admin.
        return {"token": None, "user": {"username": "local", "role": "admin"}}
    token = auth.login(req.username, req.password)
    if not token:
        raise HTTPException(status_code=401, detail="invalid credentials")
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
    try:
        session = await mgr.launch(profile, asset_dir=asset_dir, owner=owner)
    except SessionError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
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
async def session_action(session_id: str, action: str) -> dict:
    mgr = app_ref.state.manager
    try:
        session = mgr.get(session_id)
    except SessionError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

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

MAX_UPLOAD_BYTES = 512 * 1024 * 1024  # 512 MiB ceiling (Image-sized)


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

    written = 0
    try:
        with dest.open("wb") as out:
            while chunk := await file.read(1024 * 1024):
                written += len(chunk)
                if written > MAX_UPLOAD_BYTES:
                    out.close()
                    dest.unlink(missing_ok=True)
                    raise HTTPException(status_code=413, detail="file exceeds size limit")
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

    written = 0
    try:
        with dest.open("wb") as out:
            while chunk := await file.read(1024 * 1024):
                written += len(chunk)
                if written > MAX_UPLOAD_BYTES:
                    out.close()
                    dest.unlink(missing_ok=True)
                    raise HTTPException(status_code=413, detail="frame exceeds size limit")
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

    tap = session.get_tap(chardev)
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


# --- static frontend (mounted last so /api/* wins) -------------------------

if _FRONTEND_DIR.is_dir():

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(_FRONTEND_DIR / "index.html")

    app.mount("/", StaticFiles(directory=_FRONTEND_DIR), name="frontend")

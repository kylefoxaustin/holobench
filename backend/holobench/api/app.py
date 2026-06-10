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
    Response,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..profiles import ProfileError, list_profiles, load_profile
from ..profiles.loader import default_asset_dir
from ..session.manager import Session, SessionError, SessionManager

_FRONTEND_DIR = Path(__file__).resolve().parents[3] / "frontend"


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.manager = SessionManager()
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


def _auth_token() -> Optional[str]:
    """The required bearer token, or None to run open (single-user dev)."""
    return os.environ.get("HOLOBENCH_TOKEN")


@app.middleware("http")
async def auth_middleware(request, call_next):
    """Swappable auth gate. Off by default; set HOLOBENCH_TOKEN to require a
    `Authorization: Bearer <token>` on /api/* (the static UI stays open so the
    page can load). Replace this with a real provider for shared deployment."""
    token = _auth_token()
    if token and request.url.path.startswith("/api"):
        if request.headers.get("authorization") != f"Bearer {token}":
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
    return await call_next(request)


# --- models ----------------------------------------------------------------


class LaunchRequest(BaseModel):
    profile_id: str
    assets: Optional[str] = None


class SnapshotRequest(BaseModel):
    name: str


def _session_view(s: Session) -> dict:
    return {
        "id": s.id,
        "profile_id": s.profile.id,
        "display_name": s.profile.display_name,
        "soc": s.profile.soc,
        "state": s.state.value,
        "pid": s.pid,
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
def get_sessions() -> list[dict]:
    return [_session_view(s) for s in app_ref.state.manager.list()]


@app.post("/api/sessions")
async def launch_session(req: LaunchRequest) -> dict:
    try:
        profile = load_profile(req.profile_id)
    except ProfileError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    asset_dir = Path(req.assets) if req.assets else default_asset_dir(profile.id)
    try:
        session = await app_ref.state.manager.launch(profile, asset_dir=asset_dir)
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
    expected = _auth_token()
    if expected and token != expected:
        await websocket.close(code=4401, reason="unauthorized")
        return
    await websocket.accept()
    mgr = app_ref.state.manager
    try:
        session = mgr.get(session_id)
    except SessionError:
        await websocket.close(code=4404, reason="no such session")
        return

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

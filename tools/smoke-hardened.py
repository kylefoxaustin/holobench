#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Hardened-mode integration smoke: enforced auth + login throttle + admin user
API + per-session cgroup caps + session ownership + upload quota + audit log,
all on at once, driven through the real HTTP API.

Run from a built dev checkout (boots a real QEMU board, so the board's emulator
binary + assets must be present; cgroup checks need cgroup v2 delegated to the
user — HOLOBENCH_CGROUP=1). Exits non-zero if any check fails.

    .venv/bin/python tools/smoke-hardened.py
"""
import atexit, json, os, subprocess, sys, time, urllib.request, urllib.error, shutil, glob
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PORT = int(os.environ.get("SMOKE_PORT", "8099"))
BOARD = os.environ.get("SMOKE_BOARD", "imx91-evk")
sys.path.insert(0, str(REPO / "backend"))

FAILS = []
def check(ok, label):
    print(("  PASS " if ok else "  FAIL ") + label)
    if not ok:
        FAILS.append(label)

DATA = Path("/tmp/hb-smoke-data"); shutil.rmtree(DATA, ignore_errors=True); DATA.mkdir()
AUDIT = DATA / "audit.jsonl"
from holobench.auth import UserStore
UserStore(DATA / "users.yaml").add("alice", "pw", role="admin")
print("setup: admin 'alice' created (enforced auth ON)")

env = {**os.environ,
       "HOLOBENCH_USERS": str(DATA / "users.yaml"),
       "HOLOBENCH_SECRET": "smoke-secret",
       "HOLOBENCH_AUDIT_LOG": str(AUDIT),
       "HOLOBENCH_CGROUP": "1", "HOLOBENCH_NICE": "10",
       "HOLOBENCH_UPLOAD_QUOTA_MB": "8",
       "HOLOBENCH_LOGIN_MAX_FAILS": "3", "HOLOBENCH_LOGIN_WINDOW_S": "60"}
srv = subprocess.Popen([str(REPO / ".venv/bin/holobench"), "serve", "--host", "127.0.0.1", "--port", str(PORT)],
                       cwd=str(REPO), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
atexit.register(lambda: (srv.terminate(), srv.wait()))
B = f"http://127.0.0.1:{PORT}"


def call(method, path, token=None, body=None, want=None, label=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(B + path, data=data, method=method)
    if token:
        req.add_header("Authorization", "Bearer " + token)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            code, out = r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        code, out = e.code, e.read().decode()
    if want is not None:
        check(code == want, (label or f"{method} {path}") + f" -> {code}")
    try:
        return code, json.loads(out)
    except Exception:
        return code, out


def upload(path, name, mb, token, want):
    bnd = "----hb"
    body = f"--{bnd}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{name}\"\r\nContent-Type: application/octet-stream\r\n\r\n".encode()
    body += b"x" * (mb * 1024 * 1024) + f"\r\n--{bnd}--\r\n".encode()
    req = urllib.request.Request(B + path, data=body, method="POST")
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Content-Type", f"multipart/form-data; boundary={bnd}")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            code = r.status
    except urllib.error.HTTPError as e:
        code = e.code
    check(code == want, f"upload {mb}MB -> {code}")


for _ in range(40):
    try:
        urllib.request.urlopen(B + "/api/login", data=b"{}", timeout=2)
    except urllib.error.HTTPError:
        break
    except Exception:
        time.sleep(0.5)

print("\n[A] auth")
call("GET", "/api/sessions", want=401, label="no-token sessions")
_, t = call("POST", "/api/login", body={"username": "alice", "password": "pw"}, want=200, label="login")
token = t.get("token") if isinstance(t, dict) else None
check(bool(token), "token acquired")
call("GET", "/api/me", token=token, want=200, label="me")
for _ in range(3):
    call("POST", "/api/login", body={"username": "mallory", "password": "x"})
call("POST", "/api/login", body={"username": "mallory", "password": "x"}, want=429, label="brute-force throttle")

print("\n[B] admin user API")
call("GET", "/api/users", token=token, want=200, label="list users")
call("POST", "/api/users", token=token, body={"username": "bob", "password": "pw2"}, want=200, label="add user")
_, bl = call("POST", "/api/login", body={"username": "bob", "password": "pw2"})
btok = bl.get("token") if isinstance(bl, dict) else None
call("GET", "/api/users", token=btok, want=403, label="non-admin blocked")
call("DELETE", "/api/users/bob", token=token, want=200, label="remove user")

print("\n[C] launch under auth + cgroup (infinite reserve)")
_, s = call("POST", "/api/sessions", token=token, body={"profile_id": BOARD, "minutes": 0}, want=200, label="launch")
sid = s.get("id") if isinstance(s, dict) else None
check(isinstance(s, dict) and s.get("reservation", {}).get("infinite") is True, "infinite reservation")
cgs = glob.glob(f"/sys/fs/cgroup/user.slice/user-{os.getuid()}.slice/user@{os.getuid()}.service/holobench/{sid}")
check(bool(cgs), "per-session cgroup created")
if cgs:
    p = Path(cgs[0])
    print("    memory.max=", (p / "memory.max").read_text().strip(), "pids.max=", (p / "pids.max").read_text().strip())

print("\n[D] ownership")
call("POST", "/api/users", token=token, body={"username": "carol", "password": "pw3"}, want=200, label="add carol")
_, cl = call("POST", "/api/login", body={"username": "carol", "password": "pw3"})
ctok = cl.get("token") if isinstance(cl, dict) else None
call("GET", f"/api/sessions/{sid}", token=ctok, want=403, label="carol blocked from alice's session")

print("\n[E] upload quota (8MB)")
upload(f"/api/sessions/{sid}/files", "a.bin", 5, token, 200)
upload(f"/api/sessions/{sid}/files", "b.bin", 5, token, 413)

print("\n[F] audit log")
time.sleep(0.3)
events = {json.loads(l)["event"] for l in AUDIT.read_text().splitlines()} if AUDIT.exists() else set()
need = {"login", "login_fail", "login_throttled", "session_launch", "user_add", "user_remove"}
print("    events:", sorted(events))
check(need <= events, "expected audit events present")

call("POST", f"/api/sessions/{sid}/actions/stop", token=token, want=200, label="teardown")
print()
if FAILS:
    print(f"SMOKE FAILED ({len(FAILS)}): " + "; ".join(FAILS))
    sys.exit(1)
print("HARDENED-MODE SMOKE: ALL PASS")

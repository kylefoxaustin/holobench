<!-- SPDX-License-Identifier: Apache-2.0 -->
# Deploying Holobench as a shared "virtual EVK" service

Holobench runs frictionless and open for local/dev use. This guide is the
**Phase-6 hardening** path for exposing it to multiple users on a network. Read
it top to bottom before binding to anything but `127.0.0.1`.

## Threat model in one paragraph

The browser is untrusted. It must reach only **mediated** surfaces — terminal
bytes, framebuffer PNGs, and a fixed set of scoped control verbs — never raw QMP
or the serial control socket (QMP is a backend-only unix socket; gdbstub binds
`127.0.0.1`). Uploaded files and frames are hostile until proven otherwise
(size-capped, name-sanitized, confined to the session dir). Each board is a real
QEMU process that can burn CPU/RAM, so a shared host needs per-session resource
limits. Holobench enforces these; your deployment must supply TLS, a stable
secret, and (for hard isolation) cgroups.

## 1. Turn on auth

Auth is **off** until the first user exists, then enforced (login + per-user
session ownership; admins see all).

```bash
holobench user add alice --admin     # prompts for a password; switches auth ON
holobench user add bob               # a regular user
```

Admins can also manage users over the API (same store): `GET /api/users`,
`POST /api/users` (`{username, password, role}`), `DELETE /api/users/{username}`
— all admin-only (`403` otherwise; you can't delete your own account). User-mgmt
actions are audit-logged.

- **`HOLOBENCH_SECRET`** — the token-signing key. Set it explicitly for any
  multi-worker / multi-host deployment (all workers must share one key). If you
  don't, a single instance auto-generates and persists one to `data/secret`
  (mode 0600) so logins survive restarts — fine for one process, **not** for a
  load-balanced fleet.
- **Tokens expire** after 8 h (HMAC-signed, `exp` enforced).
- **Login throttle** (brute-force): 5 failed logins per ip+username per 60 s →
  `429`. Tune with `HOLOBENCH_LOGIN_MAX_FAILS` / `HOLOBENCH_LOGIN_WINDOW_S`.
- The user store (`data/users.yaml`, mode 0600, or `$HOLOBENCH_USERS`) holds
  PBKDF2-SHA256 hashes only. Keep `data/` off any world-readable share.

## 2. Terminate TLS (don't ship tokens in the clear)

Holobench speaks plain HTTP + WS. Put it behind a TLS-terminating reverse proxy
and bind Holobench itself to localhost:

```bash
holobench serve --host 127.0.0.1 --port 8080
```

Caddy (automatic certs, proxies WebSockets transparently):

```
evk.example.com {
    reverse_proxy 127.0.0.1:8080
}
```

nginx (note the WebSocket upgrade headers — the console needs them):

```nginx
server {
    listen 443 ssl;
    server_name evk.example.com;
    # ssl_certificate ... ; ssl_certificate_key ... ;
    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_read_timeout 86400;          # keep long-lived console sockets open
    }
}
```

- **`HOLOBENCH_ALLOWED_ORIGINS`** — comma-separated origin allowlist for the
  console WebSocket (CSWSH defense). Defaults to same-origin (Origin must match
  Host); set it explicitly when the public origin differs from the bind host,
  e.g. `HOLOBENCH_ALLOWED_ORIGINS=https://evk.example.com`.

## 3. Cap per-session resources (shared-host DoS)

A board is a real QEMU; one user shouldn't starve the host.

Always-on (no setup): **`RLIMIT_CORE=0`** — a crashed multi-GB-RAM QEMU won't
dump a multi-GB core (disk-fill + guest-memory info-leak). Plus **`HOLOBENCH_NICE`**
(e.g. `10`) so emulation can't peg an interactive host.

> Note: there is intentionally **no `RLIMIT_AS` memory cap** — QEMU/TCG reserves
> tens of GB of (unbacked) virtual address space, so an `RLIMIT_AS` sized to
> guest RAM kills it. Use the cgroup `memory.max` below (RSS-based) instead.

**Built-in per-session cgroup v2 caps (recommended).** Set **`HOLOBENCH_CGROUP=1`**
and each board's QEMU runs in its own cgroup with hard, RSS-based caps it cannot
exceed — one session can't OOM or fork-bomb the host, and the cgroup is removed
when the session ends. Knobs:

| Var | cgroup file | Default |
|---|---|---|
| `HOLOBENCH_MEM_CAP_MB` | `memory.max` | guest RAM × 1.5 + 512 MB |
| `HOLOBENCH_PIDS_MAX` | `pids.max` | 512 |
| `HOLOBENCH_CPU_CORES` | `cpu.max` (cores) | unset (no CPU cap) |

Holobench needs a **writable, delegated cgroup v2 parent** to create children
under. Two ways:
- **Auto** (`HOLOBENCH_CGROUP=1`): it uses your systemd user delegation
  (`user@<uid>.service`) — works out of the box on a systemd login session. Only
  the controllers your session delegates are applied (commonly `memory`+`pids`;
  `cpu` often needs system config — see below).
- **Explicit** (`HOLOBENCH_CGROUP_PARENT=/sys/fs/cgroup/…`): point at a cgroup
  you've delegated to the service account with the controllers you want enabled
  in its `cgroup.subtree_control` (`+memory +cpu +pids`).

To get the **`cpu`** controller delegated to a systemd user session (so
`HOLOBENCH_CPU_CORES` takes effect):

    # /etc/systemd/system/user@.service.d/delegate.conf
    [Service]
    Delegate=cpu cpuset io memory pids

(then `systemctl daemon-reload` + re-login). Inside a container, run with
`--cgroupns=private` and a delegated cgroup, or just rely on the container's own
`--memory`/`--cpus` for the whole service.

If no writable parent is found, cgroup capping is skipped (logged) and the board
still boots — so enabling it can never brick a launch.

Quotas (count limits, `0` = unlimited):
- **`HOLOBENCH_MAX_PER_USER`**, **`HOLOBENCH_MAX_SESSIONS`** → `429` at capacity.
- Reservations auto-reap at expiry; per-session overlays/uploads live under the
  session work dir — put it on a filesystem with quota if users can fill it.

## 4. Keep the boundary tight

- **`HOLOBENCH_ALLOW_CLIENT_ASSETS`** — leave **unset**. When unset, the API
  resolves boot artifacts only from the trusted profile id; a client cannot
  point QEMU at arbitrary host paths. Only set `=1` for trusted/CLI-only use.
- Don't expose the gdbstub/VNC ports off-host (gdb already binds `127.0.0.1`;
  QMP is a unix socket). The reverse proxy should publish only the web port.
- Treat profiles as trusted config (they choose the QEMU binary + argv). Only
  operators write `profiles/`.

## 5. Container deployment

The fat image bakes the board's QEMU + artifacts (see `README.md` → *Run as a
container*). For a shared service:

```bash
docker run -d --restart=unless-stopped \
  -p 127.0.0.1:8080:8080 \
  --memory=10g --cpus=4 \
  -e HOLOBENCH_SECRET="$(openssl rand -hex 32)" \
  -e HOLOBENCH_NICE=10 \
  -e HOLOBENCH_MAX_PER_USER=2 \
  -e HOLOBENCH_ALLOWED_ORIGINS=https://evk.example.com \
  -v holobench-data:/opt/holobench/data \
  ghcr.io/kylefoxaustin/holobench:imx95-sd
# then point your TLS reverse proxy at 127.0.0.1:8080
```

Create users with `docker exec <ctr> holobench user add ...` (persisted in the
`holobench-data` volume). The image uses TCG (no `/dev/kvm`).

## 6. Publishing caveat (Prime Directive)

The fat images embed the **forked** i.MX QEMU (the models aren't upstreamed
yet). Fine for internal/demo hosting; revisit public publishing once the machine
models land in stock QEMU. Holobench itself uses only standard QEMU interfaces.

## Environment variable reference

| Var | Purpose | Default |
|---|---|---|
| `HOLOBENCH_SECRET` | Token signing key (set in prod / multi-worker) | auto-persist to `data/secret` |
| `HOLOBENCH_USERS` | User store path | `data/users.yaml` |
| `HOLOBENCH_LOGIN_MAX_FAILS` / `_WINDOW_S` | Login brute-force throttle | 5 / 60 |
| `HOLOBENCH_ALLOWED_ORIGINS` | Console-WS origin allowlist | same-origin |
| `HOLOBENCH_NICE` | `nice` for each QEMU | 0 |
| `HOLOBENCH_CGROUP` | Enable per-session cgroup v2 caps (auto-detect parent) | off |
| `HOLOBENCH_CGROUP_PARENT` | Explicit delegated cgroup parent dir | auto |
| `HOLOBENCH_MEM_CAP_MB` | cgroup `memory.max` per board | guest RAM ×1.5 + 512 MB |
| `HOLOBENCH_CPU_CORES` | cgroup `cpu.max` per board (cores) | unset |
| `HOLOBENCH_PIDS_MAX` | cgroup `pids.max` per board | 512 |
| `HOLOBENCH_MAX_PER_USER` / `HOLOBENCH_MAX_SESSIONS` | Session quotas | 0 (∞) |
| `HOLOBENCH_AUDIT_LOG` | Append JSON audit events (login/launch/actions/user-mgmt) to this file | logger only |
| `HOLOBENCH_UPLOAD_QUOTA_MB` | Per-session upload cap (9p share + camera frames) | 0 (∞) |
| `HOLOBENCH_ALLOW_CLIENT_ASSETS` | Trust client asset paths (keep off) | off |
| `HOLOBENCH_QEMU` / `HOLOBENCH_ASSET_ROOT` / `HOLOBENCH_CAPTURE_DIR` | Path overrides (set in the image) | — |

## Hardening checklist

- [ ] At least one admin user created (auth enforced)
- [ ] `HOLOBENCH_SECRET` set (or single-instance persistent secret confirmed)
- [ ] TLS reverse proxy in front; Holobench bound to `127.0.0.1`
- [ ] `HOLOBENCH_ALLOWED_ORIGINS` set to the public origin
- [ ] Per-session cgroup caps on (`HOLOBENCH_CGROUP=1` + a delegated parent;
      verify `memory.max`/`pids.max`/`cpu.max` applied); `NICE` set
- [ ] Session quotas set; work dir on a quota'd filesystem
- [ ] `HOLOBENCH_ALLOW_CLIENT_ASSETS` unset; only operators write `profiles/`
- [ ] `data/` not world-readable; backups exclude it or encrypt it

<!-- SPDX-License-Identifier: GPL-2.0-or-later -->
# Deploying Holobench as a shared "virtual EVK" service

Holobench runs frictionless and open for local/dev use. This guide is the
**Phase-6 hardening** path for exposing it to multiple users on a network. Read
it top to bottom before binding to anything but `127.0.0.1`.

> Sizing a busy host / running many boards? See **[`docs/SCALING.md`](SCALING.md)**
> for measured per-board cost, the CPU/`cpuidle` density wall, and the
> admission-control / lazy-serial / cgroup knobs.

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

> **Compliance — read this first.** The Holobench image bakes in **only**
> redistributable bits: the OSS app + the **GPL** forked `qemu-system-aarch64`. It
> ships with **no NXP BSP artifacts** (`Image`, `*.dtb`, rootfs/initramfs, the
> i.MX95 M33 `m33_image.elf`, NXP `.ko`) — those are NXP-non-redistributable, so
> baking them into a pushed/shared layer redistributes NXP's binaries (and registry
> blobs persist even after a tag delete). You supply your **own** BSP and
> **volume-mount** it at run time. `docker/build.sh` refuses to build if a
> restricted-looking artifact is in its context.

The operator supplies a BSP directory laid out per board id and mounts it at
`/artifacts` (`HOLOBENCH_ASSET_ROOT`):

```
/srv/holobench-bsp/
  imx95-evk-sd/  Image  imx95-19x19-evk.dtb  disk.wic  m33_image_M2.elf
  imx93-evk-sd/  Image  imx93-11x11-evk.dtb  disk.wic
  imx91-evk-sd/  Image  imx91-11x11-evk.dtb  disk.wic
```

For a shared service:

```bash
docker run -d --restart=unless-stopped \
  -p 127.0.0.1:8080:8080 \
  --memory=10g --cpus=4 \
  -v /srv/holobench-bsp:/artifacts -e HOLOBENCH_ASSET_ROOT=/artifacts \
  -e HOLOBENCH_SECRET="$(openssl rand -hex 32)" \
  -e HOLOBENCH_NICE=10 \
  -e HOLOBENCH_MAX_PER_USER=2 \
  -e HOLOBENCH_ALLOWED_ORIGINS=https://evk.example.com \
  -v holobench-data:/opt/holobench/data \
  holobench:imx95-evk-sd
# then point your TLS reverse proxy at 127.0.0.1:8080
```

Create users with `docker exec <ctr> holobench user add ...` (persisted in the
`holobench-data` volume). The image uses TCG (no `/dev/kvm`).

### The i.MX95 M33 density fix (M=2 firmware)

The forked qemu baked into the image carries the WFI fix (upstream `6fd2fcdc61b` +
95's `power_state` hygiene); pair it with the **M=2** SM firmware
(`m33_image_M2.elf`) in your mounted BSP to drop an *idle* board from ~1.1 host
cores to ~0.15 (CPU-bound → RAM-bound; see `docs/SCALING.md`). The i.MX95 profile
references that elf via the `{asset_dir}` placeholder, so just drop your
`m33_image_M2.elf` into `/artifacts/imx95-evk-sd/`. Verify after boot with
`top -H -p <qemu_pid>` — no `CPU N/TCG` thread should peg while the guest is idle.

### Releasing / republishing the images

Cut a release with one command (images are now small — OSS + qemu only — and
freely publishable, since no NXP artifacts are baked):

```bash
tools/release.sh v0.3.0                                       # build all 3, tag rolling + pinned, push to GHCR
RELEASE_NOTES=notes.md tools/release.sh v0.3.0 --gh-release   # also cut the GitHub release
```

It runs on a host that has the forked qemu builds (the images bake the GPL binary),
not GitHub-hosted CI. No BSP/golden disk is needed to build anymore.

## 6. Publishing caveat (Prime Directive)

The images embed the **forked** i.MX QEMU (GPL — redistributable; the models
aren't upstreamed yet). Holobench itself uses only standard QEMU interfaces.
**Never** add NXP BSP artifacts back into the image (§5) — keep the boundary at
"redistributable bits in the image, operator supplies the restricted BSP."

## Environment variable reference

| Var | Purpose | Default |
|---|---|---|
| `HOLOBENCH_SECRET` | Token signing key (set in prod / multi-worker) | auto-persist to `data/secret` |
| `HOLOBENCH_USERS` | User store path | `data/users.yaml` |
| `HOLOBENCH_ADMIN_USER` / `HOLOBENCH_ADMIN_PASSWORD` | Seed an admin at startup (enables auth without a CLI step — ideal for containers) | unset |
| `HOLOBENCH_ALLOW_REGISTRATION` | Allow self-service signup (role *user*) from the login screen. First account on a fresh instance always registers as admin (onboarding) regardless | off |
| `HOLOBENCH_DEMO_LOGIN` | `user:pass` shown as a one-click demo box on the login screen (try-it; leave unset in prod) | unset |
| `HOLOBENCH_LOGIN_MAX_FAILS` / `_WINDOW_S` | Login brute-force throttle | 5 / 60 |
| `HOLOBENCH_ALLOWED_ORIGINS` | Console-WS origin allowlist | same-origin |
| `HOLOBENCH_NICE` | `nice` for each QEMU | 0 |
| `HOLOBENCH_CGROUP` | Enable per-session cgroup v2 caps (auto-detect parent) | off |
| `HOLOBENCH_CGROUP_PARENT` | Explicit delegated cgroup parent dir | auto |
| `HOLOBENCH_MEM_CAP_MB` | cgroup `memory.max` per board | guest RAM ×1.5 + 512 MB |
| `HOLOBENCH_CPU_CORES` | cgroup `cpu.max` per board (cores) | unset |
| `HOLOBENCH_PIDS_MAX` | cgroup `pids.max` per board | 512 |
| `HOLOBENCH_MAX_PER_USER` / `HOLOBENCH_MAX_SESSIONS` | Session quotas | 0 (∞) |
| `HOLOBENCH_MAX_CONCURRENT_LAUNCHES` | Cap concurrent in-flight launches (anti-stampede) | 0 (∞) |
| `HOLOBENCH_LAUNCH_STAGGER_S` | Free each admission slot only after this delay (paces boots in waves) | 0 |
| `HOLOBENCH_LAZY_SERIAL` | Tap serial only while a console is open (no always-on pump/board) | off |
| `HOLOBENCH_AUDIT_LOG` | Append JSON audit events (login/launch/actions/user-mgmt) to this file | logger only |
| `HOLOBENCH_UPLOAD_QUOTA_MB` | Per-session upload cap (9p share + camera frames) | 0 (∞) |
| `HOLOBENCH_ALLOW_CLIENT_ASSETS` | Trust client asset paths (keep off) | off |
| `HOLOBENCH_QEMU` / `HOLOBENCH_ASSET_ROOT` / `HOLOBENCH_CAPTURE_DIR` | Path overrides (set in the image) | — |

## Verify it

`tools/smoke-hardened.py` brings up a throwaway enforced-auth instance with
cgroup caps + upload quota + audit log and asserts the whole stack end-to-end
(401 without a token, login throttle, admin-only user API, per-session cgroup
created, session ownership 403s, upload-quota 413, audit events recorded):

    .venv/bin/python tools/smoke-hardened.py     # exits non-zero on any failure

(Needs a built dev checkout + cgroup v2 delegated to the user.)

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

#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-or-later
# Host wrapper for artifact source #3 (container build). The wizard runs this on a
# PTY so the operator sees the build + accepts the NXP EULA, then can detach. It:
#   1) builds the nxp-bsp builder image (OSS Yocto host, no NXP bits) if missing,
#   2) `docker run -i` it INTERACTIVELY with the board's NXP params + /out mounted,
# emitting Image + dtb + .wic + the SM firmware into the asset dir. The EULA is
# accepted inside the container by the operator — Holobench hosts/accepts nothing.
#
# Usage (invoked by SetupManager.start_container_build): build-nxp-bsp.sh <board> <out-dir>
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"; cd "$REPO"
BOARD="${1:?usage: build-nxp-bsp.sh <board> <out-dir>}"
OUT="${2:?need out dir}"
IMAGE="${NXP_BSP_IMAGE:-holobench-nxp-bsp:latest}"
NAME="hb-bsp-$BOARD"

# Core budget. A Yocto build is unbounded by default: bitbake sets BB_NUMBER_THREADS
# (parallel recipes) AND PARALLEL_MAKE (make -j per recipe) to nproc, so on an N-core
# host you get up to N*N concurrent compilers — enough to thrash CPU and OOM the host
# into an unstable/crash state. We bound it two ways: a HARD kernel cap via docker
# --cpus (the host can never lose more than this many cores no matter what bitbake
# spawns), AND a job-count cap passed in as BB_JOBS (limits concurrent processes ->
# bounds peak RAM, which --cpus alone does not). Default: half the host's cores.
# Override with HB_BUILD_JOBS=<n> to go higher (faster) or lower (gentler).
NPROC="$(nproc)"
HB_BUILD_JOBS="${HB_BUILD_JOBS:-$(( NPROC / 2 ))}"
[ "$HB_BUILD_JOBS" -ge 1 ] 2>/dev/null || HB_BUILD_JOBS=1

# Pull the board's NXP build params from build-sources.yaml (nxp_bsp: block).
eval "$(python3 - "tools/build-sources.yaml" "$BOARD" <<'PY'
import sys, yaml, shlex
e = (yaml.safe_load(open(sys.argv[1])) or {}).get(sys.argv[2], {}) or {}
b = e.get("nxp_bsp")
if not b:
    print("echo 'error: no nxp_bsp config for this board in build-sources.yaml (confirm with the emulator session)'; exit 1")
    sys.exit(0)
m = {"MACHINE":b.get("machine"),"DISTRO":b.get("distro"),"MANIFEST_BRANCH":b.get("manifest_branch"),
     "MANIFEST_XML":b.get("manifest_xml"),"IMAGE_TARGET":b.get("image_target"),"DTB_NAME":b.get("dtb_name"),
     "SM_CFG":b.get("sm_cfg",""),"SM_M":str(b.get("sm_m",2)),"SM_TAG":b.get("sm_tag","")}
for k,v in m.items():
    print(f"export {k}={shlex.quote(str(v or ''))}")
PY
)"

command -v docker >/dev/null || { echo "error: docker not found"; exit 1; }
echo "==> ensuring builder image $IMAGE (one-time, ~10 min apt; cached after)"
docker build -t "$IMAGE" -f docker/Dockerfile.nxp-bsp docker/

mkdir -p "$OUT"
# Persistent downloads + sstate cache (survives the --rm container) so a re-run
# after a transient fetch failure reuses what's already pulled and rebuilds
# incrementally instead of from scratch. Shared across boards/runs.
CACHE="${NXP_BSP_CACHE:-$HOME/.cache/holobench-nxp-bsp}"
mkdir -p "$CACHE"
# A previous run's container may still exist — running (e.g. draining a slow fetch),
# or exited but not yet reaped. A fresh `docker run --name` collides with it
# ("name already in use", exit 125). Force-remove any same-named container and WAIT
# until docker actually releases the name before starting, so a quick re-click /
# re-run can't race the old container's teardown. (^name$ = exact match; the docker
# name filter is a substring match otherwise.)
if docker ps -aq -f "name=^${NAME}$" 2>/dev/null | grep -q .; then
  echo "==> a container named '$NAME' from a prior run is still present — removing it"
  docker rm -f "$NAME" >/dev/null 2>&1 || true
fi
for _ in $(seq 1 40); do
  docker ps -aq -f "name=^${NAME}$" 2>/dev/null | grep -q . || break
  sleep 0.5
done
if docker ps -aq -f "name=^${NAME}$" 2>/dev/null | grep -q .; then
  echo "error: container '$NAME' could not be removed; clear it manually with 'docker rm -f $NAME' and retry" >&2
  exit 1
fi
echo "==> starting interactive NXP Yocto build for $BOARD (accept the EULA when prompted)"
echo "    MACHINE=$MACHINE DISTRO=$DISTRO  $MANIFEST_BRANCH/$MANIFEST_XML -> $IMAGE_TARGET"
echo "    cache (downloads + sstate, persists across runs): $CACHE"
echo "    core budget: $HB_BUILD_JOBS of $NPROC (docker --cpus hard cap + BB_JOBS; set HB_BUILD_JOBS to change)"
exec docker run -it --rm --name "$NAME" \
  --cpus="$HB_BUILD_JOBS" \
  -v "$OUT:/out" \
  -v "$CACHE:/cache" \
  -e MACHINE -e DISTRO -e MANIFEST_BRANCH -e MANIFEST_XML \
  -e IMAGE_TARGET -e DTB_NAME -e SM_CFG -e SM_M -e SM_TAG \
  -e BB_JOBS="$HB_BUILD_JOBS" \
  "$IMAGE"

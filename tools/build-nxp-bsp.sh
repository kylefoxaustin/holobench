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
#
# Three knobs, all overridable from the env (the wizard's "Advanced settings" sets
# these; see README "Bounding a container build"). Each accepts 0 / none to REMOVE
# that cap (uncapped == the old crash-prone behaviour — warned about in the UI):
#   HB_BUILD_JOBS  bitbake recipes in parallel + docker --cpus hard ceiling   (cores)
#   HB_MAKE_JOBS   make -j inside each recipe (PARALLEL_MAKE)                  (cores)
#   HB_BUILD_MEM   docker --memory hard ceiling, e.g. "48g"                   (RAM)
# CPU/jobs default to half the cores; make -j defaults to 4 (a low per-recipe value
# keeps peak compiler count — and thus peak RAM — sane); memory defaults to ~75% of
# host RAM, leaving headroom so the desktop never gets starved into a swap-thrash.
NPROC="$(nproc)"
HB_BUILD_JOBS="${HB_BUILD_JOBS:-$(( NPROC / 2 ))}"
[ "$HB_BUILD_JOBS" -ge 1 ] 2>/dev/null || HB_BUILD_JOBS=0   # non-numeric/blank -> uncapped
HB_MAKE_JOBS="${HB_MAKE_JOBS:-4}"
[ "$HB_MAKE_JOBS" -ge 1 ] 2>/dev/null || HB_MAKE_JOBS=0
MEM_GB="$(free -g 2>/dev/null | awk '/^Mem:/{print $2}')"
[ -n "$MEM_GB" ] && [ "$MEM_GB" -ge 1 ] 2>/dev/null || MEM_GB=0
HB_BUILD_MEM="${HB_BUILD_MEM:-$(( MEM_GB * 75 / 100 ))g}"   # ~75% of RAM, e.g. 70g

# HB_BUILD_TMPFS — RAM-back the bitbake build dir. Yocto's do_rootfs is fsync/random-IO
# heavy and collapses on a HDD (measured <2 fsync/s on a spinning disk here; on RAM
# fsync is ~free), so the rootfs assembly + image build crawl for hours. Mounting the
# build dir on a tmpfs runs them at memory speed. OFF by default (a cold from-scratch
# tmp can exceed RAM -> ENOSPC); set HB_BUILD_TMPFS=<size like 52g> to enable. Peak tmp
# for imx-image-full with WARM sstate is ~30-40G, so size above that with headroom. It
# counts toward --memory (HB_BUILD_MEM stays above it), and DL_DIR/SSTATE_DIR live on
# the /cache mount (disk), so only the hot tmp is in RAM.
HB_BUILD_TMPFS="${HB_BUILD_TMPFS:-0}"

# Translate the caps into docker flags + the BB_* env the entrypoint reads. A 0/none
# value drops the corresponding cap entirely (so the build can run uncapped on purpose).
DOCKER_CAPS=()
case "$HB_BUILD_JOBS" in 0|none|unlimited) BB_JOBS="$NPROC" ;; *) DOCKER_CAPS+=(--cpus="$HB_BUILD_JOBS"); BB_JOBS="$HB_BUILD_JOBS" ;; esac
case "$HB_MAKE_JOBS"  in 0|none|unlimited) BB_MAKE="$NPROC" ;; *) BB_MAKE="$HB_MAKE_JOBS" ;; esac
case "$HB_BUILD_MEM"  in 0|0g|none|unlimited|"") MEM_DESC="uncapped" ;; *) DOCKER_CAPS+=(--memory="$HB_BUILD_MEM"); MEM_DESC="$HB_BUILD_MEM" ;; esac
# tmpfs is mounted at the WHOLE build dir (not build/tmp) so the mountpoint IS the 1777
# tmpfs — avoids docker creating a root-owned build/ that the unprivileged `builder`
# user can't write conf/ into. exec is required (the build runs native tools from here);
# mode=1777 lets builder write; sstate/downloads are elsewhere (/cache), so only tmp lands here.
case "$HB_BUILD_TMPFS" in
  0|0g|none|off|"") TMPFS_DESC="off (build dir on disk)" ;;
  *) DOCKER_CAPS+=(--tmpfs "/home/builder/build:rw,exec,mode=1777,size=$HB_BUILD_TMPFS"); TMPFS_DESC="$HB_BUILD_TMPFS in RAM" ;;
esac

# Pull the board's NXP build params from build-sources.yaml (nxp_bsp: block).
# IMAGE_TARGETS = the space-joined list of depth variants the board declares
# (image_targets:, or the legacy singular image_target: as a 1-element fallback).
eval "$(python3 - "tools/build-sources.yaml" "$BOARD" <<'PY'
import sys, yaml, shlex
e = (yaml.safe_load(open(sys.argv[1])) or {}).get(sys.argv[2], {}) or {}
b = e.get("nxp_bsp")
if not b:
    print("echo 'error: no nxp_bsp config for this board in build-sources.yaml (confirm with the emulator session)'; exit 1")
    sys.exit(0)
targets = b.get("image_targets") or ([b["image_target"]] if b.get("image_target") else [])
m = {"MACHINE":b.get("machine"),"DISTRO":b.get("distro"),"MANIFEST_BRANCH":b.get("manifest_branch"),
     "MANIFEST_XML":b.get("manifest_xml"),"IMAGE_TARGETS":" ".join(targets),"DTB_NAME":b.get("dtb_name"),
     "SM_CFG":b.get("sm_cfg",""),"SM_M":str(b.get("sm_m",2)),"SM_TAG":b.get("sm_tag","")}
for k,v in m.items():
    print(f"export {k}={shlex.quote(str(v or ''))}")
PY
)"

# Select which variant(s) to build: HB_IMAGE_TARGET (the wizard's depth pick) if set,
# else the FIRST declared variant (the default). Space-separated -> build several in one
# run (the entrypoint loops + stages each). Every requested target must be declared.
[ -n "${IMAGE_TARGETS:-}" ] || { echo "error: no image_targets declared for $BOARD"; exit 1; }
IMAGE_TARGET="${HB_IMAGE_TARGET:-${IMAGE_TARGETS%% *}}"
for _t in $IMAGE_TARGET; do
  case " $IMAGE_TARGETS " in *" $_t "*) ;; *) echo "error: image target '$_t' is not one of $BOARD's declared variants ($IMAGE_TARGETS)"; exit 1 ;; esac
done
export IMAGE_TARGET

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
echo "    image variant: $IMAGE_TARGET   (available: $IMAGE_TARGETS; override with HB_IMAGE_TARGET / the wizard)"
echo "    cache (downloads + sstate, persists across runs): $CACHE"
echo "    resource caps: cpu=${HB_BUILD_JOBS} of ${NPROC} cores | make -j${BB_MAKE} | mem=${MEM_DESC} | bitbake pressure-throttled"
echo "    build dir (tmp): ${TMPFS_DESC}"
echo "    (override via HB_BUILD_JOBS / HB_MAKE_JOBS / HB_BUILD_MEM / HB_BUILD_TMPFS or the wizard's Advanced settings; 0=uncapped)"
# HB_FETCH_ONLY — pre-cache mode: the entrypoint fetches every source into the
# persistent /cache (DL_DIR) and exits WITHOUT building. Run this first on a fresh
# machine so the real build never depends on the network for sources (restart-proof,
# offline-capable). Empty/0 -> normal build. Passed straight through to the container.
case "${HB_FETCH_ONLY:-}" in 1|true) echo "    mode: PRE-CACHE ONLY (fetch all sources, no build)" ;; esac
exec docker run -it --rm --name "$NAME" \
  "${DOCKER_CAPS[@]}" \
  -v "$OUT:/out" \
  -v "$CACHE:/cache" \
  -e MACHINE -e DISTRO -e MANIFEST_BRANCH -e MANIFEST_XML \
  -e IMAGE_TARGET -e DTB_NAME -e SM_CFG -e SM_M -e SM_TAG \
  -e BB_JOBS="$BB_JOBS" -e BB_MAKE="$BB_MAKE" \
  -e HB_FETCH_ONLY="${HB_FETCH_ONLY:-}" \
  "$IMAGE"

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
     "SM_CFG":b.get("sm_cfg",""),"SM_M":str(b.get("sm_m",2))}
for k,v in m.items():
    print(f"export {k}={shlex.quote(str(v or ''))}")
PY
)"

command -v docker >/dev/null || { echo "error: docker not found"; exit 1; }
echo "==> ensuring builder image $IMAGE (one-time, ~10 min apt; cached after)"
docker build -t "$IMAGE" -f docker/Dockerfile.nxp-bsp docker/

mkdir -p "$OUT"
# Remove a stale same-named container so re-runs/stop work cleanly.
docker rm -f "$NAME" >/dev/null 2>&1 || true
echo "==> starting interactive NXP Yocto build for $BOARD (accept the EULA when prompted)"
echo "    MACHINE=$MACHINE DISTRO=$DISTRO  $MANIFEST_BRANCH/$MANIFEST_XML -> $IMAGE_TARGET"
exec docker run -i --rm --name "$NAME" \
  -v "$OUT:/out" \
  -e MACHINE -e DISTRO -e MANIFEST_BRANCH -e MANIFEST_XML \
  -e IMAGE_TARGET -e DTB_NAME -e SM_CFG -e SM_M \
  "$IMAGE"

#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-or-later
#
# "Build me a board" — turnkey image build for a new user with only Docker.
# Builds the GPL forked QEMU from source (per tools/build-sources.yaml) and the
# distributable Holobench runtime, in one multi-stage docker build. NO NXP BSP is
# baked; you supply artifacts at run time (your own BSP, or — later — an OSS demo).
# See docs/SETUP.md.
#
# Usage:
#   tools/build-me.sh <board> [--bsp DIR] [--demo] [--plan]
#   tools/build-me.sh imx95-evk-sd --plan          # show what it would do
#   tools/build-me.sh imx91-evk-sd --bsp ~/my-bsp  # build, then print run cmd
#
# <board> is a profile id present in tools/build-sources.yaml.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"; cd "$REPO"
SOURCES="tools/build-sources.yaml"

BOARD="${1:?usage: build-me.sh <board> [--bsp DIR] [--demo] [--plan]}"; shift || true
BSP_DIR=""; DEMO=0; PLAN=0
while [ $# -gt 0 ]; do
  case "$1" in
    --bsp) BSP_DIR="${2:?--bsp needs a directory}"; shift 2 ;;
    --demo) DEMO=1; shift ;;
    --plan) PLAN=1; shift ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

[ -f "profiles/$BOARD.yaml" ] || { echo "error: no profile 'profiles/$BOARD.yaml'"; exit 1; }

# Resolve the qemu source for this board from build-sources.yaml.
read -r REPO_URL REF TARGETS STOCK < <(python3 - "$SOURCES" "$BOARD" <<'PY'
import sys, yaml
src = yaml.safe_load(open(sys.argv[1])) or {}
e = src.get(sys.argv[2])
if not e:
    print("__MISSING__ . . ."); sys.exit(0)
if e.get("stock"):
    print(f". . . stock>={e['stock'].get('min_version','?')}"); sys.exit(0)
q = e["qemu"]
print(q["repo"], q["ref"], q.get("target_list", "aarch64-softmmu"), ".")
PY
)

if [ "$REPO_URL" = "__MISSING__" ]; then
  echo "error: no build source for '$BOARD' in $SOURCES"; exit 1
fi

IMAGE="${IMAGE:-holobench:$BOARD}"

echo "== build-me: $BOARD =="
if [ "$STOCK" != "." ]; then
  echo "  qemu: STOCK ($STOCK) — this board is upstreamed; no source build needed."
  echo "  (TODO: stock path uses the image's apt qemu; build via docker/build.sh for now.)"
  exit 0
fi
echo "  qemu source : $REPO_URL @ $REF  (target: $TARGETS)"
echo "  image       : $IMAGE"
if [ -n "$BSP_DIR" ]; then echo "  artifacts   : your BSP at $BSP_DIR"
elif [ "$DEMO" = 1 ]; then echo "  artifacts   : OSS demo bundle (fetched via tools/fetch-oss-demo.sh after build)"
else echo "  artifacts   : none chosen yet (pass --bsp DIR, or --demo)"; fi

system_arm=$(python3 - "$SOURCES" "$BOARD" <<'PY'
import sys, yaml
e=(yaml.safe_load(open(sys.argv[1])) or {}).get(sys.argv[2],{})
print("1" if e.get("qemu",{}).get("system_arm") else "0")
PY
)

RUN_HINT="docker run --rm -p 8080:8080 -v ${BSP_DIR:-/my/bsp}:/artifacts -e HOLOBENCH_ASSET_ROOT=/artifacts $IMAGE"

if [ "$PLAN" = 1 ]; then
  echo
  echo "PLAN (no build performed):"
  echo "  1. docker build -f docker/Dockerfile.buildme \\"
  echo "       --build-arg QEMU_REPO=$REPO_URL --build-arg QEMU_REF=$REF \\"
  echo "       --build-arg QEMU_TARGETS=$TARGETS -t $IMAGE <staged-context>"
  echo "  2. $RUN_HINT"
  [ "$system_arm" = 1 ] && echo "  note: builds qemu-system-arm (MCU); runtime expects it at the same path."
  exit 0
fi

# Stage a clean build context (app + selected profile only — NO qemu, NO assets;
# qemu is built in the Dockerfile's stage 1). Mirrors docker/build.sh's hygiene.
STAGE="$(mktemp -d)"; trap 'rm -rf "$STAGE"' EXIT
cp -rL backend frontend docs tools vendor README.md CLAUDE.md ROADMAP.md LICENSE "$STAGE"/ 2>/dev/null || true
mkdir -p "$STAGE/profiles"; cp -L "profiles/$BOARD.yaml" "$STAGE/profiles/"
cp docker/Dockerfile.buildme "$STAGE"/Dockerfile.buildme
[ -f docker/.dockerignore ] && cp docker/.dockerignore "$STAGE"/.dockerignore
# Compliance guard: refuse if a restricted-looking artifact slipped into context.
for stray in Image "*.dtb" "*.elf" rootfs initrd.cpio.gz disk.img disk.wic; do
  f="$(find "$STAGE" -name "$stray" -not -path "*/vendor/*" 2>/dev/null | head -1)"
  [ -z "$f" ] || { echo "error: restricted-looking artifact in context: $f"; exit 1; }
done

echo; echo "building (qemu from source — first run ~20-40 min, then cached) ..."
docker build -f "$STAGE/Dockerfile.buildme" \
  --build-arg QEMU_REPO="$REPO_URL" \
  --build-arg QEMU_REF="$REF" \
  --build-arg QEMU_TARGETS="$TARGETS" \
  -t "$IMAGE" "$STAGE"

if [ "$DEMO" = 1 ]; then
  echo; echo "fetching OSS demo artifacts ..."
  if tools/fetch-oss-demo.sh "$BOARD" "oss-demo/$BOARD"; then
    RUN_HINT="docker run --rm -p 8080:8080 -v $REPO/oss-demo/$BOARD:/artifacts/$BOARD:ro -e HOLOBENCH_ASSET_ROOT=/artifacts $IMAGE"
  else
    echo "  (OSS demo unavailable — supply your own BSP with --bsp DIR instead)"
  fi
fi

echo; echo "Built $IMAGE (OSS app + GPL qemu from source; no NXP BSP). Run it:"
echo "  $RUN_HINT"
echo "  # put your BSP in <dir>/$BOARD/{Image,*.dtb,rootfs,...}  (m33_image_M2.elf for imx95)"
echo "  # open http://localhost:8080"

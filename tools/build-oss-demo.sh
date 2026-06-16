#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-or-later
#
# Package a board's fully-OSS demo boot artifacts into a publishable bundle for
# tools/fetch-oss-demo.sh. Separation of concerns:
#   * PRODUCING the artifacts (a mainline/upstream kernel + dtb that boots the
#     model + a buildroot/busybox rootfs) is the emulator session's recipe — they
#     own what boots their model and are building exactly this for upstreaming.
#     All inputs must be OSS/redistributable (NO NXP BSP). Drop them in a dir.
#   * PACKAGING them (this script) tars + sha256s the dir and prints the
#     build-sources.yaml `oss_demo:` snippet to paste once you upload the bundle
#     (e.g. as a GitHub release asset).
#
# Usage:
#   tools/build-oss-demo.sh <board> <staged-oss-dir> [out-dir]
#     <staged-oss-dir> holds the OSS Image / *.dtb / rootfs(or initramfs) for the
#     board — laid out exactly as a guest expects under <bsp>/<board>/.
#
# Example (once the 91/93 session hands over an OSS kernel+dtb+rootfs):
#   tools/build-oss-demo.sh imx93-evk-sd /tmp/imx93-oss   ->   dist/oss-demo-imx93-evk-sd.tar.gz
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"; cd "$REPO"
BOARD="${1:?usage: build-oss-demo.sh <board> <staged-oss-dir> [out-dir]}"
SRC="${2:?need the staged OSS artifact dir}"
OUT="${3:-$REPO/dist}"

[ -d "$SRC" ] || { echo "error: no staged dir: $SRC"; exit 1; }

# Refuse to package anything that looks like an NXP-built BSP binary by name/marker.
# (Heuristic guard — the human still confirms the inputs are OSS.)
if find "$SRC" -name 'm33_image*' -o -name '*imx-sm*' 2>/dev/null | grep -q .; then
  echo "error: refusing — found an M33/imx-sm firmware (NXP, non-redistributable) in $SRC"; exit 1
fi
[ -f "$SRC/Image" ] || echo "warn: no kernel 'Image' in $SRC (continuing — boot recipe is the emulator's call)"

mkdir -p "$OUT"
bundle="$OUT/oss-demo-$BOARD.tar.gz"
tar -czf "$bundle" -C "$SRC" .
sha="$(sha256sum "$bundle" | cut -d' ' -f1)"

echo "packaged: $bundle"
echo "sha256:   $sha"
echo "contents: $(tar -tzf "$bundle" | tr '\n' ' ')"
echo
echo "Upload $bundle (e.g. a GitHub release asset), then set in tools/build-sources.yaml:"
echo "  $BOARD:"
echo "    oss_demo: { url: \"<download-url>\", sha256: \"$sha\" }"
echo
echo "Then: tools/fetch-oss-demo.sh $BOARD   (or the wizard's 'OSS demo image' option)."

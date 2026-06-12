#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# Build a fat, self-contained Holobench "virtual EVK" image.
#
# Usage:
#   docker/build.sh [QEMU_BOARD] [ASSET_BOARD ...]
#
#   QEMU_BOARD   profile whose forked qemu-system-aarch64 to BAKE IN  (default: imx91-evk)
#   ASSET_BOARD  profiles whose boot artifacts to bake in             (default: same as QEMU_BOARD)
#
# The baked qemu must register the machine of every ASSET_BOARD. The imx91 build
# registers BOTH imx91-11x11-evk and imx93-11x11-evk, so:
#   docker/build.sh imx91-evk imx91-evk imx93-evk
# yields a 2-board image. (i.MX95 needs its own qemu + M33 firmware — bake it as
# its own image: docker/build.sh imx95-evk.)
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"

QEMU_BOARD="${1:-imx91-evk}"; shift || true
ASSET_BOARDS=("$@"); [ ${#ASSET_BOARDS[@]} -eq 0 ] && ASSET_BOARDS=("$QEMU_BOARD")
IMAGE="${IMAGE:-holobench:$QEMU_BOARD}"

# Resolve the forked qemu binary from the QEMU_BOARD profile's qemu.binary.
QEMU_BIN="$(grep -E '^\s*binary:' "profiles/$QEMU_BOARD.yaml" | head -1 | sed -E 's/^\s*binary:\s*//; s/\s+#.*//' | tr -d '"')"
[ -x "$QEMU_BIN" ] || { echo "error: qemu binary not found/executable: $QEMU_BIN (is the $QEMU_BOARD emulator built?)"; exit 1; }

STAGE="$(mktemp -d)"; trap 'rm -rf "$STAGE"' EXIT
echo "staging build context in $STAGE ..."

# App (dereference any symlinks; skip venv/caches/big assets).
cp -rL backend frontend profiles docs tools vendor README.md CLAUDE.md ROADMAP.md "$STAGE"/ 2>/dev/null || true
# Ensure the GPL-2.0 capture helpers exist (staged into guests at /mnt). Build if
# the cross-compiler is present and they're missing; warn (don't fail) otherwise.
if [ ! -x vendor/camera/bin/imx95-isi-capture ] && command -v aarch64-linux-gnu-gcc >/dev/null; then
  tools/build-capture-helpers.sh && cp -rL vendor "$STAGE"/ 2>/dev/null || true
fi
[ -x "$STAGE/vendor/camera/bin/imx95-isi-capture" ] || echo "  warn: camera capture helpers not baked (build with tools/build-capture-helpers.sh)"
cp docker/Dockerfile "$STAGE"/Dockerfile
cp docker/.dockerignore "$STAGE"/.dockerignore

# Baked forked QEMU.
mkdir -p "$STAGE/qemu"
cp -L "$QEMU_BIN" "$STAGE/qemu/qemu-system-aarch64"

# Baked boot artifacts (real files, not the repo's symlinks).
for b in "${ASSET_BOARDS[@]}"; do
  src="assets/$b"
  [ -d "$src" ] || { echo "error: no staged assets at $src (run tools/make-initramfs.sh + symlink Image/dtb first)"; exit 1; }
  mkdir -p "$STAGE/assets/$b"
  cp -L "$src"/Image "$STAGE/assets/$b/"
  cp -L "$src"/*.dtb "$STAGE/assets/$b/"
  # Optional artifacts — boards vary: initramfs boot vs SD/disk boot vs data disk.
  for opt in initrd.cpio.gz disk.img disk.wic; do
    [ -f "$src/$opt" ] && cp -L "$src/$opt" "$STAGE/assets/$b/"
  done
  echo "  baked assets: $b ($(ls "$STAGE/assets/$b" | tr '\n' ' '))"
done

# Bake any loader firmware referenced in the QEMU_BOARD profile's extra_args
# (e.g. the i.MX95 M33 System Manager elf, a host-absolute path) and rewrite the
# staged profile to point at the in-container copy.
loader_file="$(grep -oE 'loader,file=[^",]+' "profiles/$QEMU_BOARD.yaml" | head -1 | sed 's/loader,file=//')"
if [ -n "$loader_file" ] && [ -f "$loader_file" ]; then
  mkdir -p "$STAGE/extra"
  cp -L "$loader_file" "$STAGE/extra/$(basename "$loader_file")"
  sed -i "s|$loader_file|/opt/holobench/extra/$(basename "$loader_file")|" \
    "$STAGE/profiles/$QEMU_BOARD.yaml"
  echo "  baked loader firmware: $(basename "$loader_file")"
fi

echo "building $IMAGE (board qemu: $QEMU_BOARD; boards: ${ASSET_BOARDS[*]}) ..."
docker build -t "$IMAGE" "$STAGE"
echo
echo "Built $IMAGE. Run it:"
echo "  docker run --rm -p 8080:8080 $IMAGE"
echo "  # open http://localhost:8080  (auth: add -e HOLOBENCH_TOKEN=secret)"

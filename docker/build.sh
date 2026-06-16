#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-or-later
# Build a DISTRIBUTABLE Holobench "virtual EVK" image.
#
# COMPLIANCE (do not regress): this image bakes ONLY freely-redistributable bits —
# the OSS Holobench app + the GPL forked qemu binary. It NEVER bakes NXP BSP
# artifacts (kernel Image, board dtb, rootfs/initramfs, the i.MX95 M33 SM firmware
# m33_image.elf, or NXP-derived .ko). Those are NXP-non-redistributable: baking
# them into a layer and pushing/serving the image = redistributing NXP binaries,
# which their license forbids (and registry blobs persist even after a tag delete).
# Instead the operator VOLUME-MOUNTS their own BSP at runtime:
#   docker run -v /my/bsp:/artifacts -e HOLOBENCH_ASSET_ROOT=/artifacts ...
# with /my/bsp/<board>/{Image,*.dtb,rootfs,m33_image_M2.elf,...}. See docs/DEPLOY.md.
#
# Usage:
#   docker/build.sh [QEMU_BOARD] [ASSET_BOARD ...]
#
#   QEMU_BOARD   profile whose forked qemu-system-aarch64 to BAKE IN  (default: imx91-evk)
#   ASSET_BOARD  profiles whose PROFILE (not artifacts) to advertise  (default: same as QEMU_BOARD)
#
# The baked qemu must register the machine of every ASSET_BOARD. The imx91 build
# registers BOTH imx91-11x11-evk and imx93-11x11-evk, so:
#   docker/build.sh imx91-evk imx91-evk imx93-evk
# yields a 2-board image. (i.MX95 needs its own qemu — build it as its own image:
# docker/build.sh imx95-evk.)
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

# App (dereference any symlinks; skip venv/caches/big assets). NOTE: profiles are
# copied selectively below, NOT wholesale — the image must only advertise boards it
# can actually launch (the baked qemu serves QEMU_BOARD + ASSET_BOARDS), or the
# picker lists profiles that error on boot (other SoCs / the MCU / virt-smoke).
cp -rL backend frontend docs tools vendor README.md CLAUDE.md ROADMAP.md LICENSE "$STAGE"/ 2>/dev/null || true
mkdir -p "$STAGE/profiles"
for prof in "$QEMU_BOARD" "${ASSET_BOARDS[@]}"; do
  [ -f "profiles/$prof.yaml" ] && cp -L "profiles/$prof.yaml" "$STAGE/profiles/"
done
echo "  baked profiles (launchable only): $(cd "$STAGE/profiles" && ls *.yaml | tr '\n' ' ')"
# Ensure the GPL-2.0 capture helpers exist (staged into guests at /mnt). Build if
# the cross-compiler is present and they're missing; warn (don't fail) otherwise.
if [ ! -x vendor/camera/bin/imx95-isi-capture ] && command -v aarch64-linux-gnu-gcc >/dev/null; then
  tools/build-capture-helpers.sh && cp -rL vendor "$STAGE"/ 2>/dev/null || true
fi
[ -x "$STAGE/vendor/camera/bin/imx95-isi-capture" ] || echo "  warn: camera capture helpers not baked (build with tools/build-capture-helpers.sh)"
cp docker/Dockerfile "$STAGE"/Dockerfile
cp docker/.dockerignore "$STAGE"/.dockerignore

# Baked forked QEMU (GPL — freely redistributable).
mkdir -p "$STAGE/qemu"
cp -L "$QEMU_BIN" "$STAGE/qemu/qemu-system-aarch64"

# NXP BSP boot artifacts (Image / *.dtb / rootfs / initramfs / *.ko) and the
# i.MX95 M33 SM firmware (m33_image.elf, referenced via {asset_dir} in the
# profile's extra_args) are DELIBERATELY NOT baked — they are NXP-non-
# redistributable. The operator volume-mounts them at runtime:
#   docker run -v /my/bsp:/artifacts -e HOLOBENCH_ASSET_ROOT=/artifacts $IMAGE
# Boot artifacts resolve from $HOLOBENCH_ASSET_ROOT/<board>/ (loader.default_asset_dir);
# the M33 elf from the same dir via the profile's {asset_dir} placeholder.
# Sanity-check the operator didn't accidentally leave restricted files in context:
for stray in Image "*.dtb" "*.elf" rootfs initrd.cpio.gz disk.img disk.wic; do
  found="$(find "$STAGE" -name "$stray" -not -path "*/vendor/*" 2>/dev/null | head -1)"
  [ -z "$found" ] || { echo "error: refusing to build — restricted-looking artifact in context: $found"; exit 1; }
done

echo "building $IMAGE (board qemu: $QEMU_BOARD; advertises: ${ASSET_BOARDS[*]}; NO baked BSP) ..."
docker build -t "$IMAGE" "$STAGE"
echo
echo "Built $IMAGE (distributable: OSS app + GPL qemu only, no NXP artifacts). Run it:"
echo "  docker run --rm -p 8080:8080 -v /my/bsp:/artifacts -e HOLOBENCH_ASSET_ROOT=/artifacts $IMAGE"
echo "  # /my/bsp/<board>/ supplies your own Image/*.dtb/rootfs (+ m33_image_M2.elf for imx95)"
echo "  # open http://localhost:8080  (open mode; auth+admin: -e HOLOBENCH_ADMIN_USER=admin -e HOLOBENCH_ADMIN_PASSWORD=secret)"

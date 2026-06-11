#!/bin/bash
# Build the multi-board Holobench image: i.MX 91 + 93 + 95 in one container,
# each booting on its own baked QEMU build. One `docker run` → all three boards
# show ready in the picker.
#
#   docker/build-multi.sh           # -> holobench:imx9x-all  (91 + 93 + 95)
#
# Routing: the i.MX91 build registers BOTH imx91 + imx93 machines, so it serves
# both; the i.MX95 build serves imx95. We bake both, plus the 95's M33 System
# Manager firmware, and rewrite each staged profile's `binary:` (and the 95's
# loader path) to the in-container locations. No HOLOBENCH_QEMU override.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"; cd "$REPO"
IMAGE="${IMAGE:-holobench:imx9x-all}"

QEMU91=~/Documents/GitHub/91emulator/build/qemu-system-aarch64     # imx91 + imx93
QEMU95=~/Documents/GitHub/95emulator/build/qemu-system-aarch64     # imx95
M33=~/Documents/nxp/sources/imx-sm/build/mx95evk/m33_image.elf      # 95 SM firmware
for f in "$QEMU91" "$QEMU95" "$M33"; do
  [ -e "$f" ] || { echo "error: missing $f (is that emulator built?)"; exit 1; }
done

STAGE="$(mktemp -d)"; trap 'rm -rf "$STAGE"' EXIT
echo "staging multi-board context in $STAGE ..."
cp -rL backend frontend profiles docs tools README.md CLAUDE.md ROADMAP.md "$STAGE"/ 2>/dev/null || true
cp docker/Dockerfile.multi "$STAGE"/Dockerfile
cp docker/.dockerignore "$STAGE"/.dockerignore

# Bake the two QEMU builds + the M33 firmware.
mkdir -p "$STAGE/qemu/a" "$STAGE/qemu/imx95" "$STAGE/extra"
cp -L "$QEMU91" "$STAGE/qemu/a/qemu-system-aarch64"
cp -L "$QEMU95" "$STAGE/qemu/imx95/qemu-system-aarch64"
cp -L "$M33"    "$STAGE/extra/m33_image.elf"

# Rewrite staged profiles -> in-container binary paths (and the 95 M33 path).
sed -i -E 's|^([[:space:]]*binary:[[:space:]]*).*|\1/opt/holobench/qemu/a/qemu-system-aarch64|' \
  "$STAGE/profiles/imx91-evk.yaml" "$STAGE/profiles/imx93-evk.yaml"
sed -i -E 's|^([[:space:]]*binary:[[:space:]]*).*|\1/opt/holobench/qemu/imx95/qemu-system-aarch64|' \
  "$STAGE/profiles/imx95-evk.yaml"
sed -i 's|/home/kyle/Documents/nxp/sources/imx-sm/build/mx95evk/m33_image.elf|/opt/holobench/extra/m33_image.elf|' \
  "$STAGE/profiles/imx95-evk.yaml"

# Bake assets for the three boards.
for b in imx91-evk imx93-evk imx95-evk; do
  src="assets/$b"; [ -d "$src" ] || { echo "error: no staged assets at $src"; exit 1; }
  mkdir -p "$STAGE/assets/$b"
  cp -L "$src"/Image "$STAGE/assets/$b/"
  cp -L "$src"/*.dtb "$STAGE/assets/$b/"
  for opt in initrd.cpio.gz disk.img disk.wic; do
    [ -f "$src/$opt" ] && cp -L "$src/$opt" "$STAGE/assets/$b/"
  done
  echo "  baked $b: $(ls "$STAGE/assets/$b" | tr '\n' ' ')"
done

echo "building $IMAGE (imx91 + imx93 on the 91 build; imx95 on the 95 build) ..."
docker build -t "$IMAGE" "$STAGE"
echo
echo "Built $IMAGE. Run it:"
echo "  docker run --rm -p 8080:8080 $IMAGE   # → http://localhost:8080 (all 3 boards)"

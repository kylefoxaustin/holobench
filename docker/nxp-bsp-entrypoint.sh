#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-or-later
# Runs INSIDE the nxp-bsp builder container (docker run). Does the real NXP i.MX
# Yocto build + the SM firmware, then stages the 4 artifacts into /out (a mounted
# asset dir). INTERACTIVE: `source imx-setup-release.sh` prompts for the NXP EULA on
# the PTY — the operator accepts it here (we never auto-accept). Nothing NXP ships
# in Holobench; this builds it on the operator's machine from NXP's own sources.
#
# Params via env (set by tools/build-nxp-bsp.sh from build-sources.yaml nxp_bsp):
#   MACHINE        e.g. imx95-19x19-lpddr5-evk
#   DISTRO         e.g. fsl-imx-wayland
#   MANIFEST_BRANCH e.g. imx-linux-walnascar
#   MANIFEST_XML   e.g. imx-6.12.3-1.0.0.xml
#   IMAGE_TARGET   e.g. imx-image-full
#   DTB_NAME       e.g. imx95-19x19-evk.dtb   (deploy artifact name)
#   SM_CFG         e.g. mx95evk   (imx-sm build cfg; empty -> skip SM)
#   SM_M           e.g. 2
# CONFIRM these per board with the emulator session before a real run (§7).
set -euo pipefail
: "${MACHINE:?}" "${DISTRO:?}" "${MANIFEST_BRANCH:?}" "${MANIFEST_XML:?}" "${IMAGE_TARGET:?}" "${DTB_NAME:?}"
OUT=/out; mkdir -p "$OUT"
cd "$HOME"

echo "==> repo init ($MANIFEST_BRANCH / $MANIFEST_XML) + sync (this pulls GBs)"
repo init -u https://github.com/nxp-imx/imx-manifest -b "$MANIFEST_BRANCH" -m "$MANIFEST_XML"
repo sync -j"$(nproc)"

echo "==> imx-setup-release (NXP EULA prompt — accept it to continue)"
# This is the interactive EULA gate; do NOT pre-accept. MACHINE/DISTRO select the build.
MACHINE="$MACHINE" DISTRO="$DISTRO" source ./imx-setup-release.sh -b build

echo "==> bitbake $IMAGE_TARGET (multi-hour)"
bitbake "$IMAGE_TARGET"

DEPLOY="tmp/deploy/images/$MACHINE"
echo "==> staging artifacts from $DEPLOY -> $OUT"
cp -L "$DEPLOY/Image" "$OUT/Image"
cp -L "$DEPLOY/$DTB_NAME" "$OUT/$DTB_NAME"
# rootfs SD image (.wic / .wic.zst): take the image-target .wic, decompressed.
wic="$(ls "$DEPLOY/$IMAGE_TARGET-$MACHINE".wic* 2>/dev/null | head -1 || true)"
[ -n "$wic" ] || wic="$(ls "$DEPLOY"/*.wic* 2>/dev/null | head -1)"
case "$wic" in
  *.zst) zstd -d -f "$wic" -o "$OUT/disk.wic" ;;
  *)     cp -L "$wic" "$OUT/disk.wic" ;;
esac

if [ -n "${SM_CFG:-}" ]; then
  echo "==> building SM firmware (imx-sm cfg=$SM_CFG M=${SM_M:-2}) — creds-free"
  git clone --depth 1 https://github.com/nxp-imx/imx-sm "$HOME/imx-sm"
  make -C "$HOME/imx-sm" cfg="$SM_CFG" M="${SM_M:-2}"
  cp -L "$HOME/imx-sm/build/$SM_CFG/m33_image.elf" "$OUT/m33_image_M2.elf"
fi

echo "==> BSP BUILD COMPLETE — artifacts in the mounted asset dir:"
ls -la "$OUT"

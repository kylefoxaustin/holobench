#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# Generate the i.MX95 "LCD attached" device tree used by Holobench's Attach LCD
# button. The stock imx95-19x19-evk is faithfully panel-less (DPU on, no panel ->
# DRM "Cannot find any crtc or sizes", dark LCD). This produces a sibling dtb that
# splices in the EVK reference 1280x800 LVDS chain so the DPU scans out.
#
# It's a thin wrapper around the 95 emulator's own panel-attach script (the
# canonical, validated splice) — Holobench just drives it. Re-run whenever the
# base dtb changes. The output is a generated asset (gitignored, like the other
# dtbs); docker/build.sh bakes whatever *.dtb is present in the asset dir.
#
# Usage: tools/make-lcd-dtb.sh [ASSET_DIR] [BASE_DTB] [OUT_DTB]
#   defaults: assets/imx95-evk-sd  imx95-19x19-evk.dtb  imx95-19x19-evk-lcd.dtb
#
# Env:
#   ATTACH_LCD  path to 95emulator's tests/lcd-panel/attach-lcd.sh
#   DTC         path to dtc (else auto-detected by attach-lcd.sh)
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
ASSET_DIR="${1:-$REPO/assets/imx95-evk-sd}"
BASE="${2:-imx95-19x19-evk.dtb}"
OUT="${3:-imx95-19x19-evk-lcd.dtb}"

ATTACH_LCD="${ATTACH_LCD:-$HOME/Documents/GitHub/95emulator/tests/lcd-panel/attach-lcd.sh}"
DTC="${DTC:-$HOME/Documents/linux-imx95-build/scripts/dtc/dtc}"

[ -x "$ATTACH_LCD" ] || { echo "error: attach-lcd.sh not found at $ATTACH_LCD (set ATTACH_LCD=)" >&2; exit 1; }
base_path="$(readlink -f "$ASSET_DIR/$BASE")"
[ -f "$base_path" ] || { echo "error: base dtb not found: $ASSET_DIR/$BASE" >&2; exit 1; }

DTC="$DTC" "$ATTACH_LCD" "$base_path" "$ASSET_DIR/$OUT"
echo "wrote $ASSET_DIR/$OUT"

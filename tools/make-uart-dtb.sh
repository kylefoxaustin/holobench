#!/bin/bash
# Generate the i.MX91 UART-link DTB: the base EVK dtb with LPUART2
# (serial@44390000 / DT alias serial1 / guest /dev/ttyLP1) flipped from
# status="disabled" to "okay", so a board-to-board UART link can use it as its
# 2nd -serial. Mirrors the runtime patch in 91emulator's
# tests/interconnect-imx91/run-uart.sh (the proven, byte-exact UART link),
# but bakes it into a stable asset the profile references (the same pattern as
# the imx95 LCD-attach dtb). Standard dtc decompile -> one-line flip -> recompile;
# no machine-model change (Prime Directive).
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
ASSET_DIR="${1:-$REPO/assets/imx91-evk-sd}"
BASE="${2:-imx91-11x11-evk.dtb}"
OUT="${3:-imx91-11x11-evk-uartlink.dtb}"
DTC="${DTC:-/home/kyle/Documents/nxp/linux/imx-yocto-bsp/build-imx91/tmp/work-shared/imx91evk/kernel-build-artifacts/scripts/dtc/dtc}"

[ -x "$DTC" ] || { echo "error: dtc not found at $DTC (set DTC=)" >&2; exit 1; }
base_path="$(readlink -f "$ASSET_DIR/$BASE")"
[ -f "$base_path" ] || { echo "error: base dtb not found: $ASSET_DIR/$BASE" >&2; exit 1; }

work="$(mktemp -d)"; trap 'rm -rf "$work"' EXIT
"$DTC" -I dtb -O dts "$base_path" 2>/dev/null > "$work/base.dts"
# Flip LPUART2 (serial@44390000) disabled -> okay. The serial1 alias already
# points here, so the kernel exposes it as /dev/ttyLP1.
awk '
  /serial@44390000 \{/ { inn = 1 }
  inn && /status = "disabled"/ { sub(/disabled/, "okay"); inn = 0 }
  { print }
' "$work/base.dts" > "$work/uart.dts"
grep -q 'serial@44390000' "$work/uart.dts" || { echo "error: serial@44390000 not in base dtb" >&2; exit 1; }
"$DTC" -I dts -O dtb "$work/uart.dts" 2>/dev/null > "$ASSET_DIR/$OUT"
echo "wrote $ASSET_DIR/$OUT (LPUART2 enabled -> guest /dev/ttyLP1)"

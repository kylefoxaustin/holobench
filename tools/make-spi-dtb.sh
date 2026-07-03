#!/bin/bash
# Generate the i.MX91 SPI-link DTB: the base EVK dtb with LPSPI1 (spi@44360000)
# enabled + a spidev child, so a board-to-board SPI link exposes /dev/spidev0.0
# in the guest. Mirrors the runtime patch in 91emulator's
# tests/interconnect-imx91/run-spi.sh (the proven, byte-exact SPI link):
#   - flip spi@44360000 status disabled -> okay
#   - drop its dmas/dma-names (the imx9x_lpspi model is PIO / ssi_transfer, no eDMA)
#   - add a spidev@0 child (rohm,dh2228fv is in the kernel spidev allow-list)
# Standard dtc decompile -> patch -> recompile; no machine-model change (the
# spi-link SSI bridge peripheral is provided by the board model, added by -device).
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
ASSET_DIR="${1:-$REPO/assets/imx91-evk-sd}"
BASE="${2:-imx91-11x11-evk.dtb}"
OUT="${3:-imx91-11x11-evk-spilink.dtb}"
DTC="${DTC:-/home/kyle/Documents/nxp/linux/imx-yocto-bsp/build-imx91/tmp/work-shared/imx91evk/kernel-build-artifacts/scripts/dtc/dtc}"

[ -x "$DTC" ] || { echo "error: dtc not found at $DTC (set DTC=)" >&2; exit 1; }
base_path="$(readlink -f "$ASSET_DIR/$BASE")"
[ -f "$base_path" ] || { echo "error: base dtb not found: $ASSET_DIR/$BASE" >&2; exit 1; }

work="$(mktemp -d)"; trap 'rm -rf "$work"' EXIT
"$DTC" -I dtb -O dts "$base_path" 2>/dev/null > "$work/base.dts"
awk '
  /spi@44360000 \{/ { inn = 1 }
  inn && /dmas =|dma-names =/ { next }
  inn && /status = "disabled"/ { print "\t\t\t\tstatus = \"okay\";"; next }
  inn && /^\t\t\t\};/ {
    print "\t\t\t\tspidev@0 {";
    print "\t\t\t\t\tcompatible = \"rohm,dh2228fv\";";
    print "\t\t\t\t\treg = <0x00>;";
    print "\t\t\t\t\tspi-max-frequency = <0xf4240>;";
    print "\t\t\t\t};";
    print; inn = 0; next
  }
  { print }
' "$work/base.dts" > "$work/spi.dts"
grep -q 'spidev@0' "$work/spi.dts" || { echo "error: spidev child not inserted (dtb layout changed?)" >&2; exit 1; }
"$DTC" -I dts -O dtb "$work/spi.dts" 2>/dev/null > "$ASSET_DIR/$OUT"
echo "wrote $ASSET_DIR/$OUT (lpspi1 enabled + spidev@0 -> guest /dev/spidev0.0)"

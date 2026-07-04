#!/bin/bash
# Generate the i.MX95 SPI-link DTB. Unlike i.MX91/93 (LPSPI disabled, no child),
# the i.MX95 EVK ships LPSPI7 (spi@42710000) ENABLED with a `lwn,bk4` child and
# eDMA. So the only patch is: swap that child's compatible to a spidev-allow-list
# string ("rohm,dh2228fv") so /dev/spidev0.0 appears — status stays okay, dmas are
# KEPT (95 SPI runs over eDMA, per 95emulator's tests/interconnect-imx95/run-spi.sh).
# Standard dtc decompile -> one-property swap -> recompile; no model change.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
ASSET_DIR="${1:-$REPO/assets/imx95-evk-sd}"
BASE="${2:-imx95-19x19-evk.dtb}"
OUT="${3:-imx95-19x19-evk-spilink.dtb}"
SPI_ADDR="${SPI_ADDR:-42710000}"        # LPSPI7
DTC="${DTC:-/home/kyle/Documents/nxp/linux/imx-yocto-bsp/build-imx95-drone-sizer/tmp/work-shared/imx95-19x19-lpddr5-evk/kernel-build-artifacts/scripts/dtc/dtc}"

[ -x "$DTC" ] || { echo "error: dtc not found at $DTC (set DTC=)" >&2; exit 1; }
base_path="$(readlink -f "$ASSET_DIR/$BASE")"
[ -f "$base_path" ] || { echo "error: base dtb not found: $ASSET_DIR/$BASE" >&2; exit 1; }

work="$(mktemp -d)"; trap 'rm -rf "$work"' EXIT
"$DTC" -I dtb -O dts "$base_path" 2>/dev/null > "$work/base.dts"
# Inside spi@<addr>, swap the child device's compatible to the spidev allow-list id.
awk -v node="spi@$SPI_ADDR {" '
  index($0, node) { inn = 1 }
  inn && /compatible = "lwn,bk4"/ { sub(/"lwn,bk4"/, "\"rohm,dh2228fv\""); done = 1 }
  inn && /^\t\t\t\};/ { inn = 0 }
  { print }
  END { if (!done) { print "error: lwn,bk4 child not found under spi@" node > "/dev/stderr"; exit 1 } }
' "$work/base.dts" > "$work/spi.dts"
"$DTC" -I dts -O dtb "$work/spi.dts" 2>/dev/null > "$ASSET_DIR/$OUT"
echo "wrote $ASSET_DIR/$OUT (LPSPI7 bk4 child -> spidev -> guest /dev/spidev0.0)"

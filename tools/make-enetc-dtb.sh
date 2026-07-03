#!/bin/bash
# Generate the i.MX95 real-ENETC DTB: the base EVK dtb with the 3 NETC/ENETC ports
# rewritten to fixed-link (no external PHY/MDIO/AQR, which aren't modeled) + an
# identity NETC msi-map, so the stock enetc4_pf driver binds eth0/eth1/eth2 and
# they carry traffic. Wraps 95emulator's tests/netc/patch-dtb.py (the source of
# truth for the per-port fixed-link + msi-map edits). With this dtb + a
# `-nic user,model=fsl-enetc` backend, the guest gets a real ENETC eth0 (DHCP/SSH)
# instead of the virtio-net convenience NIC. Standard dtc decompile -> patch ->
# recompile; no machine-model change (ENETC is already COMPUTES-tier on imx95).
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
ASSET_DIR="${1:-$REPO/assets/imx95-evk-sd}"
BASE="${2:-imx95-19x19-evk.dtb}"
OUT="${3:-imx95-19x19-evk-enetc.dtb}"
PATCH="${PATCH:-$HOME/Documents/GitHub/95emulator/tests/netc/patch-dtb.py}"
DTC="${DTC:-$HOME/Documents/linux-imx95-build/scripts/dtc/dtc}"

[ -x "$DTC" ] || { echo "error: dtc not found at $DTC (set DTC=)" >&2; exit 1; }
[ -f "$PATCH" ] || { echo "error: patch-dtb.py not found at $PATCH (set PATCH=)" >&2; exit 1; }
base_path="$(readlink -f "$ASSET_DIR/$BASE")"
[ -f "$base_path" ] || { echo "error: base dtb not found: $ASSET_DIR/$BASE" >&2; exit 1; }

work="$(mktemp -d)"; trap 'rm -rf "$work"' EXIT
"$DTC" -I dtb -O dts "$base_path" 2>/dev/null > "$work/base.dts"
python3 "$PATCH" "$work/base.dts" > "$work/netc.dts"
"$DTC" -I dts -O dtb "$work/netc.dts" 2>/dev/null > "$ASSET_DIR/$OUT"
echo "wrote $ASSET_DIR/$OUT (ENETC fixed-link + identity msi-map -> guest eth0/eth1/eth2)"

#!/bin/bash
# Generate a FlexCAN-link DTB: the base EVK dtb with both FlexCAN nodes enabled and
# their transceiver-supply repointed at a dummy always-on regulator, so a
# board-to-board CAN link exposes can0 in the guest. Mirrors the emulator sessions'
# flexcan-overlay.dtso (93emulator tests/flexcan/, same shape on 95): on the real
# board xceiver-supply is a GPIO "can-stby" regulator behind an i2c expander that
# doesn't probe in emulation, so the stock FlexCAN nodes would defer forever
# ("regulator-canN-stby not ready"). No transceiver is modelled, so a fixed always-on
# dummy is correct. Standard dtc decompile -> patch -> recompile; no model change
# (the can-host-chardev bridge is a stock -object, added at launch).
#
# Board-specific via env: FLEXCAN1_ADDR / FLEXCAN2_ADDR (the `can@<addr>` unit
# addresses). Defaults are the i.MX93 pair; pass the i.MX95 pair for that board.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
ASSET_DIR="${1:-$REPO/assets/imx93-evk-sd}"
BASE="${2:-imx93-11x11-evk.dtb}"
OUT="${3:-imx93-11x11-evk-canlink.dtb}"
FC1="${FLEXCAN1_ADDR:-443a0000}"
FC2="${FLEXCAN2_ADDR:-425b0000}"
DTC="${DTC:-/home/kyle/Documents/nxp/linux/imx-yocto-bsp/build-imx93/tmp/work-shared/imx93evk/kernel-build-artifacts/scripts/dtc/dtc}"

[ -x "$DTC" ] || { echo "error: dtc not found at $DTC (set DTC=)" >&2; exit 1; }
base_path="$(readlink -f "$ASSET_DIR/$BASE")"
[ -f "$base_path" ] || { echo "error: base dtb not found: $ASSET_DIR/$BASE" >&2; exit 1; }

work="$(mktemp -d)"; trap 'rm -rf "$work"' EXIT
"$DTC" -I dtb -O dts "$base_path" 2>/dev/null > "$work/base.dts"
grep -q "can@$FC1" "$work/base.dts" || { echo "error: can@$FC1 not in base dtb (wrong FLEXCAN1_ADDR?)" >&2; exit 1; }

# A free phandle = max existing + 1. NB: sort NUMERICALLY (strtonum) — a lexical
# sort puts 0xff after 0x100 and hands back an already-used phandle.
maxph="$(grep -oE 'phandle = <0x[0-9a-f]+>' "$work/base.dts" | grep -oE '0x[0-9a-f]+' \
          | awk '{printf "%d\n", strtonum($0)}' | sort -n | tail -1)"
newph="$(printf '0x%x' $(( maxph + 1 )))"

awk -v fc1="can@$FC1 {" -v fc2="can@$FC2 {" -v ph="$newph" '
  # Insert the dummy regulator as a root child — right BEFORE the first
  # root-level subnode (one-tab-indented `name {`), so it lands after the root
  # properties (DTS requires properties to precede subnodes).
  !reg && /^\t[A-Za-z0-9_@-]+[^{]*\{[ \t]*$/ {
    print "\tregulator-can-xceiver-dummy {"
    print "\t\tcompatible = \"regulator-fixed\";"
    print "\t\tregulator-name = \"can-xceiver-dummy\";"
    print "\t\tregulator-always-on;"
    print "\t\tphandle = <" ph ">;"
    print "\t};"
    reg = 1
    # fall through: print the subnode-open line we matched
  }
  # Enter a FlexCAN node; remember its indent so we can spot its close.
  index($0, fc1) && !in1 && !done1 { in1 = 1; match($0, /^[ \t]*/); ind1 = substr($0, 1, RLENGTH); sup1 = 0 }
  index($0, fc2) && !in2 && !done2 { in2 = 1; match($0, /^[ \t]*/); ind2 = substr($0, 1, RLENGTH); sup2 = 0 }
  # Inside a FlexCAN node: enable it, and repoint an EXISTING xceiver-supply
  # (imx95 flexcan1 ships one) or ADD one at the node close (imx93 flexcan1 has none).
  in1 {
    if ($0 ~ /status = "disabled"/) sub(/disabled/, "okay")
    if ($0 ~ /xceiver-supply = </) { sub(/<0x[0-9a-f]+>/, "<" ph ">"); sup1 = 1 }
    if ($0 == ind1 "};") { if (!sup1) print ind1 "\txceiver-supply = <" ph ">;"; in1 = 0; done1 = 1 }
  }
  in2 {
    if ($0 ~ /status = "disabled"/) sub(/disabled/, "okay")
    if ($0 ~ /xceiver-supply = </) { sub(/<0x[0-9a-f]+>/, "<" ph ">"); sup2 = 1 }
    if ($0 == ind2 "};") { if (!sup2) print ind2 "\txceiver-supply = <" ph ">;"; in2 = 0; done2 = 1 }
  }
  { print }
' "$work/base.dts" > "$work/can.dts"

grep -q 'can-xceiver-dummy' "$work/can.dts" || { echo "error: dummy regulator not inserted" >&2; exit 1; }
"$DTC" -I dts -O dtb "$work/can.dts" 2>/dev/null > "$ASSET_DIR/$OUT"
echo "wrote $ASSET_DIR/$OUT (flexcan1/2 enabled + dummy xceiver -> guest can0; phandle $newph)"

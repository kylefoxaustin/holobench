#!/usr/bin/env bash
# Build the i.MX 95 node's initramfs for the can-link-95-mcx lab.
#
# WHY THIS EXISTS AT ALL — the lab was ASPIRATIONAL and nobody had noticed.
#
# `can-link-95-mcx` booted the imx95-evk-sd profile: the full Yocto SD image, systemd, a
# login prompt, and NOTHING LISTENING ON can0. The MCX node transmits std ID 0x321 forever
# and waits for a 0x322 reply that no one was ever going to send. So the lab wired the bus
# correctly, booted both boards, and could never pass. Its console said `CAN LINK test` --
# the banner -- and never `CAN LINK PASS`.
#
#   ⭐ A LAB THAT CANNOT PASS IS NOT A FAILING LAB. IT IS A LAB NOBODY EVER RAN TO THE END.
#     It sat in labs/ looking exactly like the ones that work.
#
# The repo's own note blamed a missing kernel module ("rootfs lacks the .ko"). That was
# wrong twice over: the .ko IS in the golden rootfs, and even with it loaded there is still
# no responder. A plausible diagnosis nobody re-derived, sitting on top of a lab nobody ran.
#
# WHAT THE 95'S OWN PASSING TEST ACTUALLY DOES (tests/interconnect-imx95/xcheck-can-mcx.sh):
# it does NOT boot the SD image. It boots a busybox initramfs, insmods the four CAN modules,
# and runs `canlink respond can0`. This script reproduces that, the holobench way.
#
# WHY OUR OWN INIT (same reason as make-enet-lab3-initramfs.sh). Their init ends in
# `poweroff -f`. Correct for a self-contained test, WRONG for a board farm: a node that
# powers itself off is a session that vanishes under the coordinator, and it makes
# "this node left" and "this node crashed" THE SAME OBSERVATION. Departure is the
# coordinator's, over QMP, at a moment it chose. So our init HOLDS.
#
# THE KERNEL AND THE MODULES MUST BE ONE BUILD. We insmod .ko files straight out of the
# kernel build tree, so the profile MUST boot that same tree's Image -- vermagic is the
# version string, and a mismatch is refused. (This is the bug that hid the whole thing:
# imx95-evk-sd booted a LOCAL kernel 6.12.49-gdf24f9428e38 against a Yocto rootfs whose
# modules were built for 6.12.49-lts-next-gdf24f9428e38 -- same source, same git hash, one
# LOCALVERSION apart, and modprobe looks up `uname -r` and finds nothing.)
#
# The 95's canlink.c is compiled VERBATIM and READ-ONLY (CLAUDE.md §7). We never write to
# their repo.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
OUT_DIR="${1:-$REPO/assets/imx95-evk-can-lab}"
SRC="${SRC:-$HOME/Documents/GitHub/95emulator/tests/interconnect-imx95/canlink.c}"
BUSYBOX_CPIO="${BUSYBOX_CPIO:-$HOME/Documents/GitHub/95emulator/tests/busybox-initramfs/busybox-initramfs.cpio.gz}"
KBUILD="${KBUILD:-$HOME/Documents/linux-imx95-build}"
CC="${CC:-aarch64-linux-gnu-gcc}"

[ -f "$SRC" ]           || { echo "error: canlink.c not found at $SRC" >&2; exit 1; }
[ -f "$BUSYBOX_CPIO" ]  || { echo "error: busybox initramfs not found at $BUSYBOX_CPIO" >&2; exit 1; }
[ -d "$KBUILD" ]        || { echo "error: kernel build tree not found at $KBUILD" >&2; exit 1; }

KREL="$(cat "$KBUILD/include/config/kernel.release")"
echo "== kernel build: $KREL"

WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
root="$WORK/root"
mkdir -p "$root/bin" "$root/proc" "$root/sys" "$root/dev" "$root/mod"

# busybox (from the 95's own initramfs, so we inherit a known-good static build)
( cd "$WORK" && zcat "$BUSYBOX_CPIO" | cpio -idmu --quiet 'bin/busybox' )
cp "$WORK/bin/busybox" "$root/bin/busybox"

# the four CAN modules, from THIS kernel build (vermagic must match what we boot)
for ko in net/can/can.ko \
          drivers/net/can/dev/can-dev.ko \
          net/can/can-raw.ko \
          drivers/net/can/flexcan/flexcan.ko; do
    [ -f "$KBUILD/$ko" ] || { echo "error: module not built: $KBUILD/$ko" >&2; exit 1; }
    cp "$KBUILD/$ko" "$root/mod/$(basename "$ko")"
done
echo "== staged 4 CAN modules from $KBUILD"

# the 95's responder, compiled from THEIR source, read-only
"$CC" -O2 -static -o "$root/canlink" "$SRC"
echo "== built canlink responder ($(stat -c%s "$root/canlink") bytes, static)"

cat > "$root/init" <<'INIT'
#!/bin/busybox sh
/bin/busybox mount -t proc proc /proc
/bin/busybox mount -t sysfs sysfs /sys
/bin/busybox mount -t devtmpfs dev /dev 2>/dev/null
/bin/busybox --install -s /bin 2>/dev/null
exec >/dev/console 2>&1

echo "CAN-LAB: kernel $(uname -r)"
for m in can can-dev can-raw flexcan; do
    insmod /mod/$m.ko 2>&1 | grep -v '^$' || true
done

# WAIT for can0 rather than assume it. A module that loaded is not a device that probed.
n=0
while [ ! -e /sys/class/net/can0 ] && [ $n -lt 30 ]; do sleep 1; n=$((n+1)); done
if [ ! -e /sys/class/net/can0 ]; then
    echo "CAN-LAB: FAIL — no can0 after ${n}s (modules loaded but nothing probed)"
    # HOLD anyway. A node that powers itself off makes "it failed" and "it vanished"
    # the same observation, and the coordinator loses the console that says which.
    while true; do sleep 3600; done
fi

echo "CAN-LAB: can0 present, responding (peer sends 0x321, we reply 0x322)"
/canlink respond can0

# HOLD FOREVER. Never poweroff: departure is the COORDINATOR'S, over QMP, at a time it
# chose and recorded. A node that exits on its own writes a departure that never happened
# into every other node's data.
echo "CAN-LAB: responder returned — holding (departure is the coordinator's)"
while true; do sleep 3600; done
INIT
chmod +x "$root/init"

mkdir -p "$OUT_DIR"
( cd "$root" && find . | cpio -o -H newc --quiet | gzip -9 ) > "$OUT_DIR/can-lab.cpio.gz"
echo "== wrote $OUT_DIR/can-lab.cpio.gz ($(stat -c%s "$OUT_DIR/can-lab.cpio.gz") bytes)"
echo "== md5 $(md5sum "$OUT_DIR/can-lab.cpio.gz" | cut -d' ' -f1)   <- boot.pin this"

#!/bin/bash
# Build the i.MX 95 node's initramfs for the mcx-rt1180-95-l2 three-node L2 lab.
#
# The lab tool itself (enet-lab3.c) is the 95 session's, used VERBATIM and read-only
# — it broadcasts ethertype 0x88B7 and declares PASS only after OBSERVING both peers
# (0x88B5 mcx + 0x88B6 rt1180). Holobench never edits an emulator repo (CLAUDE.md §7);
# we compile its source and wrap it in an init WE own.
#
# WHY OUR OWN INIT. The 95's test init runs the tool and then `poweroff -f`. That is
# right for a self-contained test and WRONG for a board farm: holobench holds every
# board over QMP for the board's whole life, so a node that powers itself off is a
# session that vanishes under the coordinator — and, worse, "this node left" and
# "this node crashed" become the SAME observation. That is the collapsed oracle the
# fleet has spent the week hunting. So:
#
#   * the node HOLDS after the tool returns (idle forever, QEMU stays up, QMP alive);
#   * DEPARTURE is scheduled by the coordinator (a QMP quit at a known time), which
#     is the only way a departure is distinguishable from a death.
#
# Persistence is bought with the tool's OWN env knobs, not a patch: a long post-PASS
# broadcast window keeps it beaconing as a real peer (a node that stops transmitting
# the moment it is satisfied is a drive-by, not a peer — mcx/rt1180 both beacon
# forever, and mcx even re-arms).
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
OUT_DIR="${1:-$REPO/assets/imx95-evk-enet-lab3}"

# COMPILE THE COMMIT, NEVER THE WORKTREE.
#
# ⭐ A PATH IN A LIVE WORKTREE IS NOT AN ARTIFACT — and that is just as true of SOURCE as of a
# binary. This script's output is only as trustworthy as the .c it compiled, and 95emulator's
# worktree is a live tree that its own test runs rewrite. If the file is dirty we are building
# a node from code that is in no commit, that nobody announced, and that their next edit
# destroys — and the resulting lab would go green about a firmware nobody chose.
#
# rt1180 found the sharpest form of this in its own tree: "this firmware's source is a patch
# inside a shell script, applied to an SDK file OUTSIDE THE REPO. The ELF was committed; the
# code that produced it was committed NOWHERE."
#   ⭐ AN ARTIFACT WITH NO SOURCE IN THE TREE CANNOT GO STALE — IT WAS NEVER FRESH.
#
# And both halves are needed, which 91emulator established by retracting a credit for having
# shipped only one: a PIN WITHOUT A SOURCE GATE blesses a binary nobody compiled; a SOURCE
# GATE WITHOUT A PIN blesses a binary nobody announced.
SRC_REPO="${SRC_REPO:-$HOME/Documents/GitHub/95emulator}"
SRC_PATH="${SRC_PATH:-tests/enet-lab3/enet-lab3.c}"
SRC_REF="${SRC_REF:-HEAD}"

_SRC_TMP="$(mktemp -d)"; trap 'rm -rf "$_SRC_TMP"' EXIT
git -C "$SRC_REPO" show "$SRC_REF:$SRC_PATH" > "$_SRC_TMP/enet-lab3.c" 2>/dev/null || {
    echo "error: cannot read $SRC_PATH at $SRC_REF in $SRC_REPO" >&2; exit 1; }
SRC="${SRC:-$_SRC_TMP/enet-lab3.c}"
echo "== source: $SRC_REPO $SRC_PATH @ $(git -C "$SRC_REPO" rev-parse --short "$SRC_REF")"
echo "== source md5: $(md5sum "$SRC" | cut -d' ' -f1)   <- the OTHER half of the pin"
if ! git -C "$SRC_REPO" diff --quiet -- "$SRC_PATH" 2>/dev/null; then
    echo "== WARNING: $SRC_PATH is DIRTY in their worktree — building from the COMMIT anyway," >&2
    echo "==          which is the point. Their uncommitted edits are not a firmware." >&2
fi
BUSYBOX_CPIO="${BUSYBOX_CPIO:-$HOME/Documents/GitHub/95emulator/tests/busybox-initramfs/busybox-initramfs.cpio.gz}"
CC="${CC:-aarch64-linux-gnu-gcc}"

[ -f "$SRC" ]          || { echo "error: enet-lab3.c not found at $SRC" >&2; exit 1; }
[ -f "$BUSYBOX_CPIO" ] || { echo "error: busybox initramfs not found at $BUSYBOX_CPIO" >&2; exit 1; }
command -v "$CC" >/dev/null || { echo "error: no $CC" >&2; exit 1; }

WORK="$(mktemp -d)"; trap 'rm -rf "$WORK" "$_SRC_TMP"' EXIT   # BOTH: a second trap REPLACES the first
mkdir -p "$OUT_DIR"

"$CC" -static -O2 -Wall "$SRC" -o "$WORK/enet-lab3"

root="$WORK/root"; mkdir -p "$root"/{bin,proc,sys,dev}
( cd "$WORK" && zcat "$BUSYBOX_CPIO" | cpio -idmu --quiet 'bin/busybox' )
cp "$WORK/bin/busybox" "$root/bin/busybox"
cp "$WORK/enet-lab3"   "$root/enet-lab3"

cat > "$root/init" <<'INIT'
#!/bin/busybox sh
/bin/busybox mount -t proc proc /proc
/bin/busybox mount -t sysfs sysfs /sys
/bin/busybox --install -s /bin 2>/dev/null

ET=$(sed -n 's/.*lab_et=\([^ ]*\).*/\1/p' /proc/cmdline)
PEERS=$(sed -n 's/.*lab_peers=\([^ ]*\).*/\1/p' /proc/cmdline | tr ',' ' ')
DL=$(sed -n 's/.*lab_deadline=\([^ ]*\).*/\1/p' /proc/cmdline)
PP=$(sed -n 's/.*lab_postpass=\([^ ]*\).*/\1/p' /proc/cmdline)
[ -n "$DL" ] && export LAB_DEADLINE_MS="$DL"
[ -n "$PP" ] && export LAB_POST_PASS_MS="$PP"

# ENETC PF0 (devfn 00.0) carries the -nic backend. Resolve the netdev by PCI
# address, never by name: probe order makes "eth0" unreliable (the 95's own
# run-eth.sh learned this the hard way).
n=0
while [ $n -lt 60 ]; do
    IF=$(ls /sys/bus/pci/devices/0002:00:00.0/net 2>/dev/null | head -1)
    [ -n "$IF" ] && break
    sleep 1; n=$((n+1))
done
if [ -z "$IF" ]; then
    echo "ENET-LAB3 FAIL (no netdev on 0002:00:00.0)"
else
    ip link set "$IF" up
    sleep 1
    echo "ENET-LAB3 boot: if=$IF et=$ET peers=[$PEERS] carrier=$(cat /sys/class/net/$IF/carrier 2>/dev/null)"
    /enet-lab3 "$IF" "$ET" $PEERS
    echo "ENET-LAB3 rc=$?"
fi

# HOLD. Do NOT poweroff.
#
# holobench is a persistent board FARM: it holds this board over QMP for the whole
# lab. If the node powered itself off here, (a) the coordinator's session would
# vanish underneath it, and (b) "left early" and "crashed" would be the same
# observation. Departure is the COORDINATOR's to schedule, over QMP, at a known
# time — that is what makes an early departure a fact rather than an inference.
echo "ENET-LAB3 hold: node stays up; departure is the coordinator's to schedule."
while : ; do sleep 3600; done
INIT
chmod +x "$root/init"

( cd "$root" && find . | cpio -o -H newc --quiet ) | gzip -9 > "$OUT_DIR/lab3.cpio.gz"
echo "wrote $OUT_DIR/lab3.cpio.gz ($(stat -c%s "$OUT_DIR/lab3.cpio.gz") bytes)"

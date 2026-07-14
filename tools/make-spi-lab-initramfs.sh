#!/usr/bin/env bash
# Build the i.MX 95 node's initramfs for the spi-link-95-mcx lab.
#
# SAME DEFECT AS THE CAN LAB, FOUND THE SAME WAY. spi-link-95-mcx booted `imx95-evk-sd` --
# the Yocto SD image: systemd, a login prompt, and NOBODY DRIVING /dev/spidev0.0. The MCX
# node asserts "SPI LINK PASS"/"SPI LINK FAIL" against a pattern it must RECEIVE, so with
# nothing sending, it could only ever print its banner. The lab could not pass.
#
#   ⭐ A LAB THAT CANNOT PASS IS NOT A FAILING LAB. IT IS ONE NOBODY RAN TO THE END.
#
# WHAT THE MCX ACTUALLY WANTS (mcxn947qemu tests/mcxn-spi-link/main.c, read, not assumed):
# it is the LPSPI *master*. It clocks 0x5A out forever and reads the peer's bytes back in,
# hunting a frame: a 0xA5 MARKER, then N=32 payload bytes where expect(i) = (i*3+5) & 0x7F.
# 0xFF means "spi-link FIFO empty" and is skipped. So the 95 must put exactly
#
#     A5 05 08 0B 0E 11 14 17 1A 1D 20 23 26 29 2C 2F 32 35 38 3B 3E 41 44 47 4A 4D 50 53
#     56 59 5C 5F 62                                                       (33 bytes)
#
# on the wire, contiguously. NOT spilink's default text payload -- that would clock a
# perfectly valid stream of the WRONG BYTES and the MCX would resync forever, printing
# nothing. (This is the whole reason the payload is computed here rather than defaulted:
# two programs that both "work" and never agree on the bytes is the interop bug this fleet
# has spent all night on, one wire down.)
#
# THE PAYLOAD IS CHECKED FOR THE THINGS THAT WOULD SILENTLY BREAK IT, not just generated:
#   * no NUL      -- spilink takes the payload as a C string; a NUL truncates it mid-frame
#   * no 0xFF     -- the MCX reads 0xFF as IDLE and would skip it, corrupting the sequence
#   * no 2nd 0xA5 -- a second marker inside the body would RESYNC the MCX mid-frame
# and the init re-verifies the byte count at run time, because the payload has to survive a
# shell (it contains 0x20 SPACE and 0x5C BACKSLASH) and a payload that is silently mangled
# would make the lab fail for a reason that is not the lab's.
#
# NO KERNEL MODULES NEEDED: CONFIG_SPI_FSL_LPSPI=y and CONFIG_SPI_SPIDEV=y on this kernel,
# and the spilink dtb flips spi@42710000/spi@0 from `lwn,bk4` to `rohm,dh2228fv` -- the
# compatible spidev binds -- so /dev/spidev0.0 appears on its own. (Verified by PARSING the
# dtb. The CAN task taught me twice in one hour that a tool that isn't installed and a file
# that is wrong produce the same empty output.)
#
# The 95's spilink.c is compiled VERBATIM and READ-ONLY (CLAUDE.md §7).
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
OUT_DIR="${1:-$REPO/assets/imx95-evk-spi-lab}"
SRC="${SRC:-$HOME/Documents/GitHub/95emulator/tests/interconnect-imx95/spilink.c}"
BUSYBOX_CPIO="${BUSYBOX_CPIO:-$HOME/Documents/GitHub/95emulator/tests/busybox-initramfs/busybox-initramfs.cpio.gz}"
CC="${CC:-aarch64-linux-gnu-gcc}"

[ -f "$SRC" ]          || { echo "error: spilink.c not found at $SRC" >&2; exit 1; }
[ -f "$BUSYBOX_CPIO" ] || { echo "error: busybox initramfs not found at $BUSYBOX_CPIO" >&2; exit 1; }

WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
root="$WORK/root"
mkdir -p "$root/bin" "$root/proc" "$root/sys" "$root/dev"

# The MCX's frame, derived from ITS source — not copied from a comment — and shipped as 33
# RAW BYTES rather than as an escape sequence.
#
# The first cut of this script built the payload as backslash-octal and injected it with
# `sed`. sed read `\245\005...` as BACKREFERENCES and died. It would have been worse if it
# had NOT died: the payload contains 0x20 (space) and 0x5C (backslash), so an escape
# sequence has to survive a heredoc, sed's RHS, a shell variable, word-splitting and
# printf — five chances to mangle it silently, and a mangled frame does not look like a
# mangled frame. It looks like an MCX that never PASSes, i.e. like a broken SPI model.
#
#   ⭐ DO NOT ENCODE A PAYLOAD YOU CAN SHIP. Every layer of escaping is a layer that can
#     eat a byte and blame the hardware.
python3 - "$root/mcx-frame.bin" <<'PY'
import sys
pay = bytes([0xA5] + [((i * 3 + 5) & 0x7F) for i in range(32)])
assert 0x00 not in pay, "NUL would truncate the C-string payload"
assert 0xFF not in pay, "0xFF is the MCX's IDLE byte and would be skipped"
assert 0xA5 not in pay[1:], "a second MARKER would resync the MCX mid-frame"
open(sys.argv[1], "wb").write(pay)
PY
echo "== MCX frame: MARKER + 32 x ((i*3+5) & 0x7F) = $(stat -c%s "$root/mcx-frame.bin") bytes"

( cd "$WORK" && zcat "$BUSYBOX_CPIO" | cpio -idmu --quiet 'bin/busybox' )
cp "$WORK/bin/busybox" "$root/bin/busybox"

"$CC" -O2 -static -o "$root/spilink" "$SRC"
echo "== built spilink ($(stat -c%s "$root/spilink") bytes, static)"

cat > "$root/init" <<'INIT'
#!/bin/busybox sh
/bin/busybox mount -t proc proc /proc
/bin/busybox mount -t sysfs sysfs /sys
/bin/busybox mount -t devtmpfs dev /dev 2>/dev/null
/bin/busybox --install -s /bin 2>/dev/null
exec >/dev/console 2>&1

echo "SPI-LAB: kernel $(uname -r)"

# The MCX's expected frame, shipped as raw bytes — no escaping anywhere. It still has to
# pass through a shell variable (it contains 0x20 SPACE and 0x5C BACKSLASH), so COUNT IT
# before trusting it: a silently mangled payload does not look like a mangled payload, it
# looks like an MCX that never PASSes — i.e. like a broken SPI model.
PAY=$(cat /mcx-frame.bin)
LEN=$(printf %s "$PAY" | wc -c)
if [ "$LEN" != "33" ]; then
    echo "SPI-LAB: FAIL — payload is $LEN bytes, expected 33 (the shell mangled it)"
    while true; do sleep 3600; done
fi
echo "SPI-LAB: payload OK, 33 bytes (0xA5 marker + 32 x (i*3+5)&0x7F)"

# WAIT for spidev rather than assume it. A driver that is built in is not a device that
# probed, and the spilink dtb has to have flipped spi@0 to rohm,dh2228fv for it to bind.
n=0
while [ ! -e /dev/spidev0.0 ] && [ $n -lt 30 ]; do sleep 1; n=$((n+1)); done
if [ ! -e /dev/spidev0.0 ]; then
    echo "SPI-LAB: FAIL — no /dev/spidev0.0 after ${n}s (spidev never bound)"
    while true; do sleep 3600; done
fi
echo "SPI-LAB: /dev/spidev0.0 present, sending the MCX's frame"

# RESEND, PACED. The MCX may connect after us, and spilink's `send` role does exactly one
# transfer. Pacing is not politeness: the spi-link RX FIFO is 256 deep, and flooding it
# blocks the peer's chardev write -> its LPSPI TDR write -> a HUNG spidev ioctl on our side
# (the fleet's imx93<->MCX SPI lesson). One frame a second is plenty — the MCX resyncs on
# the marker, so a partial early burst costs nothing.
i=0
while true; do
    /spilink send /dev/spidev0.0 "$PAY" >/dev/null 2>&1
    i=$((i+1))
    [ "$i" = 5 ] && echo "SPI-LAB: sent 5 frames"
    sleep 1
done
INIT
chmod +x "$root/init"

mkdir -p "$OUT_DIR"
( cd "$root" && find . | cpio -o -H newc --quiet | gzip -9 ) > "$OUT_DIR/spi-lab.cpio.gz"
echo "== wrote $OUT_DIR/spi-lab.cpio.gz ($(stat -c%s "$OUT_DIR/spi-lab.cpio.gz") bytes)"
echo "== md5 $(md5sum "$OUT_DIR/spi-lab.cpio.gz" | cut -d' ' -f1)   <- boot.pin this"

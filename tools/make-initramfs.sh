#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# Holobench asset-prep: build an initramfs cpio.gz from a BSP rootfs tarball,
# injecting a Holobench /init. Host-side asset prep only — reads the rootfs
# read-only, writes nothing into the source tree. Mirrors the proven recipe the
# emulator soak harnesses use (zstd|tar -x -> inject init -> cpio -H newc | gzip).
#
# Usage:
#   make-initramfs.sh <rootfs.tar.zst|.tar.gz|.tar> <init-file> <out.cpio.gz>
set -euo pipefail

ROOTFS_TAR=${1:?rootfs tarball}
INIT_FILE=${2:?init file}
OUT=${3:?output cpio.gz path}

command -v fakeroot >/dev/null || { echo "error: fakeroot not found" >&2; exit 1; }
[ -f "$ROOTFS_TAR" ] || { echo "error: rootfs not found: $ROOTFS_TAR" >&2; exit 1; }
[ -f "$INIT_FILE" ]  || { echo "error: init not found: $INIT_FILE" >&2; exit 1; }

ROOTFS_TAR=$(readlink -f "$ROOTFS_TAR")
INIT_FILE=$(readlink -f "$INIT_FILE")
mkdir -p "$(dirname "$OUT")"
OUT=$(readlink -f "$OUT")

# Pick decompressor + archive type. Supports rootfs TARBALLS (.tar[.zst|.gz])
# and prebuilt initramfs CPIO archives (.cpio[.gz]).
case "$ROOTFS_TAR" in
  *.cpio.gz) DECOMP="gzip -dc"; KIND="cpio" ;;
  *.cpio)    DECOMP="cat";      KIND="cpio" ;;
  *.tar.zst|*.zst) DECOMP="zstd -dc"; KIND="tar" ;;
  *.tar.gz|*.tgz)  DECOMP="gzip -dc"; KIND="tar" ;;
  *.tar)     DECOMP="cat";      KIND="tar" ;;
  *) echo "error: unknown archive type: $ROOTFS_TAR" >&2; exit 1 ;;
esac

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

if [ "$KIND" = "cpio" ]; then
  EXTRACT="$DECOMP '$ROOTFS_TAR' | cpio -idmu 2>/dev/null"
else
  EXTRACT="$DECOMP '$ROOTFS_TAR' | tar -x 2>/dev/null"
fi

echo "make-initramfs: extracting $(basename "$ROOTFS_TAR") ($KIND) ..."
fakeroot bash -c "
  set -e
  cd '$WORK' && mkdir rootfs && cd rootfs
  $EXTRACT
  cp '$INIT_FILE' init && chmod 755 init
  [ -e dev/console ] || mknod -m 600 dev/console c 5 1
  find . | cpio -o -H newc 2>/dev/null | gzip -1 > '$OUT'
"
echo "make-initramfs: wrote $OUT ($(du -h "$OUT" | cut -f1))"

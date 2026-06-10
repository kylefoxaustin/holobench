#!/bin/bash
# Make a small "golden" data disk for the image-swap / reinstall demo: a raw
# ext4 image with a marker file. Holobench attaches a per-session qcow2 OVERLAY
# over this (writes isolated); "reinstall" drops the overlay -> golden restored.
#
# Usage: make-golden-disk.sh <out.img> [size_mb]
set -euo pipefail
OUT="${1:?output image path}"
SIZE_MB="${2:-32}"

command -v mkfs.ext4 >/dev/null || { echo "error: mkfs.ext4 not found"; exit 1; }
mkdir -p "$(dirname "$OUT")"
dd if=/dev/zero of="$OUT" bs=1M count="$SIZE_MB" status=none
mkfs.ext4 -q -F -L holobench "$OUT"

# Seed a marker so the guest can see "golden" content (and prove reinstall
# restores it). Needs a loop mount (root); fall back to debugfs if unprivileged.
MARKER="GOLDEN_IMAGE_v1 — restored by Holobench reinstall"
if mountpoint -q /mnt 2>/dev/null; then :; fi
TMP="$(mktemp -d)"
if mount -o loop "$OUT" "$TMP" 2>/dev/null; then
  echo "$MARKER" > "$TMP/MARKER.txt"
  umount "$TMP"
  echo "seeded marker via loop mount"
elif command -v debugfs >/dev/null; then
  printf '%s\n' "$MARKER" > "$TMP/MARKER.txt"
  debugfs -w -R "write $TMP/MARKER.txt MARKER.txt" "$OUT" >/dev/null 2>&1 \
    && echo "seeded marker via debugfs" || echo "note: could not seed marker (need root); disk is blank ext4"
else
  echo "note: could not seed marker (need root or debugfs); disk is blank ext4"
fi
rmdir "$TMP" 2>/dev/null || rm -rf "$TMP"
echo "wrote golden disk: $OUT ($(du -h "$OUT" | cut -f1))"

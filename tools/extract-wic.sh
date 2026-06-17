#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-or-later
#
# Extract the kernel Image + a board dtb from an NXP prebuilt .wic SD image, for
# the build-me "prebuilt + auto-extract" path (the fast 'no BSP' route). NXP's
# prebuilt download is a single .wic[.zst]: the rootfs SD image whose FAT boot
# partition (p1) holds Image + the board dtbs (sometimes instead under /boot on the
# ext4 rootfs p2). This pulls them out so the wizard has Image + dtb + disk.wic.
# (Recipe + .wic layout confirmed by the i.MX95 emulator session.) Nothing NXP is
# hosted by Holobench — the operator supplies the .wic; this just unpacks it.
#
# Generic across NXP Linux generations: searches every partition (FAT boot AND
# ext /boot) for the requested kernel + dtb by name; handles .zst/.gz/.xz/plain.
#
# Usage:
#   tools/extract-wic.sh <image.wic[.zst|.gz|.xz]> <dest-dir> [--kernel Image] [--dtb <name>.dtb]
#   e.g. tools/extract-wic.sh ~/Downloads/imx-image-full-imx95evk.wic.zst \
#          /my/bsp/imx95-evk-sd --dtb imx95-19x19-evk.dtb
#
# Extraction needs ONE of: guestfish (no root, preferred) | root/sudo (loop-mount).
set -euo pipefail

SRC="${1:?usage: extract-wic.sh <image.wic[.zst]> <dest> [--kernel NAME] [--dtb NAME]}"
DEST="${2:?need a dest dir}"; shift 2 || true
KERNEL="Image"; DTB=""
while [ $# -gt 0 ]; do
  case "$1" in
    --kernel) KERNEL="${2:?}"; shift 2 ;;
    --dtb)    DTB="${2:?}"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
[ -f "$SRC" ] || { echo "error: no such image: $SRC" >&2; exit 1; }
mkdir -p "$DEST"
WIC="$DEST/disk.wic"

# 1) Decompress (or link) the .wic into place — it doubles as the rootfs disk.wic.
echo "==> preparing raw .wic at $WIC" >&2
case "$SRC" in
  *.zst) command -v zstd >/dev/null || { echo "error: need zstd for .zst" >&2; exit 1; }; zstd -d -f "$SRC" -o "$WIC" ;;
  *.gz)  gzip  -dc "$SRC" > "$WIC" ;;
  *.xz)  xz    -dc "$SRC" > "$WIC" ;;
  *.wic|*.img|*.raw) ln -sfn "$(readlink -f "$SRC")" "$WIC" ;;
  *) echo "error: unknown image type: $SRC (expect .wic[.zst/.gz/.xz])" >&2; exit 1 ;;
esac
WICR="$(readlink -f "$WIC")"

GOT_K=0; GOT_D=0
want_dtb() { [ -n "$DTB" ]; }

# 2a) guestfish fast-path (no root, handles any fs/layout).
if command -v guestfish >/dev/null 2>&1; then
  echo "==> trying guestfish (no-root) ..." >&2
  paths="$(guestfish --ro -a "$WICR" run : list-filesystems 2>/dev/null | cut -d: -f1 || true)"
  for fs in $paths; do
    found="$(guestfish --ro -a "$WICR" run : mount-ro "$fs" / : find / 2>/dev/null || true)"
    if [ "$GOT_K" = 0 ] && k="$(printf '%s\n' "$found" | grep -E "/${KERNEL}\$" | head -1)"; [ -n "$k" ]; then
      guestfish --ro -a "$WICR" run : mount-ro "$fs" / : download "$k" "$DEST/$KERNEL" && GOT_K=1
    fi
    if want_dtb && [ "$GOT_D" = 0 ] && d="$(printf '%s\n' "$found" | grep -E "/${DTB}\$" | head -1)"; [ -n "$d" ]; then
      guestfish --ro -a "$WICR" run : mount-ro "$fs" / : download "$d" "$DEST/$DTB" && GOT_D=1
    fi
  done
fi

# 2b) loop-mount fallback (needs root/sudo). Walk every partition; check / and /boot
#     (+ /boot/dtbs/*/freescale and /freescale for the dtb).
if [ "$GOT_K" = 0 ] || { want_dtb && [ "$GOT_D" = 0 ]; }; then
  command -v sfdisk >/dev/null || { echo "error: need sfdisk (or guestfish)" >&2; exit 1; }
  MOUNT="mount"; UMOUNT="umount"
  if [ "$(id -u)" != 0 ]; then
    command -v sudo >/dev/null || { echo "error: loop-mount needs root (install guestfish to avoid sudo)" >&2; exit 1; }
    MOUNT="sudo mount"; UMOUNT="sudo umount"
    echo "==> loop-mount fallback needs sudo (no guestfish present) ..." >&2
  fi
  offsets="$(sfdisk -J "$WICR" | python3 -c 'import json,sys
for p in json.load(sys.stdin)["partitiontable"]["partitions"]:
    print(p["start"]*512)')"
  for off in $offsets; do
    { [ "$GOT_K" = 1 ] && { ! want_dtb || [ "$GOT_D" = 1 ]; }; } && break
    mnt="$(mktemp -d)"
    if $MOUNT -o ro,loop,offset="$off" "$WICR" "$mnt" 2>/dev/null; then
      for dir in "$mnt" "$mnt/boot"; do
        [ "$GOT_K" = 0 ] && [ -f "$dir/$KERNEL" ] && { cp -f "$dir/$KERNEL" "$DEST/$KERNEL"; GOT_K=1; }
        if want_dtb && [ "$GOT_D" = 0 ]; then
          for cand in "$dir/$DTB" "$dir/freescale/$DTB" "$dir"/dtbs/*/freescale/"$DTB" "$dir"/*/freescale/"$DTB"; do
            [ -f "$cand" ] && { cp -f "$cand" "$DEST/$DTB"; GOT_D=1; break; }
          done
          # Glob fallback (95's suggestion): a release may ship only a versioned dtb
          # (imx95-19x19-evk-<ver>.dtb) without the plain symlink.
          if [ "$GOT_D" = 0 ]; then
            for cand in "$dir/${DTB%.dtb}"*.dtb "$dir/freescale/${DTB%.dtb}"*.dtb "$dir"/*/freescale/"${DTB%.dtb}"*.dtb; do
              [ -f "$cand" ] && { cp -f "$cand" "$DEST/$DTB"; GOT_D=1; break; }
            done
          fi
        fi
      done
      $UMOUNT "$mnt" 2>/dev/null || true
    fi
    rmdir "$mnt" 2>/dev/null || true
  done
fi

# 3) Verify.
[ "$GOT_K" = 1 ] || { echo "ERROR: kernel '$KERNEL' not found in the image (need guestfish or root)" >&2; exit 1; }
if want_dtb && [ "$GOT_D" != 1 ]; then echo "ERROR: dtb '$DTB' not found in the image" >&2; exit 1; fi
echo "extracted -> $DEST/$KERNEL$(want_dtb && echo " , $DEST/$DTB") (+ disk.wic)"
ls -la "$DEST"

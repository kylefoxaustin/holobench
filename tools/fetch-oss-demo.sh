#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-or-later
#
# Fetch a board's fully-OSS demo boot bundle (mainline/upstream kernel + dtb +
# buildroot/busybox rootfs — no NXP BSP) into its asset dir, so a user with no NXP
# artifacts can still boot. The bundle url/sha256 live in tools/build-sources.yaml
# (oss_demo:); they're empty until the emulator session publishes a boot recipe
# (Prime Directive §7 — we never guess what boots their model). See docs/SETUP.md.
#
# Usage:
#   tools/fetch-oss-demo.sh <board> [dest_dir]
#     dest_dir default: $HOLOBENCH_ASSET_ROOT/<board>  or  assets/<board>
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"; cd "$REPO"
BOARD="${1:?usage: fetch-oss-demo.sh <board> [dest_dir]}"
DEST="${2:-${HOLOBENCH_ASSET_ROOT:-$REPO/assets}/$BOARD}"

read -r URL SHA < <(python3 - "tools/build-sources.yaml" "$BOARD" <<'PY'
import sys, yaml
e = (yaml.safe_load(open(sys.argv[1])) or {}).get(sys.argv[2], {}) or {}
d = e.get("oss_demo") or {}
print(d.get("url", "") or "-", d.get("sha256", "") or "-")
PY
)

if [ "$URL" = "-" ] || [ -z "$URL" ]; then
  echo "OSS demo for '$BOARD' is not available yet."
  echo "  The bootable OSS bundle (mainline kernel + dtb + buildroot rootfs) is owned"
  echo "  by the $BOARD emulator session and not published yet. Use your own NXP BSP"
  echo "  for now (see docs/DEPLOY.md), or check back once the recipe lands."
  exit 3
fi

mkdir -p "$DEST"
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
echo "fetching OSS demo for $BOARD: $URL"
curl -fSL "$URL" -o "$tmp/bundle.tar.gz"
if [ "$SHA" != "-" ] && [ -n "$SHA" ]; then
  echo "$SHA  $tmp/bundle.tar.gz" | sha256sum -c - || { echo "error: sha256 mismatch"; exit 1; }
fi
tar -xzf "$tmp/bundle.tar.gz" -C "$DEST"
echo "extracted OSS demo -> $DEST"
ls -la "$DEST"

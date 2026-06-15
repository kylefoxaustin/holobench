#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-or-later
# Build the vendored ISI virtual-camera capture helpers as static aarch64 ELFs.
#
# These are standalone GPL-2.0-or-later command-line tools (sources in
# vendor/camera/*.c, authored by Kyle Fox, vendored from the emulator repos).
# Holobench stages the per-board binary into a session's 9p share so the guest
# runs it from /mnt — needed because imx-image-core ships no media-ctl/v4l2-ctl
# and the imx8-isi media links start disabled (a bare v4l2-ctl EPIPEs).
#
# Output: vendor/camera/bin/imx{91,93,95}-isi-capture
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO/vendor/camera"
CC="${CC:-aarch64-linux-gnu-gcc}"
command -v "$CC" >/dev/null || { echo "error: need $CC (apt install gcc-aarch64-linux-gnu)"; exit 1; }
mkdir -p bin
for b in 91 93 95; do
  "$CC" -O2 -static -o "bin/imx${b}-isi-capture" "imx${b}_v4l2_cap.c"
  echo "  built bin/imx${b}-isi-capture ($(stat -c%s "bin/imx${b}-isi-capture") bytes, static)"
done
echo "done."

#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-or-later
# Runs INSIDE the nxp-bsp builder container (docker run). Does the real NXP i.MX
# Yocto build + the SM firmware, then stages the 4 artifacts into /out (a mounted
# asset dir). INTERACTIVE: `source imx-setup-release.sh` prompts for the NXP EULA on
# the PTY — the operator accepts it here (we never auto-accept). Nothing NXP ships
# in Holobench; this builds it on the operator's machine from NXP's own sources.
#
# Params via env (set by tools/build-nxp-bsp.sh from build-sources.yaml nxp_bsp):
#   MACHINE        e.g. imx95-19x19-lpddr5-evk
#   DISTRO         e.g. fsl-imx-wayland
#   MANIFEST_BRANCH e.g. imx-linux-walnascar
#   MANIFEST_XML   e.g. imx-6.12.3-1.0.0.xml
#   IMAGE_TARGET   e.g. imx-image-full
#   DTB_NAME       e.g. imx95-19x19-evk.dtb   (deploy artifact name)
#   SM_CFG         e.g. mx95evk   (imx-sm build cfg; empty -> skip SM)
#   SM_M           e.g. 2
# CONFIRM these per board with the emulator session before a real run (§7).
set -euo pipefail
: "${MACHINE:?}" "${DISTRO:?}" "${MANIFEST_BRANCH:?}" "${MANIFEST_XML:?}" "${IMAGE_TARGET:?}" "${DTB_NAME:?}"
OUT=/out; mkdir -p "$OUT"
cd "$HOME"

# Resource budget (set by tools/build-nxp-bsp.sh; both fall back to nproc for a manual
# `docker run`, preserving the old behaviour). BB_JOBS = recipes in parallel
# (BB_NUMBER_THREADS); BB_MAKE = make -j inside each recipe (PARALLEL_MAKE). The host
# wrapper ALSO passes docker --cpus / --memory as hard kernel-level ceilings.
BB_JOBS="${BB_JOBS:-$(nproc)}"
BB_MAKE="${BB_MAKE:-$(nproc)}"

# `repo init` runs `git var GIT_COMMITTER_IDENT` on the manifest and aborts if git
# has no committer identity. The builder user is fresh (no ~/.gitconfig), so set a
# throwaway identity — it only labels local manifest commits, never anything shipped.
git config --global user.email "builder@holobench.local"
git config --global user.name  "Holobench Builder"
git config --global --add safe.directory '*'   # avoid dubious-ownership stops on mounted trees
git config --global color.ui false

echo "==> repo init ($MANIFEST_BRANCH / $MANIFEST_XML) + SHALLOW sync (--depth 1)"
# --depth 1: clone only the tip of each Yocto meta-layer, NOT full history. Full
# clones of poky etc. are 700k+ objects and crawl regardless of bandwidth (git
# index-pack is the bottleneck, not the network). Shallow is safe here: repo sync
# only fetches the META-LAYERS; bitbake fetches the actual kernel/u-boot/app
# sources (SRC_URI) itself during the build, so no git history is needed.
repo init --depth=1 -u https://github.com/nxp-imx/imx-manifest -b "$MANIFEST_BRANCH" -m "$MANIFEST_XML"
repo sync -j"$BB_JOBS" --no-clone-bundle --optimized-fetch

# crates.io: route crate fetches through the CDN (durable fix for the 403s).
# walnascar's bitbake (2.12) crate fetcher builds the .crate URL from the API host
# crates.io/api/v1/crates, which 403s crawler UAs and rate-limits to 1 req/s -> crate
# fetches (rutabaga-gfx-ffi, remain, ...) fail under a parallel burst. Upstream bitbake
# fixed this (commit f3904634, in scarthgap 2.8) by pointing at the CDN static.crates.io,
# which has neither limit and serves the same .crate files; it was NOT backported to 2.12.
# Apply that one-line backport directly to the synced crate.py (the build TOOL, not the
# QEMU model/BSP -> no Prime-Directive issue). static.crates.io/crates/<n>/<v>/download is
# curl-verified to return 200 with a plain UA. This supersedes the FETCHCMD UA workaround
# below as the PRIMARY crate path; the UA line is kept as residual defense and can be
# dropped once this is confirmed on a clean from-scratch build.
# The sed is IDEMPOTENT (the old string is gone after it runs) and a NO-OP if the line
# isn't found (e.g. a future bitbake that already carries the fix), so it can never break
# a build. versionsurl also moves to the CDN but is only used by latest-version checks,
# not a normal image build, so it doesn't matter here.
CRATE_PY="$HOME/sources/poky/bitbake/lib/bb/fetch2/crate.py"
if [ -f "$CRATE_PY" ] && grep -q "host = 'crates.io/api/v1/crates'" "$CRATE_PY"; then
  sed -i "s#host = 'crates.io/api/v1/crates'#host = 'static.crates.io/crates'#" "$CRATE_PY"
  echo "==> patched bitbake crate fetcher to use the static.crates.io CDN (avoids crates.io API 403s)"
fi

echo "==> imx-setup-release (NXP EULA prompt — accept it to continue)"
# This is the interactive EULA gate; do NOT pre-accept. MACHINE/DISTRO select the build.
# NXP's setup scripts reference unset vars (e.g. fsl_setup_help) and aren't written
# for a strict shell, so relax errexit+nounset around the source (they run fine in a
# normal interactive shell). pipefail stays. Restore strictness afterward.
set +eu
MACHINE="$MACHINE" DISTRO="$DISTRO" source ./imx-setup-release.sh -b build
set -eu

# We're now in the build dir (conf/local.conf exists). Robustness tweaks:
#
# (0) BOUND PARALLELISM. Without this bitbake defaults both knobs to nproc, giving up
#     to nproc*nproc concurrent compilers (32*32 ~= 1024 on a 32-core host) -> CPU
#     thrash + RAM blowout that destabilises/crashes the host. BB_NUMBER_THREADS =
#     recipes run in parallel (= BB_JOBS); PARALLEL_MAKE = make -j inside each recipe
#     (= BB_MAKE, default 4 — a low per-recipe value keeps the peak compiler count, and
#     thus peak RAM, sane). The host wrapper also passes docker --cpus / --memory.
echo "BB_NUMBER_THREADS = \"$BB_JOBS\""    >> conf/local.conf
echo "PARALLEL_MAKE = \"-j $BB_MAKE\""     >> conf/local.conf
#     Pressure regulation: even with the counts bounded, the heavy phases (Qt, image
#     assembly) can still spike RAM past physical and push the HOST into swap — which
#     stalls bitbake's own coordinator until it gives up ("Timeout waiting for the
#     bitbake server", build aborts ~80% in). BB_PRESSURE_MAX_* makes bitbake stop
#     LAUNCHING new tasks whenever CPU / IO / memory pressure (read from the host's
#     /proc/pressure) exceeds the threshold, so it adaptively backs off instead of
#     thrashing. 15000 is the value from the bitbake manual's example.
echo 'BB_PRESSURE_MAX_MEMORY = "15000"'    >> conf/local.conf
echo 'BB_PRESSURE_MAX_CPU = "15000"'       >> conf/local.conf
echo 'BB_PRESSURE_MAX_IO = "15000"'        >> conf/local.conf
#
# (1) Persist downloads + sstate across runs (mounted at /cache). A from-scratch
#     walnascar build pulls thousands of crates/tarballs; persisting DL_DIR means a
#     re-run after a transient fetch failure only re-fetches what's missing (no
#     second crates.io burst), and SSTATE_DIR makes re-runs incremental, not hours.
if [ -d /cache ]; then
  mkdir -p /cache/downloads /cache/sstate-cache
  echo 'DL_DIR = "/cache/downloads"'        >> conf/local.conf
  echo 'SSTATE_DIR = "/cache/sstate-cache"' >> conf/local.conf
  # Clear incomplete downloads left by a prior interrupted/killed run. wget --continue
  # would otherwise try to RESUME these corrupt partials (e.g. a 206 Partial Content
  # that fails checksum -> "Unable to fetch from any source"), failing fetches that
  # would succeed from scratch. Only removes *.tmp (incomplete) — never finished files.
  rm -f /cache/downloads/*.tmp 2>/dev/null || true
fi
# (2) wget retry tuning. Keep wget's DEFAULT User-Agent — do NOT spoof a browser.
#     History: a "--user-agent=Mozilla/5.0" was once set here to dodge crates.io's
#     api/v1 403 (it redirects a browser UA to the CDN). But crates are now routed
#     straight to static.crates.io by the crate.py backport above (no UA games), so
#     the spoof is no longer needed — and it actively BREAKS SourceForge: with a
#     browser UA, downloads.sourceforge.net serves its HTML "choose a mirror"
#     interstitial instead of the file, so bitbake saves ~130KB of HTML as the
#     tarball and do_fetch fails the checksum (observed: half-2.1.0.zip,
#     DevIL-1.8.0.zip). wget's default UA gets the real file. So: default UA + just
#     the resilience flags (more tries / longer waits) for flaky upstreams.
echo 'FETCHCMD_wget = "/usr/bin/env wget --tries=8 --timeout=100 --waitretry=20 --retry-connrefused --continue --progress=dot --verbose"' >> conf/local.conf
# (3) Upstream source availability: a build that fetches sed/gawk/kernel/etc. straight
#     from ftp.gnu.org & friends dies whenever one of those hosts is down (observed:
#     ftp.gnu.org unreachable mid-build). The Yocto Project keeps an official source
#     mirror of (nearly) all OE source tarballs. own-mirrors makes bitbake try that
#     reliable mirror FIRST for every fetch, falling back to upstream only if missing.
#     This is the canonical "don't depend on a given night's upstream" fix and makes
#     gnu.org-style outages a non-event. (It does NOT carry crates -> crates still use
#     the UA path above; see [[buildme-followups]].)
echo 'INHERIT += "own-mirrors"' >> conf/local.conf
echo 'SOURCE_MIRROR_URL = "https://downloads.yoctoproject.org/mirror/sources/"' >> conf/local.conf

echo "==> bitbake $IMAGE_TARGET (multi-hour)"
# -k / --continue: keep going after a task failure instead of stopping at the FIRST
# one. The from-scratch fetch hits hundreds of upstream hosts; some 404 on all
# mirrors AND serve wrong/missing files (rutabaga, remain, devil, half all did).
# Without -k, each bad fetch fails the whole build and the next only surfaces on the
# NEXT run — death by a thousand re-runs. With -k, ONE run surfaces ALL the broken
# fetches at once so they can be mirrored/pre-seeded in a single batch. Exit code is
# still non-zero if anything failed; a fully-clean run still completes normally.
bitbake -k "$IMAGE_TARGET"

DEPLOY="tmp/deploy/images/$MACHINE"
echo "==> staging artifacts from $DEPLOY -> $OUT"
# IMPORTANT: --remove-destination. The asset dir may already contain a SYMLINK at
# the target name (e.g. operator-supplied Image -> some other tree). Plain `cp -L`
# would FOLLOW that dest symlink and write THROUGH it — failing if the target path
# isn't reachable in the container, or (worse, on a host run) clobbering whatever
# the link points at. --remove-destination unlinks first, so we always write a
# fresh regular file INSIDE $OUT and never touch anything outside it.
rm -f "$OUT/disk.wic"
cp --remove-destination -L "$DEPLOY/Image" "$OUT/Image"
cp --remove-destination -L "$DEPLOY/$DTB_NAME" "$OUT/$DTB_NAME"
# rootfs SD image -> disk.wic (decompressed). Pick the ACTUAL image, never a sidecar:
# a `*.wic*` glob also matches `.wic.bmap` (the block-map XML) and `.wic.json`, and
# `.bmap` sorts BEFORE `.gz`/`.zst`, so `... | head -1` grabbed the 4KB bmap instead
# of the image. Match exact suffixes in priority order (plain, then zst/gz/xz) and
# decompress accordingly. The leading `*` covers the `.rootfs.` infix newer Yocto adds.
wic_plain="$(ls -1 "$DEPLOY"/*.wic 2>/dev/null | head -1 || true)"
wic_comp="$(ls -1 "$DEPLOY"/*.wic.zst "$DEPLOY"/*.wic.gz "$DEPLOY"/*.wic.xz 2>/dev/null | head -1 || true)"
if [ -n "$wic_plain" ]; then
  cp --remove-destination -L "$wic_plain" "$OUT/disk.wic"
else
  case "$wic_comp" in
    *.zst) zstd -d -f "$wic_comp" -o "$OUT/disk.wic" ;;
    *.gz)  gzip -dc "$wic_comp" > "$OUT/disk.wic" ;;
    *.xz)  xz -dc "$wic_comp"  > "$OUT/disk.wic" ;;
    *) echo "error: no .wic rootfs image found in $DEPLOY (only sidecars?)" >&2; exit 1 ;;
  esac
fi

if [ -n "${SM_CFG:-}" ]; then
  echo "==> building SM firmware (imx-sm ${SM_TAG:-default} cfg=$SM_CFG M=${SM_M:-2}) — creds-free"
  if [ -n "${SM_TAG:-}" ]; then
    git clone --depth 1 --branch "$SM_TAG" https://github.com/nxp-imx/imx-sm "$HOME/imx-sm"
  else
    git clone --depth 1 https://github.com/nxp-imx/imx-sm "$HOME/imx-sm"
  fi
  make -C "$HOME/imx-sm" -j"$BB_JOBS" cfg="$SM_CFG" M="${SM_M:-2}"
  cp --remove-destination -L "$HOME/imx-sm/build/$SM_CFG/m33_image.elf" "$OUT/m33_image_M2.elf"
fi

echo "==> BSP BUILD COMPLETE — artifacts in the mounted asset dir:"
ls -la "$OUT"

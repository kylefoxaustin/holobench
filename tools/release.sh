#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-or-later
#
# One-command release: build the DISTRIBUTABLE board images (OSS app + GPL forked
# qemu only — NO NXP BSP artifacts; operators volume-mount their own at runtime,
# see docs/DEPLOY.md), tag + push them to GHCR (rolling + pinned), and optionally
# cut the GitHub release. Must run on a host that has the forked qemu builds — a
# GitHub-hosted runner doesn't, so this is for your box. No BSP/golden needed.
#
# Usage:
#   tools/release.sh <version> [board ...]        # build + push images
#   tools/release.sh v0.2.4                       # default boards (95/91/93 full distro)
#   RELEASE_NOTES=notes.md tools/release.sh v0.2.4 --gh-release   # also cut the GH release
#
# Each <board> is a *profile id* whose image is built via docker/build.sh and
# published as ghcr.io/$GHCR_OWNER/holobench:<short>  +  :<short>-<version>,
# where <short> drops a trailing "-evk" (imx95-evk-sd -> imx95-sd).
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"; cd "$REPO"
GHCR_OWNER="${GHCR_OWNER:-kylefoxaustin}"
REG="ghcr.io/$GHCR_OWNER/holobench"

VERSION="${1:?usage: release.sh <version> [board ...] [--gh-release]}"; shift || true
GH_RELEASE=0; BOARDS=()
for a in "$@"; do
  [ "$a" = "--gh-release" ] && { GH_RELEASE=1; continue; }
  BOARDS+=("$a")
done
[ ${#BOARDS[@]} -eq 0 ] && BOARDS=(imx95-evk-sd imx91-evk-sd imx93-evk-sd)

echo "== release $VERSION : ${BOARDS[*]} -> $REG =="
declare -A DIGEST
for board in "${BOARDS[@]}"; do
  short="${board%-evk-sd}-sd"; short="${short/-evk/}"   # imx95-evk-sd->imx95-sd
  echo; echo "---- build $board (-> $short) ----"
  IMAGE="holobench:$board" docker/build.sh "$board" "$board"
  for tag in "$short" "$short-$VERSION"; do
    docker tag "holobench:$board" "$REG:$tag"
    echo "  push $REG:$tag"
    out="$(docker push "$REG:$tag" 2>&1 | tee /dev/stderr)"
    DIGEST["$short"]="$(printf '%s' "$out" | grep -oE 'sha256:[0-9a-f]+' | head -1)"
  done
done

echo; echo "== pushed =="
for s in "${!DIGEST[@]}"; do echo "  $REG:$s  ${DIGEST[$s]}"; done

if [ "$GH_RELEASE" = 1 ]; then
  echo; echo "== cutting GitHub release $VERSION =="
  notes="${RELEASE_NOTES:-}"
  if [ -n "$notes" ] && [ -f "$notes" ]; then
    gh release create "$VERSION" --repo "$GHCR_OWNER/holobench" --target main \
      --title "Holobench $VERSION" --notes-file "$notes"
  else
    gh release create "$VERSION" --repo "$GHCR_OWNER/holobench" --target main \
      --title "Holobench $VERSION" --generate-notes
  fi
fi
echo "== done =="

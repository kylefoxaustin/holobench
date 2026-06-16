#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-or-later
#
# One-command release: build the DISTRIBUTABLE board images (OSS app + GPL forked
# qemu only — NO NXP BSP artifacts; operators volume-mount their own at runtime,
# see docs/DEPLOY.md), tag them locally, and optionally cut the GitHub release.
# Must run on a host that has the forked qemu builds. No BSP/golden needed.
#
# Holobench does NOT publish prebuilt images: users build from source (the image
# is OSS + GPL qemu only). Pushing to a registry is OPT-IN via --push-ghcr (left
# off by default so a release doesn't recreate a deleted package). Even then it is
# only ever safe because the image carries no NXP artifacts.
#
# Usage:
#   tools/release.sh <version> [board ...]                 # build + tag locally
#   tools/release.sh v0.3.1 --gh-release                   # also cut the GH release
#   tools/release.sh v0.3.1 --push-ghcr --gh-release       # also push images (opt-in)
#   RELEASE_NOTES=notes.md tools/release.sh v0.3.1 --gh-release
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"; cd "$REPO"
GHCR_OWNER="${GHCR_OWNER:-kylefoxaustin}"
REG="ghcr.io/$GHCR_OWNER/holobench"

VERSION="${1:?usage: release.sh <version> [board ...] [--push-ghcr] [--gh-release]}"; shift || true
GH_RELEASE=0; PUSH_GHCR=0; BOARDS=()
for a in "$@"; do
  case "$a" in
    --gh-release) GH_RELEASE=1 ;;
    --push-ghcr)  PUSH_GHCR=1 ;;
    *) BOARDS+=("$a") ;;
  esac
done
[ ${#BOARDS[@]} -eq 0 ] && BOARDS=(imx95-evk-sd imx91-evk-sd imx93-evk-sd)

echo "== release $VERSION : ${BOARDS[*]} (push-ghcr=$PUSH_GHCR) =="
declare -A DIGEST
for board in "${BOARDS[@]}"; do
  short="${board%-evk-sd}-sd"; short="${short/-evk/}"   # imx95-evk-sd->imx95-sd
  echo; echo "---- build $board (-> $short) ----"
  IMAGE="holobench:$board" docker/build.sh "$board" "$board"
  for tag in "$short" "$short-$VERSION"; do
    docker tag "holobench:$board" "$REG:$tag"
    if [ "$PUSH_GHCR" = 1 ]; then
      echo "  push $REG:$tag"
      out="$(docker push "$REG:$tag" 2>&1 | tee /dev/stderr)"
      DIGEST["$short"]="$(printf '%s' "$out" | grep -oE 'sha256:[0-9a-f]+' | head -1)"
    fi
  done
done

if [ "$PUSH_GHCR" = 1 ]; then
  echo; echo "== pushed =="
  for s in "${!DIGEST[@]}"; do echo "  $REG:$s  ${DIGEST[$s]}"; done
else
  echo; echo "== built + tagged locally (no registry push; use --push-ghcr to publish) =="
fi

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

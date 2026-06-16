#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-or-later
# Assemble a board artifact set on the OPERATOR host. Compliance: nothing NXP
# originates from holobench; NXP_TOKEN is operator-env only and never logged;
# EULA-gated files are hand-off (never auto-fetched); the SM firmware is built
# reproducibly with no creds. (Authored by the i.MX95 emulator session; see
# docs/SETUP.md §(b) NXP-credential fetch UX.)
# Usage:  ART=./artifacts ./fetch-nxp.sh imx95.manifest
# Manifest line (| delimited; # and blank lines ignored):
#   name | sha256 | required | kind | source | build_cmd | build_out
#   kind=build : source=repo_url ; build_cmd run in repo ; build_out=path in repo
#   kind=url   : source=url       ; uses $NXP_TOKEN from your env
#   kind=byo   : source=hint      ; you download via nxp.com login+EULA
set -euo pipefail
ART=${ART:-./artifacts}
MAN=${1:?usage: fetch-nxp.sh <manifest>}
WORK=${WORK:-./.fetch-work}
mkdir -p "$ART" "$WORK"
log(){ printf "%s\n" "$*" >&2; }
trim(){ printf "%s" "$1" | sed -e "s/^[[:space:]]*//" -e "s/[[:space:]]*$//"; }

do_build(){   # repo build_cmd build_out outname
  local repo=$1 cmd=$2 out=$3 name=$4 dir
  if [ -n "${IMX_SM_SRC:-}" ] && [ "$repo" = "https://github.com/nxp-imx/imx-sm" ]; then
    dir=$IMX_SM_SRC
  else
    dir=$WORK/$(basename "$repo" .git)
    [ -d "$dir/.git" ] || git clone --depth 1 "$repo" "$dir"
  fi
  log "build: ($cmd) in $dir"
  ( cd "$dir" && eval "$cmd" )
  cp -f "$dir/$out" "$ART/$name"
}
do_url(){     # url outname
  local url=$1 name=$2
  : "${NXP_TOKEN:?kind=url needs NXP_TOKEN in your own env (never logged, never leaves this host)}"
  log "fetch: $name"
  ( set +x; curl -fL --retry 2 -H "Authorization: Bearer $NXP_TOKEN" -o "$ART/$name" "$url" )
}
do_byo(){     # hint outname
  log "SUPPLY $2 -> $1"
  log "       download with YOUR nxp.com login + EULA, drop into $ART/"
}
verify(){     # name sha
  local f=$ART/$1 want=$2 got
  [ -e "$f" ] || { echo "MISSING |$1"; return 1; }
  [ -z "$want" ] && { echo "PRESENT |$1 (no sha pinned)"; return 0; }
  got=$(sha256sum "$f" | cut -d" " -f1)
  [ "$got" = "$want" ] && { echo "OK      |$1"; return 0; } || { echo "MISMATCH|$1 got=$got want=$want"; return 1; }
}
# pass 1: obtain (build / fetch / hand-off); never abort the whole run on one miss
while IFS="|" read -r name sha req kind src bcmd bout; do
  name=$(trim "${name:-}"); case "$name" in ""|\#*) continue;; esac
  kind=$(trim "${kind:-}")
  [ -e "$ART/$name" ] && continue
  case "$kind" in
    build) do_build "$(trim "$src")" "$bcmd" "$(trim "$bout")" "$name" || log "WARN build failed: $name";;
    url)   do_url   "$(trim "$src")" "$name" || log "WARN fetch failed: $name";;
    byo)   do_byo   "$src" "$name";;
    *)     log "WARN unknown kind=$kind for $name";;
  esac
done < "$MAN"
# pass 2: validate all required; print a table; exit 0 only when complete
rc=0; log ""; log "==== artifact status ($ART) ===="
while IFS="|" read -r name sha req kind src bcmd bout; do
  name=$(trim "${name:-}"); case "$name" in ""|\#*) continue;; esac
  sha=$(trim "${sha:-}"); req=$(trim "${req:-}")
  line=$(verify "$name" "$sha") || { [ "$req" = "true" ] && rc=1; }
  log "  $line  (required=$req)"
done < "$MAN"
[ "$rc" = 0 ] && log ">>> ALL REQUIRED ARTIFACTS PRESENT -- ready to run" || log ">>> INCOMPLETE -- supply/fix the MISSING/MISMATCH files above"
exit $rc

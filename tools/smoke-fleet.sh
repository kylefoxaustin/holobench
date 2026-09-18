#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-or-later
#
# smoke-fleet.sh — boot one board per SoC family and judge it on GUEST LIVENESS.
#
# ⭐ WHY THIS IS NOT "did QEMU start". A board whose QEMU runs and whose QMP answers can
# still have produced nothing at all — that is the distinction every rule in
# docs/fleet/validation-standard.md turns on. The pass condition here is a string the GUEST
# emitted.
#
# ⚠️ AND WHY IT READS A PER-BOARD CHANNEL. A first version of this check read console0.log
# for every board and reported the i.MX RT1180 as SILENT — zero lines, no output, looks
# dead. It was perfectly healthy: that firmware prints over ARM SEMIHOSTING, never touches
# LPUART1, and its profile says so in a comment. The console slot is deliberately silent and
# its log is CORRECTLY zero bytes.
#   Had that been reported it would have been a FALSE DEFECT FILED AGAINST ANOTHER SESSION'S
#   BOARD — from a check that could not tell "the subject is dead" from "I am listening on
#   the wrong channel" (validation-standard rule 2). So each board names its channel and the
#   evidence expected on it.
#
#   usage:  .venv/bin/... ; bash tools/smoke-fleet.sh          (all families)
#           BOARDS="imx93-evk imx91-evk" bash tools/smoke-fleet.sh
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOLD="${HOLD:-40}"

# board | channel | regex the GUEST must emit on that channel
MATRIX=$(cat <<'EOF'
imx95-evk|console|Booting Linux|Linux boots on the modelled DDR/GIC
imx93-evk|console|Booting Linux|Linux boots
imx91-evk|console|Booting Linux|Linux boots
mcxn947-evk|console|Booting Zephyr OS|Zephyr firmware runs
imxrt1180-evk-uart-device|qemu|PASSTHROUGH ready|semihosting firmware announces itself
EOF
)

RUNS="$REPO/scratchpad-consoles/runs"; mkdir -p "$RUNS"
LOG="$RUNS/smoke-fleet-$(date +%Y%m%d-%H%M%S).log"
exec > >(tee -a "$LOG") 2>&1
echo "📝 transcript: $LOG"
echo "══ fleet smoke — judged on GUEST liveness, per-board channel ══"

pass=0; fail=0
while IFS='|' read -r board chan want why; do
    [ -z "$board" ] && continue
    # ⚠️ BOARDS= was in the usage block before it was in the code — a flag that
    # existed only in prose. Documented-but-absent is the same defect class as
    # tested-but-inert; both read as working to anyone who does not try them.
    if [ -n "${BOARDS:-}" ] && [[ " $BOARDS " != *" $board "* ]]; then continue; fi
    printf "  %-30s " "$board"
    out=$(timeout $((HOLD + 150)) "$REPO/.venv/bin/holobench" launch "$board" \
            --no-reset --hold "$HOLD" --quiet-console --keep 2>&1)
    dir=$(printf '%s' "$out" | grep -oE "/tmp/holobench-[0-9]+/${board}-[0-9a-f]+" | head -1)
    if [ -z "$dir" ]; then echo "❌ LAUNCH FAILED"; fail=$((fail+1)); continue; fi
    src="$dir/console0.log"; [ "$chan" = "qemu" ] && src="$dir/qemu.log"
    if grep -aq "$want" "$src" 2>/dev/null; then
        echo "✅ ALIVE  ($why)"; pass=$((pass+1))
    else
        # ⚠️ Say WHICH channel was read and how big it was. "No output" without naming the
        # channel is the exact report that would have condemned a healthy board.
        echo "❌ no '$want' in $(basename "$src") ($(stat -c%s "$src" 2>/dev/null || echo 0) bytes)"
        fail=$((fail+1))
    fi
    # ⚠️ STOP IS NOT CLEANUP. `holobench stop` quits QEMU but leaves the work dir, so a
    # smoke sweep that "finished cleanly" left one directory per board behind. Reap them,
    # and REPORT what could not be reaped rather than assuming — a corpse list that counts
    # only what you managed to remove is a list of your successes.
    "$REPO/.venv/bin/holobench" stop "$(basename "$dir")" >/dev/null 2>&1
    rm -rf "$dir" 2>/dev/null
    [ -d "$dir" ] && echo "     ⚠️  work dir NOT removed: $dir"
done <<< "$MATRIX"

left=$(ls -d /tmp/holobench-$(id -u)/*/ 2>/dev/null | wc -l)
procs=$(ps -eo args --no-headers | grep -c "[q]emu-system.*holobench-$(id -u)")
echo "── corpse list: $procs qemu proc(s), $left work dir(s) remaining ──"

echo "── $pass alive, $fail not ──"
echo "📝 transcript: $LOG"
[ "$fail" -eq 0 ]

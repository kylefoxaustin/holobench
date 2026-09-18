#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-or-later
#
# revalidate.sh — THE EMULATOR MOVED. DOES THE BOARD STILL BOOT?
#
# Ties a change in any emulator's QEMU binary to a re-run of the boot test, so "someone
# rebuilt their tree" stops being something we find out about weeks later by accident.
#
#   usage:  bash tools/revalidate.sh            # test only what changed
#           CHECK_ONLY=1 bash tools/revalidate.sh # report what moved, BOOT NOTHING
#           FORCE=1 bash tools/revalidate.sh      # test everything regardless
#
# ⚠️ CHECK_ONLY EXISTS BECAUSE ITS ABSENCE BIT ME IMMEDIATELY. "Show me what changed" is the
# first thing anyone runs, and without it this script silently boots five boards. I ran it
# expecting a report, piped it through `head`, and treated that as bounding the work — it
# bounds the OUTPUT YOU SEE, never the work. Two smoke sweeps ended up racing each other.
#
# ⭐ WHAT THIS RECORDS, AND WHAT IT DELIBERATELY DOES NOT.
# It maintains evidence/boot-baseline.json: "QEMU build <md5> was observed to boot <board>
# on <date>, evidenced by <string the guest emitted>". That is a BOOT BASELINE and it is a
# WEAKER CLAIM THAN A PIN.
#
#   ⚠️ IT NEVER TOUCHES `qemu.binary_pin`. A pin means "this binary was validated FOR THIS
#   LAB" — for the 2-port profile that meant real silicon on the far end of two cables,
#   381 body-validated frames, and a human running it. A board reaching a login prompt does
#   not establish that. Auto-promoting a smoke pass to a pin would manufacture exactly the
#   courtesy-pin this project twice refused from the emulator sessions, and it would look
#   like provenance while certifying almost nothing.
#
# ⭐ AND THE LIVENESS TEST IS NOT REIMPLEMENTED HERE. It shells out to smoke-fleet.sh, which
# owns the per-board channel matrix. Two definitions of "alive" would drift, and the one
# that drifts is always the one nobody is looking at.
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BASE="$REPO/evidence/boot-baseline.json"
PY="$REPO/.venv/bin/python"
RUNS="$REPO/scratchpad-consoles/runs"; mkdir -p "$RUNS"
LOG="$RUNS/revalidate-$(date +%Y%m%d-%H%M%S).log"
exec > >(tee -a "$LOG") 2>&1
echo "📝 transcript: $LOG"

BOARDS_ALL="imx95-evk imx93-evk imx91-evk mcxn947-evk imxrt1180-evk-uart-device"

echo "══ revalidate — which emulator builds have moved since we last booted them? ══"
changed=""
for b in $BOARDS_ALL; do
    # resolve the profile's binary + hash it, via the package (one source of truth)
    out=$("$PY" -c "
import hashlib, sys
sys.path.insert(0, '$REPO/backend')
from holobench.profiles.loader import load_profile
from pathlib import Path
p = load_profile('$b'); b = Path(p.qemu.binary)
print(b, hashlib.md5(b.read_bytes()).hexdigest() if b.is_file() else 'MISSING')
" 2>/dev/null)
    path=${out% *}; md5=${out##* }
    was=$("$PY" -c "
import json,sys
try: print(json.load(open('$BASE')).get('$b',{}).get('qemu_md5','') or 'none')
except Exception: print('none')" 2>/dev/null)
    if [ "$md5" = "MISSING" ]; then
        printf "  %-28s ⚠️  binary missing (%s)\n" "$b" "$path"
    elif [ "${FORCE:-0}" = 1 ]; then
        printf "  %-28s → forced\n" "$b"; changed="$changed $b"
    elif [ "$was" = "none" ]; then
        printf "  %-28s → NO BASELINE yet\n" "$b"; changed="$changed $b"
    elif [ "$was" != "$md5" ]; then
        printf "  %-28s → CHANGED  %s… → %s…\n" "$b" "${was:0:12}" "${md5:0:12}"; changed="$changed $b"
    else
        printf "  %-28s ✅ unchanged (%s…)\n" "$b" "${md5:0:12}"
    fi
done

changed=$(echo $changed | xargs || true)
if [ "${CHECK_ONLY:-0}" = 1 ]; then
    echo
    if [ -n "$changed" ]; then
        echo "── CHECK_ONLY: would re-run the boot test for:$changed ──"
        echo "   (nothing was booted. drop CHECK_ONLY=1 to actually prove them.)"
    else
        echo "── CHECK_ONLY: nothing moved ──"
    fi
    echo "📝 transcript: $LOG"; exit 0
fi
if [ -z "$changed" ]; then
    echo "── nothing moved; nothing to re-prove ──"; echo "📝 transcript: $LOG"; exit 0
fi

echo
echo "── re-running the boot test for:$changed ──"
BOARDS="$changed" bash "$REPO/tools/smoke-fleet.sh"
rc=$?

echo
if [ "$rc" -ne 0 ]; then
    # ⚠️ A FAILING BOARD MUST NOT UPDATE ITS BASELINE. The baseline records builds that were
    # OBSERVED TO BOOT; writing a failing one in would turn the record into a log of what we
    # last ran rather than of what last worked.
    echo "🛑 at least one board did NOT boot on its new build. BASELINE NOT UPDATED."
    echo "   The old baseline still names the last build known to work — which is the only"
    echo "   thing that makes it worth having. Fix the board or the build, then re-run."
    echo "📝 transcript: $LOG"
    exit 1
fi

"$PY" - "$BASE" $changed <<'PYEOF'
import hashlib, json, sys, datetime
from pathlib import Path
base = Path(sys.argv[1]); boards = sys.argv[2:]
sys.path.insert(0, str(base.parents[1] / "backend"))
from holobench.profiles.loader import load_profile
data = json.loads(base.read_text()) if base.is_file() else {}
for b in boards:
    p = load_profile(b); f = Path(p.qemu.binary)
    data[b] = {
        "qemu_md5": hashlib.md5(f.read_bytes()).hexdigest(),
        "qemu_path": str(f),
        "booted_on": datetime.date.today().isoformat(),
        "claim": "OBSERVED TO BOOT (smoke liveness). NOT a binary_pin — no lab, no silicon.",
    }
base.parent.mkdir(parents=True, exist_ok=True)
base.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
print(f"  baseline updated for: {' '.join(boards)}")
PYEOF
echo "📝 transcript: $LOG"

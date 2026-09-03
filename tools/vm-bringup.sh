#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-or-later
#
# vm-bringup.sh — get holobench able to boot an i.MX board on a machine where you have
#                 NO ROOT. Nothing here uses sudo, and nothing writes outside $HOME.
#
# ⚠️ THE THING THAT ACTUALLY BLOCKS YOU, stated first because the error message does not
# say it: STOCK QEMU CANNOT RUN THESE BOARDS. Ubuntu's qemu-system-aarch64 ships imx25,
# imx6ul and imx7d and has ZERO imx95 machines — verified, not assumed. The
# imx95-19x19-evk / imx93-11x11-evk / imx91-11x11-evk machines exist only in the emulator
# forks. So "install qemu" is not a fix; you need the FORKED binary, and this script's
# whole job is to get you one and prove it works.
#
#   usage:
#     bash tools/vm-bringup.sh                       # diagnose only, change nothing
#     HB_QEMU_URL=<url>  bash tools/vm-bringup.sh    # fetch a prebuilt forked binary
#     HB_QEMU_SRC=<path> bash tools/vm-bringup.sh    # copy one you already have
#     HB_BUILD=1         bash tools/vm-bringup.sh    # git clone + build it here
#
# ⭐ THE SUCCESS TEST IS NOT "FILES WERE FETCHED". It is `-M help | grep imx95`: the binary
# runs AND carries the machine. A script that reports success for having downloaded
# something is the same defect this repo keeps finding — a check that cannot fail the way
# the thing it checks actually fails.
set -uo pipefail

PREFIX="${HB_PREFIX:-$HOME/opt/holobench}"
BIN="$PREFIX/bin"; LIB="$PREFIX/lib"; WORK="$PREFIX/work"
mkdir -p "$BIN" "$LIB" "$WORK"
LOG="$PREFIX/bringup-$(date +%Y%m%d-%H%M%S).log"
exec > >(tee -a "$LOG") 2>&1
echo "📝 transcript: $LOG"

ok(){ echo "  ✅ $*"; }; no(){ echo "  ❌ $*"; }; warn(){ echo "  ⚠️  $*"; }
say(){ echo; echo "── $* ──────────────────────────────────────────"; }

# ── 1. what have we got ─────────────────────────────────────────────────────────────────
say "ENVIRONMENT (no root needed for any of this)"
for t in git curl python3 dpkg-deb apt-get; do
    printf "  %-10s %s\n" "$t" "$(command -v "$t" || echo 'MISSING — some routes unavailable')"
done
for t in gcc make ninja meson pkg-config; do
    printf "  %-10s %s\n" "$t" "$(command -v "$t" || echo 'missing (only needed for HB_BUILD=1)')"
done
echo "  home free: $(df -h "$HOME" | awk 'NR==2{print $4}')"

# ── 2. obtain the forked binary ─────────────────────────────────────────────────────────
QEMU="$BIN/qemu-system-aarch64"
say "FORKED QEMU"
if [ -x "$QEMU" ]; then
    ok "already present at $QEMU"
elif [ -n "${HB_QEMU_SRC:-}" ]; then
    cp "$HB_QEMU_SRC" "$QEMU" && chmod +x "$QEMU" && ok "copied from $HB_QEMU_SRC"
elif [ -n "${HB_QEMU_URL:-}" ]; then
    echo "  fetching $HB_QEMU_URL"
    curl -fL --progress-bar -o "$QEMU" "$HB_QEMU_URL" && chmod +x "$QEMU" \
        && ok "downloaded" || { no "download failed"; QEMU=""; }
elif [ "${HB_BUILD:-0}" = 1 ]; then
    # ⭐ A C COMPILER IS THE ONLY THING YOU CANNOT WORK AROUND WITHOUT ROOT.
    # meson and ninja install from pip; the -dev headers unpack from .debs; gcc does neither.
    command -v gcc >/dev/null || {
        no "no C compiler. meson/ninja come from pip and headers unpack from .debs, but gcc"
        echo "     cannot be obtained without root. Use HB_QEMU_URL=<release asset> instead."
        exit 2; }
    ok "gcc: $(gcc -dumpversion)"

    # meson + ninja via pip --user (verified: both are pip-installable, no root)
    export PATH="$HOME/.local/bin:$PATH"
    for t in meson ninja; do
        command -v $t >/dev/null || { echo "  pip installing $t…"
            python3 -m pip install --user -q "$t" 2>&1 | tail -1
            command -v $t >/dev/null && ok "$t $( $t --version 2>/dev/null | head -1)" \
                                     || { no "pip could not provide $t"; exit 2; }; }
    done

    # QEMU's hard pkg-config requirements are exactly these three (from its meson.build).
    # Stage the -dev packages into $PREFIX if the headers are not already present.
    export PKG_CONFIG_PATH="$LIB/pkgconfig:$PREFIX/usr/lib/x86_64-linux-gnu/pkgconfig:${PKG_CONFIG_PATH:-}"
    need=""
    for m in glib-2.0 pixman-1 zlib; do pkg-config --exists "$m" 2>/dev/null || need="$need $m"; done
    if [ -n "$need" ]; then
        warn "missing dev headers for:$need — trying to stage them without root"
        # ⚠️ THE FRAGILE PART, AND I AM NOT PRETENDING OTHERWISE. glib's .pc file pulls
        # transitive deps (libffi, pcre2, mount, blkid, selinux) whose own .pc files must
        # also be staged, and one missing link fails configure with a confusing message.
        # If this does not work, the release-asset route is faster than debugging it.
        ( cd "$WORK" && for d in libglib2.0-dev libpixman-1-dev zlib1g-dev \
                                 libffi-dev libpcre2-dev libmount-dev libblkid-dev libselinux1-dev; do
            apt-get download "$d" >/dev/null 2>&1 && dpkg-deb -x "$d"_*.deb "$PREFIX" 2>/dev/null \
              && echo "      staged $d"; done ) || true
        still=""
        for m in glib-2.0 pixman-1 zlib; do pkg-config --exists "$m" 2>/dev/null || still="$still $m"; done
        [ -z "$still" ] && ok "headers resolve now" || {
            no "still missing:$still — use HB_QEMU_URL=<release asset> instead"; exit 2; }
    else
        ok "glib-2.0, pixman-1, zlib headers all present"
    fi

    src="$WORK/qemu-imx95"
    [ -d "$src/.git" ] || git clone --depth 1 --branch "${HB_QEMU_REF:-main}" \
        https://github.com/kylefoxaustin/qemu-imx95 "$src" || exit 2
    # ⭐ --enable-tools EXPLICITLY: that is what produces qemu-img. The tagbuild on the
    # reference host omitted it and has no qemu-img, which is exactly the gap that started
    # this whole thread.
    ( cd "$src" && mkdir -p build && cd build \
      && ../configure --target-list=aarch64-softmmu --enable-tools --prefix="$PREFIX" \
      && make -j"$(nproc)" ) || { no "build failed — see above"; exit 2; }
    cp "$src/build/qemu-system-aarch64" "$QEMU"
    [ -x "$src/build/qemu-img" ] && cp "$src/build/qemu-img" "$BIN/qemu-img" && ok "qemu-img built too"
    ok "built from source"
else
    no "no forked QEMU yet, and none requested."
    echo "     Pick one:  HB_QEMU_URL=<url>   HB_QEMU_SRC=<path>   HB_BUILD=1"
fi

# ── 3. resolve its libraries, rootlessly ────────────────────────────────────────────────
if [ -n "$QEMU" ] && [ -x "$QEMU" ]; then
    say "SHARED LIBRARIES"
    missing=$(LD_LIBRARY_PATH="$LIB" ldd "$QEMU" 2>/dev/null | awk '/not found/{print $1}' | sort -u)
    if [ -z "$missing" ]; then
        ok "every library resolves"
    else
        echo "  missing: $(echo $missing | tr '\n' ' ')"
        # ⭐ apt-get download + dpkg-deb -x need NO ROOT. Verified on jammy.
        if command -v apt-get >/dev/null && command -v dpkg-deb >/dev/null; then
            cd "$WORK" || exit 2
            for so in $missing; do
                pkg=$(apt-file search "$so" 2>/dev/null | head -1 | cut -d: -f1)
                [ -z "$pkg" ] && pkg=$(printf '%s' "$so" | sed 's/\.so.*//; s/^lib/lib/')
                echo "    fetching $so (guessing package '$pkg')"
                apt-get download "$pkg" >/dev/null 2>&1 \
                    && dpkg-deb -x "$pkg"_*.deb "$WORK/x" 2>/dev/null \
                    && find "$WORK/x" -name "$so*" -exec cp -n {} "$LIB/" \; \
                    && ok "$so staged into $LIB" || warn "could not resolve $so automatically"
            done
            still=$(LD_LIBRARY_PATH="$LIB" ldd "$QEMU" 2>/dev/null | awk '/not found/{print $1}')
            [ -z "$still" ] && ok "all libraries resolve now" || no "still missing: $still"
        else
            warn "no apt-get/dpkg-deb — cannot stage libraries automatically"
        fi
    fi
fi

# ── 4. qemu-img (only needed when a profile sets introspection.snapshots) ───────────────
say "qemu-img"
if [ -x "$BIN/qemu-img" ]; then ok "present at $BIN/qemu-img"
elif command -v apt-get >/dev/null && command -v dpkg-deb >/dev/null; then
    ( cd "$WORK" && apt-get download qemu-utils >/dev/null 2>&1 \
      && dpkg-deb -x qemu-utils_*.deb "$WORK/qu" 2>/dev/null \
      && cp "$WORK/qu/usr/bin/qemu-img" "$BIN/" ) \
      && ok "staged into $BIN" || warn "could not fetch qemu-utils"
else
    warn "not available; set introspection.snapshots: false in the profile instead"
fi

# ── 5. THE ONLY TEST THAT COUNTS ────────────────────────────────────────────────────────
say "VERDICT — does the binary RUN and carry the machine?"
rc=1
if [ -n "$QEMU" ] && [ -x "$QEMU" ]; then
    ver=$(LD_LIBRARY_PATH="$LIB" "$QEMU" --version 2>&1 | head -1)
    if [ -n "$ver" ] && LD_LIBRARY_PATH="$LIB" "$QEMU" --version >/dev/null 2>&1; then
        ok "runs: $ver"
        n=$(LD_LIBRARY_PATH="$LIB" "$QEMU" -M help 2>/dev/null | grep -ci imx9)
        if [ "${n:-0}" -gt 0 ]; then
            ok "carries $n i.MX9x machine(s):"
            LD_LIBRARY_PATH="$LIB" "$QEMU" -M help 2>/dev/null | grep -i imx9 | sed 's/^/       /'
            rc=0
        else
            no "RUNS BUT HAS NO i.MX9x MACHINE — this is a stock QEMU, not a fork."
            echo "     Stock builds carry imx25/imx6ul/imx7d only. You need the forked binary."
        fi
    else
        no "present but will not execute (see the library section above)"
    fi
else
    no "no binary to test"
fi

say "WHAT TO PUT IN YOUR ENVIRONMENT"
if [ "$rc" = 0 ]; then
    echo "  export HOLOBENCH_QEMU=$QEMU"
    [ -x "$BIN/qemu-img" ] && echo "  export HOLOBENCH_QEMU_IMG=$BIN/qemu-img"
    echo "  export LD_LIBRARY_PATH=$LIB\${LD_LIBRARY_PATH:+:\$LD_LIBRARY_PATH}"
    echo
    echo "  Then restart the holobench backend. HOLOBENCH_QEMU overrides the profile's"
    echo "  binary path, so no profile edit is needed (and imx95-evk carries no binary_pin,"
    echo "  so nothing will refuse the override)."
else
    echo "  NOTHING YET — the verdict above did not pass, so there is nothing honest to export."
fi
echo
echo "📝 transcript: $LOG"
exit "$rc"

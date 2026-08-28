#!/usr/bin/env bash
# Install this repo's hooks OUTSIDE the working tree.
#
# ⭐ WHY OUTSIDE: qualcomm, 2026-08-27 — "the detector cannot be the thing the failure
# removes." A hooks dir living in the tree is deleted by `git reset --hard` to a commit
# predating it, and git then runs NO hooks at all, silently — including the fleet push
# gate. Installing into .git/ puts the wiring outside the blast radius of every
# working-tree command (verified: reset --hard, checkout, clean -fd all leave .git/ alone).
#
# The cost is drift: an installed copy can diverge from the committed source. So the
# installed pre-commit compares itself against .githooks/ and REFUSES on mismatch.
#
# ⚠️ WHAT THIS STILL DOES NOT COVER: a FRESH CLONE has no .git/holobench-hooks and is
# therefore unarmed until someone runs this. Local hooks cannot fix that, because on a
# fresh clone no local hook exists to complain.
#
# ⭐ UPDATE 2026-08-28: CI IS NOW WIRED (.github/workflows/suite.yml) and runs this script
# on every push, from the exact fresh-clone state described above — so this file is now
# EXERCISED rather than merely documented, and a break in it turns the build red. That
# does not arm anyone's laptop; it means the repo's guarantee no longer depends on a
# human remembering. The remaining hole is one level out and named in the workflow: CI's
# own trigger is governed by the repository's Actions setting, which no file here can see.
set -e
ROOT="$(git rev-parse --show-toplevel)"
mkdir -p "$ROOT/.git/holobench-hooks"
cp "$ROOT/.githooks/pre-commit" "$ROOT/.githooks/pre-push" "$ROOT/.git/holobench-hooks/"
chmod +x "$ROOT/.git/holobench-hooks/"*
git -C "$ROOT" config core.hooksPath .git/holobench-hooks
echo "hooks installed -> .git/holobench-hooks (core.hooksPath set)"

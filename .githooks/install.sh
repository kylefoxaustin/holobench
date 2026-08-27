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
# therefore unarmed until someone runs this. Local hooks cannot fix that; only CI can,
# and CI is not wired here. Stated rather than left as an assumption.
set -e
ROOT="$(git rev-parse --show-toplevel)"
mkdir -p "$ROOT/.git/holobench-hooks"
cp "$ROOT/.githooks/pre-commit" "$ROOT/.githooks/pre-push" "$ROOT/.git/holobench-hooks/"
chmod +x "$ROOT/.git/holobench-hooks/"*
git -C "$ROOT" config core.hooksPath .git/holobench-hooks
echo "hooks installed -> .git/holobench-hooks (core.hooksPath set)"

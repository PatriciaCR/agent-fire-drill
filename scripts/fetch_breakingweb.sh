#!/usr/bin/env bash
# Fetch BreakingWeb at the pinned commit into third_party/breakingweb (gitignored) and export a pristine tree to
# build/breakingweb for image builds. Fails if the checked-out commit differs from the lock or the tree is modified.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"; source <(grep -E '^(repo|commit)=' "$ROOT/third_party/breakingweb.lock")
SRC="$ROOT/third_party/breakingweb"; OUT="$ROOT/build/breakingweb"
if [ ! -d "$SRC/.git" ]; then git clone -q "$repo" "$SRC"; fi
git -C "$SRC" fetch -q origin "$commit" 2>/dev/null || true
git -C "$SRC" checkout -q --detach "$commit"
[ "$(git -C "$SRC" rev-parse HEAD)" = "$commit" ] || { echo "BreakingWeb commit mismatch" >&2; exit 1; }
[ -z "$(git -C "$SRC" status --porcelain)" ] || { echo "BreakingWeb checkout has local modifications" >&2; exit 1; }
rm -rf "$OUT"; mkdir -p "$OUT"; git -C "$SRC" archive "$commit" | tar -x -C "$OUT"
echo "BreakingWeb $commit exported to build/breakingweb (pristine)"

#!/bin/sh
# Copies the current kit into every repo that uses it. Run this after changing the kit.
#   sh agent-memory-kit/sync-kit.sh [store-name ...]     (default: every store)
# Keeps each repo's Steering block and settings. In CLAUDE.md it replaces the kit's protocol block and
# leaves everything outside it alone. Commits nothing in a repo.
set -eu
kit=$(cd "$(dirname "$0")" && pwd)
[ $# -gt 0 ] || set -- $(ls "$HOME/agent-memory")
# Every repo is checked before any is touched: a sync either reaches all of them or none.
for name in "$@"; do
  store=$HOME/agent-memory/$name
  [ -f "$store/roots.txt" ] || { echo "$name has no roots.txt; run install-store.sh for it" >&2; exit 1; }
  while IFS= read -r repo; do
    [ -n "$repo" ] || continue
    [ -d "$repo" ] || { echo "$name lists $repo, which isn't there. Fix $store/roots.txt. Nothing was synced." >&2; exit 1; }
    python3 "$kit/merge_settings.py" "$kit" "$repo" "$name" --check || { echo "Nothing was synced." >&2; exit 1; }
  done <"$store/roots.txt"
done
for name in "$@"; do
  store=$HOME/agent-memory/$name
  while IFS= read -r repo; do
    [ -n "$repo" ] || continue
    while IFS= read -r f; do
      [ -n "$f" ] || continue
      mkdir -p "$repo/$(dirname "$f")"
      cp "$kit/$f" "$repo/$f"
    done <"$kit/kit-files.txt"
    python3 "$kit/merge_settings.py" "$kit" "$repo" "$name"
    sh "$kit/link-worktrees.sh" "$name" "$repo"
    sh "$kit/exclude-local.sh" "$repo"
    echo "synced: $repo"
  done <"$store/roots.txt"
  AGENT_MEMORY_DIR=$store python3 "$kit/scripts/build_index.py" >/dev/null
  git -C "$store" add INDEX.md
  git -C "$store" diff --cached --quiet || git -C "$store" commit -q -m "memory: index rebuilt from the store"
done

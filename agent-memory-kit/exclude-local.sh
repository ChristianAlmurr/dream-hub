#!/bin/sh
# Keeps what the kit leaves on this machine out of a repo's git status, without touching its .gitignore.
#   sh agent-memory-kit/exclude-local.sh <repo>
# install-store.sh and sync-kit.sh both run it, so a store set up by an older kit catches up at the next sync.
# The lines go in the repo's own .git/info/exclude, which is never committed. Running it again adds nothing.
set -eu
repo=$(cd "$1" && pwd)
common=$(cd "$repo" && git rev-parse --path-format=absolute --git-common-dir)
mkdir -p "$common/info"
exclude=$common/info/exclude
# A last line without a newline would swallow what is appended next.
[ ! -s "$exclude" ] || [ -z "$(tail -c1 "$exclude")" ] || printf '\n' >>"$exclude"
# The store link, this machine's settings, and the bytecode Python writes beside the kit's scripts.
for line in /memory /.claude/settings.local.json /scripts/__pycache__/; do
  grep -qxF "$line" "$exclude" 2>/dev/null || printf '%s\n' "$line" >>"$exclude"
done

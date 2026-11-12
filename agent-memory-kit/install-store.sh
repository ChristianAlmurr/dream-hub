#!/bin/sh
# Installs the kit into one or more repos that share one private memory store.
#   sh agent-memory-kit/install-store.sh <store-name> <repo> [repo ...]
# The store is its own git repo at ~/agent-memory/<store-name>, linked into each repo as memory/,
# so memory commits never touch the project's history. Safe to re-run; nothing is committed in a repo.
set -eu
kit=$(cd "$(dirname "$0")" && pwd)
[ $# -ge 2 ] || { echo "usage: install-store.sh <store-name> <repo> [repo ...]" >&2; exit 2; }
name=$1; shift
store=$HOME/agent-memory/$name
created=
if [ ! -e "$store" ]; then
  created=1
  mkdir -p "$store"
  cp -R "$kit/memory-template/." "$store/"
  git -C "$store" init -q -b main
  git -C "$store" add -A
  git -C "$store" commit -q -m "memory: init"
  echo "store created: $store"
fi
for repo in "$@"; do
  repo=$(cd "$repo" && pwd)
  git -C "$repo" rev-parse --git-common-dir >/dev/null
  if [ -e "$repo/memory" ] || [ -L "$repo/memory" ]; then
    [ "$(cd "$repo/memory" && pwd -P)" = "$(cd "$store" && pwd -P)" ] || {
      echo "$repo/memory already exists and is not this store; nothing changed there" >&2; exit 1; }
  else
    ln -s "$store" "$repo/memory"
  fi
  grep -qxF "$repo" "$store/roots.txt" 2>/dev/null || printf '%s\n' "$repo" >>"$store/roots.txt"
  mkdir -p "$repo/scripts" "$repo/.claude/commands" "$repo/.claude/agents"
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    if [ -e "$repo/$f" ]; then echo "kept existing $f in $repo (sync-kit.sh updates it)"; else cp "$kit/$f" "$repo/$f"; fi
  done <"$kit/kit-files.txt"
  python3 "$kit/merge_settings.py" "$kit" "$repo" "$name"
  sh "$kit/link-worktrees.sh" "$name" "$repo"
  sh "$kit/exclude-local.sh" "$repo"
  echo "installed: $repo"
done
# The human tier: any write to ORG.md fails, by any route, until a person lifts the flag.
command -v chflags >/dev/null && chflags uchg "$store/ORG.md"
# A new store starts fully set up: the alert, the index, and the daily dream check on a free evening hour.
if [ -n "$created" ]; then
  cp "$kit/notify.sh.template" "$store/notify.sh"
  chmod +x "$store/notify.sh"
  AGENT_MEMORY_DIR=$store python3 "$kit/scripts/build_index.py" >/dev/null
  if [ -z "${AGENT_KIT_NO_SCHEDULE:-}" ] && command -v claude >/dev/null; then
    for hour in 18 20 22 16 14 12 10; do
      grep -qs "<key>Hour</key><integer>$hour</integer>" "$HOME"/Library/LaunchAgents/ai.agent-memory.dream.*.plist || break
    done
    sh "$kit/schedule-dream.sh" "$repo" --hour "$hour"
  fi
fi
git -C "$store" add -A
git -C "$store" diff --cached --quiet || git -C "$store" commit -q -m "memory: add repos"
cat <<EOF

Next, in each repo:
  1. Start Claude there once and accept the trust dialog: cd <repo> && claude
  2. Fill in ORG.md, the Steering block in .claude/commands/dream.md, and project/gotchas.md
     (until you have written in ORG.md and Steering, sessions are not given them)
  3. Commit CLAUDE.md, scripts/ and .claude/ on a branch (memory/ and settings.local.json stay out by themselves)
EOF

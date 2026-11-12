#!/bin/sh
# Brings this clone of Dream Hub up to date with its origin, then copies the kit into your repos and restarts the hub.
#   sh update.sh
# Safe to run again: with nothing new it says so and changes nothing. It refuses, changing nothing, when the clone has
# uncommitted changes, is not on its default branch, has commits of its own, or can't reach origin. It moves the clone
# forward only (git merge --ff-only), never commits in your repos, and leaves running sessions alone: their terminals
# live in tmux, which a hub restart doesn't touch. The hub's Update button runs this same script.
set -u
here=$(cd "$(dirname "$0")" && pwd -P)
kit=$here/agent-memory-kit
say() { printf '%s\n' "$*"; }
fail() { printf 'update.sh: %s\n' "$*" >&2; exit 1; }
g() { GIT_TERMINAL_PROMPT=0 GIT_ASKPASS=/usr/bin/false GIT_SSH_COMMAND='ssh -o BatchMode=yes -o ConnectTimeout=10' git -C "$here" "$@"; }

[ -d "$here/.git" ] || fail "$here is not a git clone, so there is nothing to update from. Download a new copy instead."
g remote get-url origin >/dev/null 2>&1 || fail "this clone has no origin to update from."
branch=$(g symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null | sed 's|^origin/||')
[ -n "$branch" ] || branch=$(g ls-remote --symref origin HEAD 2>/dev/null | sed -n 's|^ref: refs/heads/\([^	]*\)	HEAD$|\1|p')
[ -n "$branch" ] || branch=main
current=$(g symbolic-ref --short -q HEAD) || fail "this clone isn't on a branch (a detached HEAD). Run: git -C $here checkout $branch, then sh update.sh again. Nothing was changed."
[ "$current" = "$branch" ] || fail "this clone is on $current, not $branch. Switch with: git -C $here checkout $branch, then run sh update.sh again. Nothing was changed."
[ -z "$(g status --porcelain --untracked-files=no)" ] || fail "this clone has uncommitted changes (git -C $here status). Commit or stash them, then run sh update.sh again. Nothing was changed."

before=$(g rev-parse HEAD)
g fetch --quiet --no-tags origin "$branch" || fail "couldn't reach origin ($(g remote get-url origin)). Nothing was changed; try again when you're online."
target=$(g rev-parse FETCH_HEAD)
if [ "$before" = "$target" ] || g merge-base --is-ancestor "$target" "$before"; then
  say "Dream Hub is up to date ($(g log -1 --format='%h, %cs'))."
  exit 0
fi
g merge-base --is-ancestor "$before" "$target" || fail "this clone has commits of its own that origin doesn't have, so it can't simply move forward. Update it by hand (git pull --rebase, or merge). Nothing was changed."
g merge --ff-only --quiet "$target" || fail "git couldn't move the clone forward. Nothing was changed; see git -C $here status."
say "Updated Dream Hub from $(g log -1 --format=%h "$before") to $(g log -1 --format='%h (%cs)'):"
g log --format='  %h %s' "$before..HEAD"

# The kit's files are copied into each repo of each store; a store with a dream in progress is left for later.
stores=$HOME/agent-memory
changed_kit=$(g diff --name-only "$before" HEAD -- agent-memory-kit | head -1)
if [ -n "$changed_kit" ] && [ -d "$stores" ]; then
  say ""
  say "Copying the kit into your repos:"
  no_vcs=$(/usr/bin/python3 -c 'import json, os, sys
try: c = json.load(open(os.path.expanduser("~/.config/agent-hub/config.json")))
except Exception: c = {}
for p in c.get("no_vcs") or []:
    if isinstance(p, str): print(os.path.realpath(os.path.join(os.path.expanduser("~"), os.path.expanduser(p))))' 2>/dev/null)
  for store in "$stores"/*/; do
    store=${store%/}; name=$(basename "$store")
    [ -f "$store/roots.txt" ] || continue
    AGENT_MEMORY_DIR=$store python3 "$kit/scripts/dream_owner.py" >/dev/null 2>&1
    if [ $? -eq 4 ]; then  # dream_owner.py exits 4 while someone is dreaming on the store
      say "  skipped $name: a dream is in progress. When it has finished: sh $kit/sync-kit.sh $name"
      continue
    fi
    if ! sh "$kit/sync-kit.sh" "$name" >/dev/null 2>"$here/.update-sync.err"; then
      say "  $name was not synced: $(head -1 "$here/.update-sync.err"). Fix that, then: sh $kit/sync-kit.sh $name"
      rm -f "$here/.update-sync.err"; continue
    fi
    rm -f "$here/.update-sync.err"
    while IFS= read -r repo; do
      [ -n "$repo" ] || continue
      if printf '%s\n' "$no_vcs" | grep -qxF "$(cd "$repo" 2>/dev/null && pwd -P)"; then
        say "  synced $repo (listed in no_vcs: not looked at with git)"
      elif [ -n "$(git -C "$repo" status --porcelain -- scripts .claude CLAUDE.md 2>/dev/null)" ]; then
        say "  synced $repo: its kit files changed; commit them there when you're ready"
      else
        say "  synced $repo: nothing to commit"
      fi
    done <"$store/roots.txt"
  done
elif [ -z "$changed_kit" ]; then
  say ""
  say "The kit didn't change, so nothing was copied into your repos."
fi

say ""
if [ -f "$HOME/Library/LaunchAgents/ai.agent-hub.plist" ]; then
  launchctl kickstart -k "gui/$(id -u)/ai.agent-hub" >/dev/null 2>&1 && say "Restarted the hub (ai.agent-hub). Open pages offer to reload; sessions keep running." \
    || say "Couldn't restart the hub's launchd job: run launchctl kickstart -k gui/$(id -u)/ai.agent-hub"
else
  say "Restart the hub to run the new version (stop it and start /usr/bin/python3 $here/hub/server.py). Sessions keep running."
fi

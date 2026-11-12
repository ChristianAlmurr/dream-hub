#!/bin/sh
# Gives every worktree of a repo the memory/ link, now and from here on, through a post-checkout hook.
#   sh agent-memory-kit/link-worktrees.sh <store-name> <repo>
# `git worktree add` runs post-checkout, so a new worktree is linked before anything works in it.
# A new worktree also gets its own .claude/settings.local.json: access to the store, and the repo's own
# setting for Claude's auto memory, so a store retired with retire-auto-memory.sh stays retired.
set -eu
name=$1; repo=$(cd "$2" && pwd)
store=$HOME/agent-memory/$name
common=$(git -C "$repo" rev-parse --path-format=absolute --git-common-dir)
hook=$common/hooks/post-checkout
mark="# agent-memory-kit link"
if [ -e "$hook" ] && ! grep -qF "$mark" "$hook"; then
  echo "$hook exists and isn't ours; add the memory/ link to it by hand" >&2; exit 1
fi
mkdir -p "$common/hooks"
cat >"$hook" <<EOF
#!/bin/sh
$mark
top=\$(git rev-parse --show-toplevel 2>/dev/null) || exit 0
[ -e "\$top/memory" ] || [ -L "\$top/memory" ] || ln -s "$store" "\$top/memory"
if [ ! -e "\$top/.claude/settings.local.json" ]; then
  mkdir -p "\$top/.claude"
  # What the repo's own local settings say about Claude's auto memory travels to a new worktree, so a
  # store retired with retire-auto-memory.sh stays retired there. scripts/own_notes.py knows how to read it.
  own=\$(python3 "$repo/scripts/own_notes.py" local "$repo") || own=unset
  python3 -c '
import json, sys
new, store, own = sys.argv[1:4]
data = {"permissions": {"additionalDirectories": [store]}}
if own in ("true", "false"):
    data["autoMemoryEnabled"] = own == "true"
with open(new, "w") as f:
    json.dump(data, f, indent=2)
    f.write(chr(10))
' "\$top/.claude/settings.local.json" "~/agent-memory/$name" "\$own"
fi
exit 0
EOF
chmod +x "$hook"
# Worktrees that already exist.
n=0
while IFS= read -r w; do  # a line at a time: a path may hold spaces
  [ -d "$w" ] || continue
  (cd "$w" && "$hook")
  n=$((n + 1))
done <<LIST
$(git -C "$repo" worktree list --porcelain | sed -n 's/^worktree //p')
LIST
echo "linked: $n worktree(s) of $repo"

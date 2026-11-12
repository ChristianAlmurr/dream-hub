#!/bin/sh
# Takes Dream Hub off this Mac, asking before each step. Your memory stores and your repos are kept unless you say so.
#   sh uninstall.sh [--yes]
set -eu
here=$(cd "$(dirname "$0")" && pwd -P)
kit=$here/agent-memory-kit
yes=; [ "${1:-}" = --yes ] && yes=1
say() { printf '%s\n' "$*"; }
ask() {
  [ -n "$yes" ] && { say "$1 yes (--yes)"; return 0; }
  [ -r /dev/tty ] || { say "$1 no (no terminal to ask on)"; return 1; }
  printf '%s [y/N] ' "$1"; read -r answer </dev/tty || return 1
  case $answer in y|Y|yes|YES) return 0 ;; *) return 1 ;; esac
}
[ "$(id -u)" -ne 0 ] || { echo "uninstall.sh: run it as yourself, not with sudo." >&2; exit 1; }
uid=$(id -u)

plist=$HOME/Library/LaunchAgents/ai.agent-hub.plist
if [ -f "$plist" ] && ask "Stop the hub and remove its launchd job (ai.agent-hub)?"; then
  launchctl bootout "gui/$uid/ai.agent-hub" 2>/dev/null || true
  rm -f "$plist"; say "removed: $plist"
fi

if command -v tmux >/dev/null 2>&1 && tmux -L agent-hub list-sessions >/dev/null 2>&1; then
  if ask "End every terminal the hub started (tmux -L agent-hub kill-server)? Running Claude sessions in them stop."; then
    tmux -L agent-hub kill-server || true
  fi
fi

jobs=$(ls "$HOME"/Library/LaunchAgents/ai.agent-memory.dream.*.plist 2>/dev/null || true)
if [ -n "$jobs" ] && ask "Turn off the daily dream checks ($(printf '%s\n' "$jobs" | wc -l | tr -d ' ') launchd jobs ai.agent-memory.dream.*)?"; then
  for job in $jobs; do
    label=$(basename "$job" .plist)
    launchctl bootout "gui/$uid/$label" 2>/dev/null || true
    rm -f "$job"; say "removed: $job"
  done
fi

if [ "$(readlink "$HOME/.local/bin/amk" 2>/dev/null)" = "$kit/bin/amk" ] && ask "Remove the link ~/.local/bin/amk?"; then
  rm -f "$HOME/.local/bin/amk"
fi

if command -v tailscale >/dev/null 2>&1 && grep -qs '"login"' "$HOME/.config/agent-hub/config.json"; then
  if ask "Stop serving the hub on your tailnet (tailscale serve --https=8443 off)?"; then
    tailscale serve --https=8443 off >/dev/null 2>&1 || true
  fi
fi

[ -d "$HOME/.config/agent-hub" ] && ask "Delete the hub's settings and key (~/.config/agent-hub)?" && rm -rf "$HOME/.config/agent-hub"
[ -d "$HOME/.local/state/agent-hub" ] && ask "Delete the hub's state: session list, kept transcripts, uploads (~/.local/state/agent-hub)?" && rm -rf "$HOME/.local/state/agent-hub"
[ -d "$HOME/.local/state/agent-memory" ] && ask "Delete the kit's state and session archive (~/.local/state/agent-memory)?" && rm -rf "$HOME/.local/state/agent-memory"

cat <<EOF

Kept, because they hold your work:
  - Your memory stores, ~/agent-memory/<store>: each is its own git repository. To delete one, first lift the lock on
    its human-tier files (chflags nouchg ~/agent-memory/<store>/ORG.md ~/agent-memory/<store>/STEERING.md), then
    remove the folder.
  - The kit's files in each repo you attached: the memory link, scripts/ (the files in agent-memory-kit/kit-files.txt),
    .claude/commands/dream.md, .claude/agents/, the kit's entries in .claude/settings.json, the protocol block in
    CLAUDE.md (between the agent-memory-kit:protocol markers), the post-checkout hook line marked agent-memory-kit,
    and the lines exclude-local.sh added to .git/info/exclude. Remove them by hand, or with git if you never
    committed them.
  - If you turned Claude's own auto memory off with retire-auto-memory.sh, turn it back on with
    sh $kit/retire-auto-memory.sh <store> --undo before you delete the store.
  - ~/.config/agent-memory (the Python on record for amk, and an oauth-token if you saved one there).
  - This clone. Delete it when you're done.
EOF

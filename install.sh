#!/bin/sh
# Dream Hub installer, for macOS with Claude Code. Safe to run again: what is already set up is kept.
#
#   sh install.sh                      check what's needed, set up the hub, offer to attach a first repo
#   sh install.sh --repo <path> [--store <name>]   also attach this repo to a memory store (named after it by default)
#
# Options:
#   --yes             answer yes to every question (for scripts; read them first)
#   --port <n>        the hub's loopback port (default 8491)
#   --tailscale       serve the hub on your tailnet with Tailscale Serve instead of this Mac only
#   --new-key         replace the hub's key (this-Mac-only mode), which signs out every browser
#   --no-launchd      don't offer the launchd job that keeps the hub running; you start it yourself
#   --schedule-dream  turn on the daily dream check for the store attached with --repo (it costs usage when it dreams)
#   --projects <dir>  the folder the hub lists repos from and creates new ones in (default ~/projects)
#
# What it writes, and only after asking: ~/.config/agent-hub/config.json, ~/Library/LaunchAgents/ai.agent-hub.plist,
# ~/.local/bin/amk (a link), the projects folder if it is missing, and, for --repo, the store in ~/agent-memory/<store>
# and the kit's files in that repo. It never uses sudo and never creates, reads or prints a Claude token.
# uninstall.sh takes it all out again.
set -eu

here=$(cd "$(dirname "$0")" && pwd -P)
kit=$here/agent-memory-kit
hub=$here/hub
port=; yes=; tailscale=; new_key=; launchd=1; schedule=; repo=; store=; projects=
while [ $# -gt 0 ]; do
  case $1 in
    --yes) yes=1 ;;
    --port) port=${2:?--port needs a number}; shift ;;
    --tailscale) tailscale=1 ;;
    --new-key) new_key=1 ;;
    --no-launchd) launchd= ;;
    --schedule-dream) schedule=1 ;;
    --repo) repo=${2:?--repo needs a folder}; shift ;;
    --store) store=${2:?--store needs a name}; shift ;;
    --projects) projects=${2:?--projects needs a folder}; shift ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "install.sh: unknown option $1 (see sh install.sh --help)" >&2; exit 2 ;;
  esac
  shift
done

say() { printf '%s\n' "$*"; }
step() { printf '\n== %s\n' "$*"; }
fail() { printf 'install.sh: %s\n' "$*" >&2; exit 1; }
ask() {  # ask "<question>": yes with --yes; otherwise asks on the terminal, and no when there is none
  [ -n "$yes" ] && { say "$1 yes (--yes)"; return 0; }
  [ -r /dev/tty ] || { say "$1 no (no terminal to ask on; use --yes)"; return 1; }
  printf '%s [y/N] ' "$1"; read -r answer </dev/tty || return 1
  case $answer in y|Y|yes|YES) return 0 ;; *) return 1 ;; esac
}

[ "$(id -u)" -ne 0 ] || fail "run it as yourself, not as root or with sudo."
for name in HUB_PORT HUB_STATE HUB_TMUX HUB_CONFIG HUB_KIT HUB_JOBS; do
  if env | grep -q "^$name="; then
    echo "$name is set, which describes a second hub instance (see hub/README.md). install.sh only installs the one hub, so unset the HUB_* variables first." >&2
    exit 2
  fi
done
port_given=$port
if [ -n "$port" ]; then
  case $port in *[!0-9]*) fail "--port must be a number from 1024 to 65535" ;; esac
  [ "$port" -ge 1024 ] && [ "$port" -le 65535 ] || fail "--port must be a number from 1024 to 65535"
fi

step "Checking what Dream Hub needs"
missing=
[ "$(uname -s)" = Darwin ] || fail "this is macOS-only today (it uses launchd, chflags and sips). See 'Platform support' in README.md for what a Linux port needs."
if /usr/bin/python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
  say "ok   python3 $(/usr/bin/python3 -c 'import platform; print(platform.python_version())') at /usr/bin/python3"
else
  say "MISSING  /usr/bin/python3 3.9 or newer. Install Apple's command line tools: xcode-select --install"; missing=1
fi
for tool in git tmux claude; do
  if command -v $tool >/dev/null 2>&1; then say "ok   $tool: $(command -v $tool)"
  else
    case $tool in
      git) say "MISSING  git: xcode-select --install" ;;
      tmux) say "MISSING  tmux: brew install tmux (or any tmux 3.2 or newer on your PATH)" ;;
      claude) say "MISSING  claude (Claude Code): see https://docs.claude.com/en/docs/claude-code" ;;
    esac
    missing=1
  fi
done
command -v jj >/dev/null 2>&1 && say "ok   jj (optional): $(command -v jj)" || say "--   jj not found (optional; only for jj repos)"
if [ -n "$tailscale" ]; then
  command -v tailscale >/dev/null 2>&1 && say "ok   tailscale: $(command -v tailscale)" || { say "MISSING  tailscale, which --tailscale needs"; missing=1; }
fi
[ -x "$kit/bin/amk" ] && [ -f "$hub/server.py" ] || fail "run this from a whole clone: $kit or $hub is missing."
[ -z "$missing" ] || fail "install what is missing above, then run sh install.sh again."

step "The hub's settings: ~/.config/agent-hub/config.json"
config_dir=$HOME/.config/agent-hub
config=$config_dir/config.json
projects=${projects:-$HOME/projects}
case $projects in /*) ;; *) projects=$(cd "$projects" 2>/dev/null && pwd) || fail "--projects: no such folder" ;; esac
json_get() { /usr/bin/python3 -c 'import json,sys; v=json.load(open(sys.argv[1])).get(sys.argv[2]); print("" if v is None else v)' "$config" "$1" 2>/dev/null || true; }
# merged '<json>': the config as it is, with these keys set (null removes one); other keys you added are kept.
merged() { /usr/bin/python3 -c '
import json, sys
try:
    now = json.load(open(sys.argv[1]))
except (OSError, ValueError):
    now = {}
for key, value in json.loads(sys.argv[2]).items():
    now.pop(key, None) if value is None else now.__setitem__(key, value)
print(json.dumps(now, indent=1))' "$config" "$1"; }
write_config=
[ -f "$config" ] && [ -z "$port_given" ] && port=$(json_get port)
port=${port:-8491}
if [ ! -f "$config" ]; then write_config=1
elif [ -n "$new_key" ] || { [ -n "$tailscale" ] && [ -z "$(json_get login)" ]; } || [ -n "$port_given" ]; then write_config=1
else say "kept: $config (sh install.sh --new-key replaces the key)"
fi
if [ -n "$write_config" ] && [ -z "$tailscale" ] && lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
  say "note: something already listens on port $port. If it isn't this hub, run again with --port <another>."
fi
if [ -n "$write_config" ]; then
  if [ -n "$tailscale" ]; then
    host=$(tailscale status --self --json | /usr/bin/python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')
    login=$(tailscale status --self --json | /usr/bin/python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["User"][str(d["Self"]["UserID"])]["LoginName"])')
    body=$(merged "{\"login\": \"$login\", \"origin\": \"https://$host:8443\", \"port\": $port, \"projects\": \"$projects\", \"key\": null}")
    say "The hub will answer only $login, through Tailscale Serve at https://$host:8443."
  else
    key=$(/usr/bin/python3 -c 'import secrets; print(secrets.token_hex(32))')
    body=$(merged "{\"login\": null, \"origin\": \"http://127.0.0.1:$port\", \"key\": \"$key\", \"port\": $port, \"projects\": \"$projects\"}")
    say "The hub will answer on this Mac only, at http://127.0.0.1:$port, to a browser that has its key."
  fi
  if ask "Write $config?"; then
    mkdir -p "$config_dir"; chmod 700 "$config_dir"
    old_umask=$(umask); umask 077; printf '%s\n' "$body" >"$config.new"; umask "$old_umask"
    mv "$config.new" "$config"; chmod 600 "$config"
    say "written: $config (mode 600)"
  else
    fail "the hub can't start without its settings. Nothing else was changed."
  fi
fi

mkdir -p "$HOME/agent-memory"  # where every store lives; the hub lists it even while it is empty
if [ ! -d "$projects" ]; then
  if ask "Create the projects folder $projects (the hub lists repos from it and makes new ones in it)?"; then mkdir -p "$projects"; else say "skipped: create $projects, or run again with --projects <folder>"; fi
fi

step "amk, the kit's command"
if [ "$(readlink "$HOME/.local/bin/amk" 2>/dev/null)" = "$kit/bin/amk" ]; then say "kept: ~/.local/bin/amk -> $kit/bin/amk"
elif [ -e "$HOME/.local/bin/amk" ]; then say "skipped: ~/.local/bin/amk exists and is something else; left as it is"
elif ask "Link ~/.local/bin/amk to $kit/bin/amk, so 'amk status' works from anywhere?"; then
  mkdir -p "$HOME/.local/bin"; ln -s "$kit/bin/amk" "$HOME/.local/bin/amk"; say "linked: ~/.local/bin/amk"
  case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) say "note: ~/.local/bin is not on your PATH; add it in your shell profile." ;; esac
fi

step "Keeping the hub running"
label=ai.agent-hub
plist=$HOME/Library/LaunchAgents/$label.plist
xml() { printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'; }
started=
if [ -n "$launchd" ] && ask "Install the launchd job $label, which starts the hub at login and restarts it if it stops?"; then
  mkdir -p "$HOME/Library/LaunchAgents" "$HOME/.local/state/agent-hub"
  new=$(mktemp "$HOME/.local/state/agent-hub/plist.XXXXXX")
  # The template: the hub runs from this clone, with the PATH of the shell that ran install.sh (where tmux and claude are).
  cat >"$new" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$label</string>
  <key>WorkingDirectory</key><string>$(xml "$hub")</string>
  <key>ProgramArguments</key><array><string>/usr/bin/python3</string><string>$(xml "$hub/server.py")</string></array>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>$(xml "$HOME/.local/bin:$PATH")</string>
    <key>HOME</key><string>$(xml "$HOME")</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>AbandonProcessGroup</key><true/>
  <key>StandardOutPath</key><string>$(xml "$HOME/.local/state/agent-hub/server.log")</string>
  <key>StandardErrorPath</key><string>$(xml "$HOME/.local/state/agent-hub/server.log")</string>
</dict></plist>
EOF
  plutil -lint "$new" >/dev/null || { rm -f "$new"; fail "the launchd job came out invalid; nothing was changed."; }
  chmod 644 "$new"
  launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
  mv "$new" "$plist"
  launchctl bootstrap "gui/$(id -u)" "$plist"
  say "on: $label (log: ~/.local/state/agent-hub/server.log)"; started=1
else
  say "Start the hub yourself when you want it:  /usr/bin/python3 $hub/server.py"
fi

if [ -n "$tailscale" ]; then
  if ask "Serve the hub on your tailnet (tailscale serve --bg --https=8443 http://127.0.0.1:$port)?"; then
    tailscale serve --bg --https=8443 "http://127.0.0.1:$port" >/dev/null
  fi
fi

if [ -n "$repo" ]; then
  step "A first memory store"
  repo=$(cd "$repo" 2>/dev/null && pwd) || fail "--repo: no such folder"
  git -C "$repo" rev-parse --git-dir >/dev/null 2>&1 || fail "$repo is not a git repository."
  store=${store:-$(basename "$repo" | tr 'A-Z_. ' 'a-z---')}
  case $store in [a-z0-9]*) ;; *) fail "--store: use lowercase letters, numbers and dashes" ;; esac
  say "The store $store at ~/agent-memory/$store, its own git repository, linked into $repo as memory/."
  say "In $repo this adds scripts/, .claude/ (commands, agents, settings.json), a block in CLAUDE.md, and a memory link"
  say "kept out of git; it commits nothing in your repo."
  if [ -n "$schedule" ]; then say "It also turns on the daily dream check (a launchd job), which starts a real Claude session when a dream is due."
  else say "The daily dream check stays off (run again with --schedule-dream, or turn it on in the hub)."; fi
  if ask "Attach $repo to the store $store?"; then
    if [ -n "$schedule" ]; then sh "$kit/install-store.sh" "$store" "$repo"
    else AGENT_KIT_NO_SCHEDULE=1 sh "$kit/install-store.sh" "$store" "$repo"; fi
  fi
fi

step "Done"
if [ -n "$(json_get key)" ] && [ -z "$(json_get login)" ]; then
  say "Open the hub once at this address; your browser then keeps the key in a cookie, and plain http://127.0.0.1:$port works:"
  say "  http://127.0.0.1:$port/?key=$(json_get key)"
  say "The key is in $config. Treat it like a password."
else
  say "Open the hub at $(json_get origin)"
fi
[ -n "$started" ] || say "(start it first: /usr/bin/python3 $hub/server.py)"
cat <<EOF

Next:
  - Unattended dreams need a sign-in that lasts: run 'claude setup-token' yourself and save what it prints in
    ~/.config/agent-memory/oauth-token (chmod 600). Nothing here creates or reads it for you otherwise.
  - Attach a repo:  sh $kit/install-store.sh <store> <repo>   (or 'New project' in the hub)
  - Check:          $kit/bin/amk status
  - Update:         sh $here/update.sh   (the hub also tells you when there is one)
  - Remove:         sh $here/uninstall.sh
EOF

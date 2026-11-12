#!/bin/sh
# A daily check through launchd, at the hour you choose: run-dream.sh --if-due starts a /dream only when
# scripts/dream_due.py says enough has happened since the last one. A dream only proposes; each one costs money.
#   sh agent-memory-kit/schedule-dream.sh <repo> [--hour 18]   turn it on (once per store)
#   sh agent-memory-kit/schedule-dream.sh <repo> --remove      turn it off
# Running it again replaces the job, also the weekly one an older kit set up. The new job is written
# and checked before the old one is taken out, so a mistake never leaves a store with no schedule.
# A store shared by several repos needs one schedule: the run covers every repo in roots.txt.
set -eu
repo=$(cd "$1" && pwd); shift
hour=18; remove=
while [ $# -gt 0 ]; do
  case $1 in
    --hour) [ $# -ge 2 ] || { echo "--hour needs a number from 0 to 23" >&2; exit 2; }; hour=$2; shift 2 ;;
    --remove) remove=1; shift ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done
case $hour in ''|*[!0-9]*|???*) echo "--hour is '$hour'; it has to be a whole number from 0 to 23" >&2; exit 2 ;; esac
hour=$(expr "$hour" + 0) || true  # 08 is 8; expr exits 1 when the answer is 0
[ "$hour" -le 23 ] || { echo "--hour is $hour; it has to be a whole number from 0 to 23" >&2; exit 2; }
store=$(basename "$(cd "$repo/memory" && pwd -P)")
label=ai.agent-memory.dream.$store
plist=$HOME/Library/LaunchAgents/$label.plist
if [ -n "$remove" ]; then
  launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
  rm -f "$plist"
  echo "removed $label"; exit 0
fi
# launchd has almost no PATH, so find claude now and pass its folder on.
claude=$(command -v claude)
kit=$(cd "$(dirname "$0")" && pwd)
state=$HOME/.local/state/agent-memory
mkdir -p "$(dirname "$plist")" "$state"
# A path may hold & or <, which would end the value or break the file.
xml() { printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'; }
new=$(mktemp "$state/$store.plist.XXXXXX")
cat >"$new" <<EOF_PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$(xml "$label")</string>
  <key>WorkingDirectory</key><string>$(xml "$repo")</string>
  <key>ProgramArguments</key><array>
    <string>/bin/sh</string><string>$(xml "$kit/run-dream.sh")</string><string>$(xml "$repo")</string><string>--if-due</string>
  </array>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>$(xml "$(dirname "$claude"):/usr/bin:/bin:/usr/sbin:/sbin")</string>
    <key>HOME</key><string>$(xml "$HOME")</string>
  </dict>
  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>$hour</integer><key>Minute</key><integer>0</integer></dict>
  <key>StandardOutPath</key><string>$(xml "$state/launchd-$store.log")</string>
  <key>StandardErrorPath</key><string>$(xml "$state/launchd-$store.log")</string>
</dict></plist>
EOF_PLIST
if ! problem=$(plutil -lint "$new" 2>&1); then
  rm -f "$new"
  echo "The new job for $store is not a valid plist, so nothing was changed: $problem" >&2
  exit 1
fi
chmod 644 "$new"
launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
mv "$new" "$plist"
launchctl bootstrap "gui/$(id -u)" "$plist"
echo "on: $label, checks every day at $hour:00 and dreams when one is due; the daily check logs to ~/.local/state/agent-memory/launchd-$store.log, a dream to ~/.local/state/agent-memory/dream-$store.log"

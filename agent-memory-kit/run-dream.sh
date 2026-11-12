#!/bin/sh
# One unattended /dream. launchd and Dream Hub both start it here, so a failure is never silent.
#   sh agent-memory-kit/run-dream.sh <repo>            dream now
#   sh agent-memory-kit/run-dream.sh <repo> --if-due   ask scripts/dream_due.py first; the daily job does this
# With --if-due and nothing due it stops there: no dream, no alert, exit 0. The check is recorded
# in ~/.local/state/agent-memory/<store>.json either way.
# No dream starts while the store has an open proposal (scripts/dream_proposal.py). The daily check
# says so in its line and reminds the person every few days; a start by hand names the proposal and
# how to apply or dismiss it, and exits 4.
# One dream at a time per store, by one rule (scripts/dream_owner.py). A daily check that finds a
# dream alive leaves quietly (exit 0); a start by hand says who is dreaming and since when, and
# exits 4. Neither launches claude, sends an alert or touches the other dream's log, marker or batch.
# That holds for another unattended run, which has the run lock, and for a person's /dream in a
# session, which owns the batch for as long as its session shows signs of life. If the dream the
# daily check itself started is still running a day later, it says so in an alert, once a day.
# A state file that can't be read or written stops either kind of start before claude is launched.
# After a dream the daily check started that wrote no proposal, it waits 3 days before the next
# (scripts/dream_try.py keeps the list). A start by hand is not held back and is not on the list.
# The dream reads from where the last one stopped: the collector works that out, nothing is passed.
# It can only propose: memwrite refuses every write during the run.
# Claude Code's own auto memory is off for the run, so the dream neither loads those notes as
# its own memory nor adds to them; it reads them as data from the batch.
# For a login that outlasts a session, put the output of `claude setup-token` in
# ~/.config/agent-memory/oauth-token (chmod 600).
# A repo with its copy of the kit (scripts/dream_try.py) runs that copy, through its memory/ link, as
# always. A repo with none (attached by its agent-memory.store key alone) runs the kit's own scripts,
# with AGENT_MEMORY_DIR set to its store and the kit's bin/ first on the PATH: the dream gets the kit's
# instructions spelled for `amk`, its analysts, its hooks and the allow rules for those commands from
# scripts/dream_try.py (prompt, agents, hooks, rules) in place of /dream and the repo's settings.
if_due=
case $# in
  1) ;;
  2) [ "$2" = --if-due ] || { echo "unknown option: $2" >&2; exit 2; }; if_due=1 ;;
  *) echo "usage: run-dream.sh <repo> [--if-due]" >&2; exit 2 ;;
esac
cd "$1" || exit 2
repo=$(pwd)
kit=$(cd "$(dirname "$0")" && pwd -P)
if [ -f scripts/dream_try.py ]; then
  mem=memory; try() { python3 scripts/dream_try.py "$@"; }
else
  mem=$("$kit/bin/amk" where) || exit 2
  AGENT_MEMORY_DIR=$mem AGENT_MEMORY_FROM_KIT=$kit PATH=$kit/bin:$PATH; export AGENT_MEMORY_DIR AGENT_MEMORY_FROM_KIT PATH
  try() { python3 "$kit/scripts/dream_try.py" "$@"; }
fi
store=$(basename "$(cd "$mem" && pwd -P)")
state=$HOME/.local/state/agent-memory
mkdir -p "$state"
log=$state/dream-$store.log
# The lock belongs to this open file, so it lasts until this script ends, however it ends.
exec 9>>"$state/$store.run.lock"
other=$(try lock $$)
case $? in
  0) ;;
  7) if [ -n "$if_due" ]; then
       echo "$other Nothing was started."
       long=$(try held 2>/dev/null)
       [ -n "$long" ] && [ -x "$mem/notify.sh" ] && "$mem/notify.sh" "Dream still running: $store" "$long"
       exit 0
     fi
     echo "$other Nothing was started." >&2; exit 4 ;;
  *) echo "couldn't take the lock $state/$store.run.lock, so nothing was started" >&2; exit 1 ;;
esac
# Everything started from here on is this run: it holds the lock, so no other unattended run is alive.
AGENT_MEMORY_DREAM_RUN="$$-$(date +%s)"; export AGENT_MEMORY_DREAM_RUN
tried=
fail() {
  next=
  [ -n "$tried" ] && next=$(try failed 2>/dev/null)
  [ -x "$mem/notify.sh" ] && "$mem/notify.sh" "Dream failed: $store" "$1${next:+ $next}"
  echo "dream failed: see $log" >&2
  exit 1
}
reason="started by hand"
# What the scripts say on stderr is kept apart, so a warning can never become part of the reason.
errors=$(mktemp "$state/$store.err.XXXXXX")
broke() { { date; echo "$answer"; cat "$errors"; echo "$1 exit: $asked"; } >"$log"; said=$(tail -1 "$errors"); rm -f "$errors"; }
if [ -z "$if_due" ]; then
  # The daily check reads and writes the state file before it starts anything. A start by hand asks the same first.
  answer=$(try ready 2>"$errors"); asked=$?
  if [ $asked -ne 0 ]; then broke "dream_try.py ready"; fail "No dream was started. $said"; fi
  # A person's /dream that is alive in a session owns the batch. The daily check is told so by dream_due.py; a start by hand asks here.
  answer=$(try running 2>"$errors"); asked=$?
  if [ $asked -eq 4 ]; then rm -f "$errors"; echo "$answer Nothing was started." >&2; exit 4; fi
  if [ $asked -ne 0 ]; then broke "dream_try.py running"; fail "No dream was started. $said"; fi
  # One open proposal per store: a dream now would push it, and the ticks in it, out of sight. The collector would stop on it anyway.
  answer=$(try open 2>"$errors"); asked=$?
  if [ $asked -eq 4 ]; then rm -f "$errors"; echo "$answer Nothing was started." >&2; exit 4; fi
  if [ $asked -ne 0 ]; then broke "dream_try.py open"; fail "No dream was started. $said"; fi
else
  answer=$(try due 2>"$errors"); asked=$?
  if [ $asked -eq 3 ]; then rm -f "$errors"; echo "$answer"; exit 0; fi  # the last dream's log stays as it is
  if [ $asked -ne 0 ]; then broke dream_due.py; fail "The check for whether a dream is due didn't work, so no dream was started. $said"; fi
  reason=${answer#due: }
  # Recorded before the launch, so a run that is killed still counts. If it can't be recorded, nothing is launched.
  answer=$(try start 2>"$errors"); asked=$?
  if [ $asked -ne 0 ]; then broke "dream_try.py start"; fail "Couldn't record that a dream is starting, so none was started. $said"; fi
  tried=1
fi
rm -f "$errors"
token=$HOME/.config/agent-memory/oauth-token
[ -r "$token" ] && CLAUDE_CODE_OAUTH_TOKEN=$(cat "$token") && export CLAUDE_CODE_OAUTH_TOKEN
claude=$(command -v claude || echo "$HOME/.local/bin/claude")
# The run may write two kinds of file with its file tools, its proposal and the file beside it that
# holds what it read and did not propose, and run the scripts the repo's settings allow. Anything else
# is refused without asking. Both spellings of each path are needed: memory/ is a link, and a rule
# must fit the path as asked for and as it resolves.
set -- -p /dream --permission-mode dontAsk --allowedTools "Edit(/memory/dreams/*-proposal.md)" "Edit(~/agent-memory/$store/dreams/*-proposal.md)" \
  "Edit(/memory/dreams/*-notes.md)" "Edit(~/agent-memory/$store/dreams/*-notes.md)"
if [ "$mem" != memory ]; then  # no copy here: what /dream and the repo's settings give a copy, from the kit's own files
  given=$(mktemp -d "$state/$store.given.XXXXXX")
  try prompt >"$given/prompt" && try agents >"$given/agents.json" && try rules >"$given/rules" && try hooks >"$given/hooks.json" \
    || { rm -rf "$given"; fail "The dream's instructions could not be made from the kit, so no dream was started."; }
  shift 2; set -- -p "$(cat "$given/prompt")" "$@"
  while IFS= read -r rule; do set -- "$@" "$rule"; done <"$given/rules"
  set -- "$@" --agents "$given/agents.json" --settings "$given/hooks.json" --add-dir "$mem"
fi
while IFS= read -r other; do
  [ -n "$other" ] && [ "$other" != "$repo" ] && set -- "$@" --add-dir "$other"
done <"$mem/roots.txt"
rm -f "$state/$store.finished"  # dream_finish.py writes this run's name into it; only that counts as done
# The collector and dream_finish.py read the reason: a dream the daily check started may stop when there
# is too little, and gets its ending recorded; one started by hand goes on and is not on that list.
export AGENT_MEMORY_PROPOSE_ONLY=1 AGENT_MEMORY_DREAM_REASON="$reason" CLAUDE_CODE_DISABLE_AUTO_MEMORY=1
# Claude doesn't get the lock's file: something it leaves running must not hold the lock after this run.
{ date; "$claude" "$@" 9>&-; echo "exit: $?"; } >"$log" 2>&1
[ -n "${given:-}" ] && rm -rf "$given"
if [ "$(cat "$state/$store.finished" 2>/dev/null)" != "$AGENT_MEMORY_DREAM_RUN" ]; then
  why="It didn't finish. Log: $log"
  grep -q "has not been trusted" "$log" && why="Open this project in Claude once and accept the trust prompt."
  grep -Eqi "not logged in|please run /login|oauth token|authentication_error|invalid api key" "$log" && why="Claude's sign-in has run out. Run claude setup-token."
  # dream_finish.py says exactly what to do when the proposal is in but the state file is not.
  grep -q "is committed, but the state file could not be updated" "$log" && why=$(grep -A1 "is committed, but the state file could not be updated" "$log" | tr '\n' ' ')
  # proposal_check.py gives a dream two lists for how its proposal is written, then says it has failed; the dream repeats that and stops.
  grep -q "times for how it is written and is still not written as a proposal" "$log" && why="The dream wrote a proposal that was given back twice for how it is written and was still not one a person can tick, so it stopped. Nothing was committed, the batch is kept and the next dream reads the same sessions. Log: $log"
  fail "$why"
fi

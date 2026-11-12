#!/bin/sh
# What sessions load and what a dream collects, on a fixture in the older layout of the kit:
# the kit's files copied into each repo, a memory/ link, no agent-memory.store key, no STEERING.md. Two stores: one
# with two repos that share a real Steering block (and a worktree of one), one with a repo that has the kit's default.
#   sh tests/live-layout.sh <kit> <empty folder>
# Prints one line per output, `<sha256>  <what>`, with the folder's path taken out, so the same kit prints the same lines
# wherever it runs. tests/live-layout.before is what the kit printed when this layout was released: the suite holds this kit to it.
# Everything happens under the folder, which is HOME for the run; nothing else is touched.
KIT=$1; D=$2
export HOME=$D TZ=UTC AGENT_KIT_NO_SCHEDULE=1 CLAUDE_CODE_SESSION_ID=fixture-session
export GIT_AUTHOR_DATE=2026-11-01T12:00:00Z GIT_COMMITTER_DATE=2026-11-01T12:00:00Z
unset AGENT_MEMORY_DIR AGENT_MEMORY_FROM_KIT AGENT_MEMORY_PROPOSE_ONLY AGENT_MEMORY_DREAM_REASON AGENT_MEMORY_DREAM_RUN
git config --global user.name test; git config --global user.email t@t; git config --global init.defaultBranch main
P=$D/projects; mkdir -p $P/webshop $P/shop-ios $P/solo
for r in webshop shop-ios solo; do (cd $P/$r && git init -q && printf '# %s\n' $r > CLAUDE.md && git add -A && git commit -qm init); done
sh $KIT/install-store.sh shop $P/webshop $P/shop-ios > $D/install.log 2>&1 || { cat $D/install.log >&2; exit 1; }
sh $KIT/install-store.sh solo $P/solo >> $D/install.log 2>&1 || { cat $D/install.log >&2; exit 1; }
STEER='- Matters here:
  - Anything that could expose a customer: personal data reaching a third party, keys outside `~/.config/`.
  - Server and app disagreeing: a field one side reads and the other does not send.
- Privacy: transcripts can hold personal details. Proposals quote none of them.
- Doesn'"'"'t matter:
  - Formatting nits the build already catches.'
for r in webshop shop-ios; do
  python3 - "$P/$r/.claude/commands/dream.md" "$STEER" <<'PY'
import sys
path, steer = sys.argv[1:3]
head = open(path).read().split("\n## Steering", 1)[0]
open(path, "w").write(head + "\n## Steering (edit per project)\n" + steer + "\n")
PY
done
H=$D/agent-memory/shop
chflags nouchg $H/ORG.md
printf '# ORG\n\n- Product: an online shop and its app.\n- Never: commit secrets.\n' > $H/ORG.md
(cd $P/webshop && python3 scripts/memcheck.py --seal > /dev/null; chflags uchg $H/ORG.md)
W() { (cd $P/webshop && python3 scripts/memwrite.py "$@"); }
for line in '- [2026-11-02] The agenda goes stale when Calendar access is lost → re-grant it in Privacy settings (checked: sess-a)' \
            '- [2026-11-03] A test that writes into data/ breaks the nightly snapshot → write into a temporary folder (checked: sess-b)'; do
  printf '%s\n' "$line" | W write project/gotchas.md --expect "$(W read project/gotchas.md | sed -n 's/^HASH: //p')" --agent fixture --why seed --append > /dev/null
done
printf '%s\n' '- [2026-11-04] Decision: the app reads the server, never the files. Why: one source. (by Sam)' \
  | W write project/decisions.md --expect "$(W read project/decisions.md | sed -n 's/^HASH: //p')" --agent fixture --why seed --append > /dev/null
python3 - $H/CHANGELOG.md <<'PY'
import re, sys
path = sys.argv[1]
open(path, "w").write(re.sub(r"^\| \d{4}-\d\d-\d\dT[\d:]+ \|", "| 2026-11-04T12:00:00 |", open(path).read(), flags=re.M))
PY
git -C $H commit -qam "fixture: fixed times in the log"
(cd $P/shop-ios && git add -A && git commit -qm "the kit, committed" && git worktree add -q -b wt $P/shop-ios-wt 2>/dev/null)
# Sessions: two in webshop, one in shop-ios, one in its worktree, each with the person's words and an error.
mkdir -p $D/.claude/projects/fixture
python3 - "$P" "$D/.claude/projects/fixture" <<'PY'
import datetime, json, os, sys
P, out = sys.argv[1:3]
for name, repo, when in (("s-dash-1", "webshop", "2026-11-05T09:00:00"), ("s-dash-2", "webshop", "2026-11-06T09:00:00"),
                         ("s-ios-1", "shop-ios", "2026-11-06T10:00:00"), ("s-wt-1", "shop-ios-wt", "2026-11-07T10:00:00")):
    path = f"{out}/{name}.jsonl"
    with open(path, "w") as f:
        rows = [("user", f"fix the agenda in {repo}, it is stale again"),
                ("assistant", [{"type": "text", "text": "Looking at the refresh job."}, {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls data"}}]),
                ("user", [{"type": "tool_result", "tool_use_id": "t1", "is_error": True, "content": "ls: data: No such file or directory"}]),
                ("assistant", [{"type": "text", "text": "The folder is elsewhere; the job reads it from the settings."}]),
                ("user", "Decision: the refresh job never writes outside data/")]
        for at, (kind, said) in enumerate(rows):
            stamp = (datetime.datetime.fromisoformat(when) + datetime.timedelta(minutes=at)).isoformat() + "Z"
            f.write(json.dumps({"type": kind, "cwd": f"{P}/{repo}", "sessionId": name, "timestamp": stamp, "message": {"role": kind, "id": f"{name}-{at}", "content": said}}) + "\n")
    end = datetime.datetime.fromisoformat(when + "+00:00").timestamp() + 600
    os.utime(path, (end, end))
PY
NOW=$(python3 -c 'import datetime; print(int(datetime.datetime.fromisoformat("2026-11-08T18:00:00+00:00").timestamp()))')
out() { sed -E -e "s#$D#<HOME>#g" -e 's/"head": "[0-9a-f]{40}"/"head": "<the store'"'"'s commit, which holds the log'"'"'s times>"/' | shasum -a 256 | cut -c1-64; }
for r in webshop shop-ios shop-ios-wt solo; do
  for event in '{"hook_event_name":"SessionStart"}' '{"hook_event_name":"SubagentStart","agent_type":"Explore"}'; do
    printf '%s  loads: %s %s\n' "$(cd $P/$r && echo "$event" | python3 scripts/memory_context.py | out)" $r "$(echo "$event" | sed 's/.*"hook_event_name":"\([A-Za-z]*\)".*/\1/')"
  done
done
(cd $P/webshop && AGENT_MEMORY_NOW=$NOW python3 scripts/collect_transcripts.py) > $D/collect.out 2>&1 || { cat $D/collect.out >&2; exit 1; }
printf '%s  collector output\n' "$(out < $D/collect.out)"
for f in $(cd $H/dreams/_batch && find . -type f | sort); do
  # _groups.md gives each group's size in characters, which counts the paths in the files: the numbers depend on where the folder is
  case $f in ./_groups.md) printf '%s  batch: %s (its numbers taken out)\n' "$(sed -E 's/[0-9][0-9,]*/N/g' $H/dreams/_batch/$f | out)" "$f" ;;
             *) printf '%s  batch: %s\n' "$(out < $H/dreams/_batch/$f)" "$f" ;; esac
done

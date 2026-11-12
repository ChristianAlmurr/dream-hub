#!/usr/bin/env python3
"""For run-dream.sh only: what it needs from the store's state file around a run.

  python3 scripts/dream_try.py lock [pid]  takes the run lock on the file run-dream.sh opened (fd 9) and writes that process number into it; if another run has it, says who and since when, exit 7
  python3 scripts/dream_try.py running  says which dream is alive on the store, if one is that is not this run's, exit 4
  python3 scripts/dream_try.py open     says which proposal is still open on the store, if one is, exit 4
  python3 scripts/dream_try.py due      the daily check: dream_due.py's line and exit code, and a reminder through notify.sh when an open proposal holds a due dream back
  python3 scripts/dream_try.py ready    stops with the reason unless the state file can be read, makes sense and can be written
  python3 scripts/dream_try.py start    the daily check is about to start a dream
  python3 scripts/dream_try.py failed   that dream never reached dream_finish.py; prints when the next try is
  python3 scripts/dream_try.py held     a dream is running; prints the words for an alert if the daily check's own has run too long
  python3 scripts/dream_try.py prompt|agents|rules|hooks   for a repo with no copy of the kit, from the kit's own copy (see below)

After a try that wrote no proposal the daily check waits before it starts another (dream_due.py).
These write that list, so they are apart from dream_due.py, which agents may run: no permission
rule allows this file, and an agent that asks for it is asked back.

A repo with no copy of the kit has no /dream, no analysts and no rules or hooks of the kit in its
settings. For it, run-dream.sh gives the dream what a copy would, from the kit's own files, run with
the working directory in the repo: `prompt`, the propose part of .claude/commands/dream.md with every
command spelled as the amk verb that runs it (verbs.spelled()) and `memory/` as the store's path;
`agents`, the two analysts for --agents; `rules`, the allow rules of settings.snippet.json for those
commands, spelled the same way (verbs.amk_rules()); `hooks`, the start and Bash hooks for --settings.
"""
import json, os, re, shlex, subprocess, sys
from pathlib import Path

import dream_due
import verbs

KIT = Path(__file__).resolve().parent.parent
LINK = re.compile(r"(?<![\w./-])memory/|(?<=-C )memory(?=[\s)`]|$)")  # the store, as a repo's copy reaches it through its link: `memory/…`, and `git -C memory …`


def for_kit(text: str) -> str:
    """Text written for a repo's copy, said for this run: commands as amk verbs, `memory/` and `-C memory` as the store's path."""
    return LINK.sub(lambda found: f"{dream_due.MEM}" + ("/" if found[0].endswith("/") else ""), verbs.spelled(text))


def rules() -> list:
    """The copy's allow rules for this run: its scripts as amk verbs, its other commands (`git -C memory log *`) with the store's path."""
    allow = json.loads((KIT / "settings.snippet.json").read_text())["permissions"]["allow"]
    return verbs.amk_rules(allow) + [for_kit(rule) for rule in allow if rule.startswith("Bash(") and "python3 scripts/" not in rule]


def prompt() -> str:
    """The unattended dream's instructions: what `/dream` with no argument gives a repo that has a copy, for this run."""
    text = (KIT / ".claude" / "commands" / "dream.md").read_text().split("---\n", 2)[2]
    text = text.split("\n## Steering", 1)[0].replace("$ARGUMENTS", "")  # the kit's own Steering block is no repo's: Steering is the store's STEERING.md
    # An unattended dream only proposes, and this repo has no block of its own and no .claude/dream-apply.md: the parts that say otherwise go.
    head, apply_part = text.split("## If the argument is `apply`", 1)
    text = head + "## Otherwise (propose)." + apply_part.split("## Otherwise (propose).", 1)[1]
    text = text.replace("; else this repo's block at the end of this file.", ". Without it, this repo has no Steering.")
    pairs = sorted({rule[5:-1].split(" *")[0] for rule in rules()})
    return (f"This dream is for {Path.cwd()}, which has no copy of the memory kit, so /dream is not a command here and these are its instructions. "
            f"`memory/` in them, and in every file you read, is the store {dream_due.MEM}. The kit's commands are run as `amk` verbs: "
            f"where a file you read gives one as `python3 scripts/<name>.py …`, run the verb for that script, as these instructions spell it. "
            f"The verbs you may run: {', '.join(f'`{pair}`' for pair in pairs)}.\n\n" + for_kit(text).strip("\n") + "\n")


def agents() -> dict:
    """The analysts the dream starts, from the kit's .claude/agents/, as --agents takes them."""
    found = {}
    for path in sorted((KIT / ".claude" / "agents").glob("*.md")):
        _, front, body = path.read_text().split("---\n", 2)
        fields = dict(line.split(": ", 1) for line in front.strip().split("\n"))
        found[fields["name"]] = {"description": fields["description"], "prompt": for_kit(body).strip("\n"),
                                 "tools": [tool.strip() for tool in fields["tools"].split(",")], "model": fields["model"]}
    return found


def hooks() -> dict:
    """The hooks a copy's settings carry, run from the kit: the start note, and the Bash guard."""
    run = lambda script: [{"type": "command", "command": "python3 " + shlex.quote(str(KIT / "scripts" / script))}]
    return {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": run("guard_memory.py")}],
                      "SessionStart": [{"hooks": run("memory_context.py")}], "SubagentStart": [{"hooks": run("memory_context.py")}]}}

def due() -> int:
    found = dream_due.check(remind=True)
    print(found["line"])
    notify = dream_due.MEM / "notify.sh"
    if found.get("remind") and found.get("waiting"):  # both due today: one message that says both
        found["remind"] += " Also: " + found.pop("waiting")
    for key, title in (("remind", "Proposal waiting"), ("hung", "Dream still running"), ("waiting", found.get("waiting_title"))):
        if found.get(key) and os.access(notify, os.X_OK):
            sent = subprocess.run([str(notify), f"{title}: {dream_due.MEM.name}", found[key]], capture_output=True, text=True)
            if sent.returncode != 0:
                print(f"WARN: the alert could not be sent: {sent.stderr.strip()[:200]}", file=sys.stderr)
    return 0 if found["due"] else dream_due.NOT_DUE


if __name__ == "__main__":
    if sys.argv[1:2] == ["lock"] and len(sys.argv) <= 3:
        sys.exit(dream_due.take_run_lock(*sys.argv[2:]))
    elif sys.argv[1:] == ["running"]:
        sys.exit(dream_due.turned_away())
    elif sys.argv[1:] == ["open"]:
        sys.exit(dream_due.proposal_waiting())
    elif sys.argv[1:] == ["due"]:
        sys.exit(due())
    elif sys.argv[1:] == ["ready"]:
        dream_due.ready()
    elif sys.argv[1:] == ["start"]:
        dream_due.try_start()
    elif sys.argv[1:] == ["failed"]:
        print(dream_due.try_failed())
    elif sys.argv[1:] == ["held"]:
        print(dream_due.try_held())
    elif sys.argv[1:] == ["prompt"]:
        print(prompt(), end="")
    elif sys.argv[1:] == ["agents"]:
        print(json.dumps(agents()))
    elif sys.argv[1:] == ["rules"]:
        print("\n".join(rules()))
    elif sys.argv[1:] == ["hooks"]:
        print(json.dumps(hooks()))
    else:
        sys.exit("usage: dream_try.py lock | running | open | due | ready | start | failed | held | prompt | agents | rules | hooks")

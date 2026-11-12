#!/usr/bin/env python3
"""The verbs of bin/amk: one table for the two that have to agree on it.

scripts/amk.py runs a verb's script. guard_memory.py holds every spelling of a command that runs
memwrite.py to the same rules about how memory text reaches it, and `amk write` is such a spelling:
it takes the verbs that run memwrite.py from here, so a verb can't be added without the guard knowing.

It loads nothing of the kit and binds no store, so the guard's copy in a repo and amk, which must
load no script before the store is known, can both have it.

as_amk() is the one map from how a repo's copy of the kit spells a command (`python3 scripts/<name>.py …`)
to the amk verb that runs the same thing. run-dream.sh uses it for a repo with no copy of the kit: the
dream's instructions and its allow rules are the copy's, spelled for amk.
"""
import re

# verb: (script, arguments put before the caller's, whether it needs this folder's store, what it does)
VERBS = {
    "read": ("memwrite.py", ["read"], True, "print a memory file, then its HASH: line"),
    "write": ("memwrite.py", ["write"], True, "write a memory file from stdin (--expect <hash> --agent <name> --why <line> [--append])"),
    "edit": ("memwrite.py", ["edit"], True, "change one line: stdin is the line as it is, then the line as it should be"),
    "delete": ("memwrite.py", ["delete"], True, "take one line out: stdin is the line as it is"),
    "undo": ("memwrite.py", ["undo"], True, "take back one edit or delete (--row <its line in CHANGELOG.md>)"),
    "past": ("past.py", [], True, "search this project's past sessions for words"),
    "check": ("memcheck.py", [], True, "is the store intact? (--seal commits scratchpads, the inbox and hand edits to ORG.md first)"),
    "due": ("dream_due.py", [], True, "is a dream due? exit 0 when it is, 3 when not (--json for the counts)"),
    "proposal-check": ("proposal_check.py", [], True, "does the proposal being written hold anything the store refuses?"),
    "proposal": ("dream_proposal.py", [], True, "the store's open proposal, the only one that can be applied, or that there is none"),
    "applied": ("dream_proposal.py", ["applied"], True, "the last step of /dream apply: the open proposal is applied (the numbers of ticked items that could not be)"),
    "declined": ("dream_proposal.py", ["declined"], True, "the items the person has declined, for one project/ file or for all"),
    "alive": ("dream_owner.py", ["alive"], True, "for a dream between its steps: this dream is still at work"),
    "finish": ("dream_finish.py", [], True, "a dream's last step: record, clear the batch, commit, alert (--none when nothing was proposed)"),
    "archive": ("archive.py", [], True, "bring the archive of past sessions up to date"),
    "own-notes": ("own_notes.py", [], True, "Claude Code's own notes for each repository of the store"),
    "collect": ("collect_transcripts.py", [], True, "a dream's first step: what happened since the last dream, into the store's batch (--days N)"),
    "machine-check": ("machine_check.py", [], True, "a dream's checks of facts about this machine, the numbered claims in its side file"),
    "apply": ("dream_apply.py", [], True, "/dream apply: the ticked items of the open proposal (--dry-run, --done <item>); --yours is the person's"),
    "look": ("repo_look.py", [], True, "read a git history of the store's repos, and only read it: amk look <repo> <log|show|diff|grep|…> [as for git]"),
    "steering": ("steering.py", [], True, "amk steering init: the repositories' Steering blocks become the store's STEERING.md (the person's to run)"),
    "status": ("status.py", [], False, "every store at a glance; `amk status <store>` for one in full; --json for programs"),
    "where": ("store.py", ["where"], False, "the store this folder belongs to, or that it has none"),
    "attach": ("store.py", ["attach"], False, "amk attach <store> <repo>: make a repository one of the store's (the person's to run)"),
    "detach": ("store.py", ["detach"], False, "amk detach <store> <repo>: take it out again (the person's to run)"),
    "interpreter": ("interpreter.py", [], False, "the Python amk runs under and the one on record; `amk interpreter record [python3]` is the person's"),
}
# Verbs that take nothing after them. What followed would reach the script as another of its commands, and those
# (dream_proposal.py dismiss and forget, dream_owner.py release) are the person's: they have no verb.
ALONE = {"proposal", "alive", "machine-check"}  # machine_check.py takes nothing either: its claims come from the dream's side file, never a command line
# The commands that are the person's, by script and the word that asks for them, with what each does. No verb is the
# way an agent runs one: guard_memory.py stops every spelling it can read, with or without amk in front, and each
# script refuses inside an unattended dream.
PERSONS = {
    ("store.py", "attach"): "which store a repository belongs to",
    ("store.py", "detach"): "which store a repository belongs to",
    ("interpreter.py", "record"): "which Python every amk command and hook runs under",
    ("dream_proposal.py", "dismiss"): "closing a proposal without applying it",
    ("dream_proposal.py", "forget"): "taking an item off the declined list",
    ("dream_proposal.py", "withdraw"): "taking back an item that was approved",
    ("dream_proposal.py", "done"): "closing what a dream said needs the person",
    ("own_notes.py", "reread"): "having Claude's own notes read again by the next dream",
    ("dream_owner.py", "release"): "giving up the dream that holds a store",
    ("dream_apply.py", "--yours"): "the ticked changes to ORG.md and Steering",
    ("steering.py", "init"): "where a store's Steering is kept",
}


def behind(verb: str, rest: list):
    """(the script an amk verb runs, the words it is given), or None for a word that is no verb."""
    if verb in VERBS:
        return VERBS[verb][0], VERBS[verb][1] + rest
    return None


def persons(script: str, words: list) -> str:
    """What a command is about when it is one of the person's, else empty: its first word, or an option anywhere, is in PERSONS."""
    words = [word for word in words if word != "--"]  # argparse takes a bare `--` and reads the next word as the command
    asked = words[:1] + [word for word in words[1:] if word.startswith("--")]
    return next((PERSONS[script, word] for word in asked if (script, word) in PERSONS), "")


def running(script: str) -> list:
    """The verbs that run this script."""
    return [verb for verb, (runs, _, _, _) in VERBS.items() if runs == script]


def as_amk(script: str, words: list):
    """(the amk verb, the words after it) for `python3 scripts/<script> <words>`, or None when no verb runs that command or it is the person's."""
    if persons(script, words):
        return None
    for verb, (runs, first, _, _) in sorted(VERBS.items(), key=lambda pair: -len(pair[1][1])):
        if runs == script and words[:len(first)] == first and not (verb in ALONE and words[len(first):]):
            return verb, words[len(first):]
    return None


COPY = re.compile(r"python3 scripts/(\w+\.py)((?: [\w*-]+)?)")  # how a repo's copy is run: the script, and the word after it that may choose the verb


def spelled(text: str) -> str:
    """The text with every command a repo's copy of the kit would run spelled as the amk verb that runs it. The person's commands stay as they are."""
    def one(match):
        words = match[2].split()
        found = as_amk(match[1], words)
        if not found and not persons(match[1], words):  # the word after it is an argument of a verb that takes no word of its own
            bare = as_amk(match[1], [])
            found = bare and bare[0] not in ALONE and (bare[0], words)
        return " ".join(["amk", found[0], *found[1]]) if found else match[0]
    return COPY.sub(one, text)


def amk_rules(rules: list) -> list:
    """A copy's allow rules for `python3 scripts/…`, as the rules for the same commands spelled for amk. A rule for every command of a script (`<script> *`) becomes one for each of its verbs."""
    out = []
    for rule in rules:
        match = re.fullmatch(r"Bash\(python3 scripts/(\w+\.py)((?: \S+)*)\)", rule)
        if not match:
            continue
        words = match[2].split()
        found = as_amk(match[1], words)
        if found:
            out.append("Bash(" + " ".join(["amk", found[0], *found[1]]) + ")")
        elif words == ["*"]:
            out += [f"Bash(amk {verb} *)" for verb in running(match[1]) if verb not in ALONE and not persons(match[1], VERBS[verb][1])]
    return list(dict.fromkeys(out))

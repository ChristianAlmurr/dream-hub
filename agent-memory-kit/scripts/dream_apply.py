#!/usr/bin/env python3
"""Applies the ticked items of a dream proposal, one item at a time, and records what became of each.

  python3 scripts/dream_apply.py                    apply every ticked item that isn't applied yet
  python3 scripts/dream_apply.py --dry-run          print what each ticked item would do; write nothing
  (with no proposal open, the first three list what earlier proposals left waiting for a session, with the proposal each came from)
  python3 scripts/dream_apply.py --done <item> ...  the apply session did one of the items listed as "needs a session" (--of <proposal> when its number waits in two)
  python3 scripts/dream_apply.py --yours            the person's pass: the ticked ORG.md and Steering items that wait, of every proposal that has any

Applying a tick is text work, so a script does it and a model session is left only what needs
judgement. Every change goes through memwrite's own functions: the same checks as a direct write
(reject.txt, credentials, injected instructions, the tiers), one log row and one commit per
change, under the agent name `dreamer` and the session `dream-<date>`. Nothing here uses --force
or a whole-file replace, so the script can do nothing an ordinary agent's memwrite call can't.
It works on the store's open proposal (dream_proposal.py says which) and writes only that store.
It never closes a proposal: that is `python3 scripts/dream_proposal.py applied`, the last step of
/dream apply, which reads the record written here.

What each kind of ticked item becomes:
  add | edit | delete | merge | split on a file in project/ (agents/, inbox/): applied here.
      An item names where the file should end up, so the steps come from what the file holds
      now. `already applied` needs evidence: the line to add is there; or the log's last word on
      the line to delete is that it left; or, for an edit, the new line is there and the old one
      is out by the log or has nothing like it left. A line to delete that is not there with no
      such row is `refused` (not found), with the closest lines; so is one that fits several
      lines or that the store refuses. Nothing is guessed.
  Inbox item, reject: the inbox file is removed and committed here.
  A new skill, a scratchpad to condense, an accepted inbox item: `needs a session`. They are
      listed with their ids; the session does them and reports each with --done.
  carry (`notes <note file> — carry — <n> checked facts`): the checked facts of one of Claude's own
      notes, approved with one tick. Each line under it, `+ project/<file>.md "<line>"`, is one
      append, as an `add` would make it: its own checked write, log row and commit. A line the person
      turned to `-` is left out. The item's row holds `parts`, one per fact: {"sign", "file", "line",
      "result", "reason", "commits"}, with `result` one of applied, already applied, refused, left out.
      The item is `refused` while a fact it takes is; a second run writes only what is still missing,
      and a fact that was left out is written by a later run once its `-` is turned back to `+`.
  A ticked line that is not written as an item: `refused`, naming the line. Nothing ticked vanishes.
  A ticked line under "Needs you": `refused`. That section holds what the person has to do, not memory.
  ORG.md and Steering: `yours`. See "The person's tier" below.

The person's tier. A tick sits in a file agents can write, so it proves nothing about who ticked.
The kit has one mechanism that marks a person: the kernel's immutable flag on ORG.md (uchg), which
install-store.sh sets and which agents are kept from lifting. This script never lifts it. --yours
refuses while the flag is set, and without a terminal: the person lifts it (`chflags nouchg <store>/ORG.md`) and runs
--yours, which reads the proposal once, shows every change with its file and the line it goes
under, asks, applies exactly what it showed (it stops if the proposal changed meanwhile) and sets
the flag again, however it ends. Every other run of this script sets the flag at its start if it
finds it off. ORG.md changes are logged as `human` and committed, so memcheck.py stays clean.
Steering is the store's STEERING.md once it has one, with the same lock as ORG.md: an item for
`Steering` changes the text at its top, an item for `Steering (<repo>)` that repository's section, and
the change is logged as `human` and committed like one to ORG.md. Without it, Steering is the block of
.claude/commands/dream.md in the repository the run is for (store.run_for(): the one this copy sits in,
or for the kit's own copy the one the working directory is in), and it is left uncommitted there.

The record is <store>/dreams/<name>-applied.json, named after the proposal (<name>-proposal.md),
which is never changed:
  {"proposal": "<file>", "items": [{"key", "item", "line", "section", "target", "action",
   "kind", "result", "reason", "commits", "at"}]}
`result` is one of: applied, already applied, refused, yours, needs a session. `line` is the
item's line in the proposal, counted from 0. `key` is a hash of the item's own text and the lines
under it, so an item that was rewritten is a new item. An item recorded as applied is never
applied again. The record is saved after every item, so a run that is killed loses nothing it
did, and committed with the proposal at the end. Agents don't edit it (guard_memory.py, and a
deny rule): a row falsely saying `applied` would keep an approved change from ever being made.
"""
import argparse, contextlib, datetime, fcntl, json, os, re, shlex, signal, sys
from pathlib import Path
from typing import NamedTuple, Optional

import dream_proposal
import memcheck
import memory_context
import needs
import memwrite
import store
from memwrite import AMBIGUOUS, DENIED, REJECTED, STALE, Refused

ROOT = Path(__file__).resolve().parent.parent
MEM = memwrite.MEM
DREAMS = MEM / "dreams"
ORG = MEM / "ORG.md"
REPO = store.run_for(ROOT, MEM)  # the repository this run is for: a new skill goes there, and so does Steering while the store has no STEERING.md
DREAM_MD = REPO / ".claude" / "commands" / "dream.md" if REPO else None  # without STEERING.md, Steering is the end of this file
STEERING_MD = MEM / memcheck.STEERING  # once it is there, Steering is this file and nothing else
AGENT = "dreamer"
ITEM = re.compile(r"^- \[([ xX])\] (.*)$")
TICKED = re.compile(r"^\s*[-*]\s*\[[xX]\]")  # anything a person would call a ticked line
NUMBER = re.compile(r"^(\d+)\.\s+(.*)$")
MOVE = re.compile(r"^(\S+\.md): (.+)$")  # under a split: one new file and the lines that move there
SKILL = re.compile(r"^\.claude/skills/[\w.-]+/SKILL\.md$")
STEERING = "\n## Steering"
SUFFIX = "-proposal.md"
APPLIED, ALREADY, REFUSED, YOURS, SESSION = "applied", "already applied", "refused", "yours", "needs a session"
LEFT_OUT = "left out"  # a fact of a carry item whose + the person turned into a -
FINAL = (APPLIED, ALREADY)


class Item:
    """One tick line of the proposal with the indented lines under it."""

    def __init__(self, line: int, section: str, ticked: bool, text: str, details: list, raw: str = None):
        numbered = NUMBER.match(text)
        fields = re.split(r"\s+—\s+", numbered[2] if numbered else text, maxsplit=2) + ["", ""]
        self.line, self.section, self.ticked, self.details, self.raw = line, section, ticked, details, raw
        self.id = f"line {line + 1}" if raw is not None else numbered[1] if numbered else fields[0].strip()
        self.target = re.sub(r"^(\./)?memory/", "", fields[0].strip())
        self.said = fields[1].strip()  # the action as written, for messages
        self.action = (self.said.split() or [""])[0].lower()
        self.rest = fields[2].strip()
        if raw is not None:
            self.target, self.said, self.action, self.rest = raw.strip(), "", "", ""
        self.key = dream_proposal.item_id(text, details)  # which item this is, for the record and for what is carried: never its number

    def detail(self, name: str):
        """The text of the indented line that starts `<name>:`, or None."""
        return next((row[len(name) + 1:].strip() for row in self.details if row.startswith(name + ":")), None)

    @property
    def why(self) -> str:
        return (self.detail("why") or self.said).split(" · ")[0][:200]

    @property
    def kind(self) -> str:
        if self.raw is not None:
            return "unreadable"
        if self.section.startswith(needs.HEADING):
            return "needs"  # what needs the person is not memory, whatever the line says
        if self.action == dream_proposal.CARRY:
            return "carry"
        if self.section.startswith("Inbox items"):
            return "inbox"
        if self.section.startswith("Scratchpads"):
            return "scratchpad"
        if self.target == "ORG.md":
            return "org"
        if self.target.startswith("Steering"):
            return "steering"
        return "skill" if SKILL.match(self.target) else "memory"

    @property
    def part(self):
        """For a Steering item in a store with STEERING.md: the repository whose section it changes (`Steering (<repo>)`), or None for the text at its top."""
        named = re.fullmatch(r"Steering \((.+)\)", self.target)
        return named[1].strip() if named else None

    @property
    def mine(self) -> bool:
        return self.kind in ("org", "steering")

    def label(self) -> str:
        return " — ".join(part for part in (self.target, self.said) if part)


def items(text: str) -> list:
    """Every tick line. One that is ticked but not written as an item (`* [x]`, a leading space) is kept too, to be refused by name."""
    found, section, lines = [], "", text.split("\n")
    for at, line in enumerate(lines):
        if line.startswith("## "):
            section = line[3:].strip()
        match = ITEM.match(line)
        if match:
            end = at + 1
            while end < len(lines) and lines[end].startswith("      "):
                end += 1
            found.append(Item(at, section, match[1] != " ", match[2], [row.strip() for row in lines[at + 1:end]]))
        elif TICKED.match(line):
            found.append(Item(at, section, True, line, [], raw=line))
    return found


def unquoted(text: str, what: str) -> str:
    """The text between one pair of double quotes. The proposal quotes every line it wants written or removed."""
    if len(text) < 2 or not (text.startswith('"') and text.endswith('"')):
        raise Refused(REJECTED, f"REJECTED: {what} is not between double quotes in the proposal, so it isn't clear where the line starts and ends.")
    return text[1:-1]


class View:
    """What each file holds. Planning ahead of the writes (a dry run, the person's pass): as the steps so far would leave it."""

    def __init__(self, became: dict = None):
        self.texts = {}
        self.became = became or {}  # {(target, a line without its pin): what a correction in this proposal makes of it}

    def read(self, path: Path) -> str:
        if path in self.texts:
            return self.texts[path]
        return steering(path)[2] if path == DREAM_MD and path is not None else memwrite.read(path)[0]

    def has(self, path: Path) -> bool:
        return path in self.texts or path.exists()


def steering(path: Path):
    """(everything up to the Steering heading, the rest of that heading's line, the block under it)."""
    text = path.read_text() if path.exists() else ""
    if STEERING not in text:
        raise Refused(STALE, f"STALE: {path} has no Steering block. Nothing was written.")
    head, rest = text.split(STEERING, 1)
    title, _, block = rest.partition("\n")
    return head, title, block


def within(text: str, section, change) -> str:
    """A STEERING.md with one part changed by `change`: the text at its top (section None), or the section `## <section>`, made at the end when it is not there."""
    lines = text.split("\n")
    heads = [at for at, line in enumerate(lines) if memory_context.SECTION.match(line)]
    if section is None:
        start, end = (1 if lines and lines[0].startswith("# ") else 0), (heads[0] if heads else len(lines))
    else:
        found = [at for at in heads if memory_context.SECTION.match(lines[at])[1] == section]
        if not found:
            return text.rstrip("\n") + f"\n\n## {section}\n" + change("")
        start, end = found[0] + 1, next((at for at in heads if at > found[0]), len(lines))
    part = "\n".join(lines[start:end])
    lead = part[:len(part) - len(part.lstrip("\n"))]
    changed = change(part.strip("\n"))
    changed = changed if changed.endswith("\n") else changed + "\n"  # the part ends its line, whatever the change left
    out = "\n".join(lines[:start]) + ("\n" if start else "") + lead + changed
    return out + ("\n" + "\n".join(lines[end:]) if end < len(lines) else "")


class Part(NamedTuple):
    """The last element of a step on STEERING.md: the part it acts in, a repo's section or (None) the text at the top."""
    section: Optional[str]


def part_of(text: str, section) -> str:
    """What one part of a STEERING.md holds: the text at its top, or the section `## <section>` (empty when there is none)."""
    seen = []
    within(text, section, lambda part: seen.append(part) or part)
    return seen[0] if seen else ""


def heading(path: Path, comments: list = ()) -> str:
    """How a new project/ file starts: its topic as a heading, then the format comments it was given."""
    return f"# {path.stem.replace('-', ' ').replace('_', ' ').capitalize()}\n" + ("\n" + "".join(row + "\n" for row in comments) if comments else "")


# ── from an item to steps. A step is (verb, path, text...): create, append, edit, delete. ──
def opening(text: str, words: str, but: str = None) -> list:
    """The entries that begin with these words. `but` is left out: a merged line may begin like a line it stands for."""
    return [entry for entry in memwrite.entries(text) if entry.startswith(words) and (but is None or memwrite.shape(entry) != memwrite.shape(but))]


def gone(text: str, entry: str, name: str, logged, but: str):
    """The old line of an edit is not in the file and the new one (`but`) is. That counts as done only if the old one is really out.

    Either the log shows that exact entry leaving the file (`logged` is the file's name in the log, None for a
    file the store doesn't log), or the file holds nothing like it apart from the new line. A near match means
    the item named the old line inexactly (one character off, only its opening words, without its indentation)
    and it is still there beside the new one, so the item is refused, with the closest lines.
    """
    if logged and memwrite.taken_out(logged, entry):
        return
    if memwrite.near(text, entry, but):
        raise memwrite.missing(text, entry, name, but)


def corrections(ticked: list) -> dict:
    """What the ticked edit items of one proposal make of the lines they correct: {(target, the line without its pin): the new line}.

    A pin item names its line as it was when the dream wrote the proposal. When a correction of that line is in the
    same proposal and was applied first, in this run or an earlier one, the line reads otherwise by then; this is
    how the pin item still finds it, so the dream need not put the two in an order.
    """
    found = {}
    for item in ticked:
        if item.action == "edit" and item.detail("replaces") is not None:
            try:
                old, new = unquoted(item.detail("replaces"), "the `replaces:` line"), unquoted(item.rest, "the item's line")
            except Refused:
                continue  # an item that is not written as one is refused when its own turn comes, with the reason
            if memwrite.unpinned(old) != memwrite.unpinned(new):
                found[(item.target, memwrite.unpinned(old))] = new
    return found


def plan(item: Item, text: str, path: Path, name: str, logged, new_file: bool = False, became: dict = None) -> list:
    """The writes that take one file from what it holds to what the item says. None when it is already there.

    `became` is what corrections in the same proposal make of this file's lines (corrections()), by the line without its pin.
    """
    line = unquoted(item.rest, "the item's line")
    there = bool(memwrite.find(text, line))
    if item.action == "add":
        if memwrite.depth(line):
            raise Refused(REJECTED, "REJECTED: the line to add is indented, so it belongs under another line, and the proposal can't say which. "
                                    "Nothing was written. An added line starts at the margin.")
        if there or line.rstrip() in [row.rstrip() for row in text.split("\n")]:
            return []  # also when the line is there with lines indented under it
        return ([("create", path, heading(path))] if new_file else []) + [("append", path, line)]
    if item.action == "delete":
        if there:
            return [("delete", path, line)]
        if logged and memwrite.taken_out(logged, line):
            return []
        # Not there, and no log row shows it leaving: done, or named wrongly? A wrong "done" leaves the line in place for good and
        # unseen; a wrong "refused" costs the person one line to read. So it is refused, with whatever the file holds that is close.
        raise memwrite.missing(text, line, name)
    if item.action == "edit":
        if item.detail("replaces") is None:
            raise Refused(REJECTED, "REJECTED: an edit names the line it changes on a `replaces:` line, and this item has none.")
        old = unquoted(item.detail("replaces"), "the `replaces:` line")
        if memwrite.shape(old) == memwrite.shape(line):
            raise Refused(REJECTED, "REJECTED: the item replaces a line with itself.")
        if memwrite.unpinned(old) == memwrite.unpinned(line):
            # A pin item: the same line, with ` (pin)` put on or taken off. The file may name it either way; what counts is how it stands there.
            found, named, followed = memwrite.find(text, old), old, set()
            while not found and became and memwrite.unpinned(old) in became and old not in followed:
                followed.add(old)
                old = became[memwrite.unpinned(old)]  # a correction in this proposal changed the wording: the pin goes on the line as it reads now
                found = memwrite.find(text, old)
            if not found:
                raise memwrite.missing(text, named, name)
            pinned, wanted = memwrite.is_pinned(text.split("\n")[found[0][0]]), memwrite.is_pinned(line)
            first, sep, rest = memwrite.unpinned(old).partition("\n")
            return [] if pinned == wanted else [("edit", path, old, first + (memwrite.PINNED if wanted else "") + sep + rest)]
        if memwrite.find(text, old):
            return [("delete", path, old)] if there else [("edit", path, old, line)]
        if not there:
            raise memwrite.missing(text, old, name)
        gone(text, old, name, logged, line)
        return []
    if item.action == "merge":
        words = [part.strip() for part in (item.detail("replaces") or "").split(" ; ") if part.strip()]
        if not words:
            raise Refused(REJECTED, "REJECTED: a merge names the lines it stands for on a `replaces:` line, by their opening words, and this item has none.")
        stood = []
        for part in words:
            fits = opening(text, part, line)
            if len(fits) > 1 or (fits and fits[0] in stood):
                raise Refused(AMBIGUOUS, f"AMBIGUOUS: \"{part}\" fits more than one line of {name}, or a line another part already names. Nothing was written.")
            stood.append(fits[0] if fits else None)
        # A pinned line first: the merged line takes the place of the first, and an edit keeps a line pinned.
        present = sorted((entry for entry in stood if entry), key=lambda entry: not memwrite.is_pinned(entry))
        if there:
            return [("delete", path, entry) for entry in present]
        if None in stood:
            raise Refused(STALE, f"STALE: a line this merge stands for is not in {name} now (\"{words[stood.index(None)]}\"), "
                                 "and the merged line is not there either. Nothing was written.")
        return [("edit", path, present[0], line)] + [("delete", path, entry) for entry in present[1:]]
    raise Refused(REJECTED, f"REJECTED: \"{item.said}\" is not add, edit, delete, merge or split.")


def plan_split(item: Item, view: View, source: Path):
    """(steps, lines left where they are, lines an earlier run moved). A line is appended to its topic file first and only then taken out of the original."""
    moves = [MOVE.match(row) for row in item.details]
    moves = [(match[1], [part.strip() for part in match[2].split(" ; ") if part.strip()]) for match in moves if match]
    if not moves:
        raise Refused(REJECTED, "REJECTED: a split names each new file on a line of its own, followed by the lines that move there, and this item has none.")
    text, steps, left, taken, moved = view.read(source), [], [], [], 0
    top = text.split("\n- ", 1)[0] if "\n- " in text else ""  # what stands above the first line: the heading and the format comments
    comments = [row for row in top.split("\n") if re.fullmatch(r"<!--.*-->", row.strip())]
    for target, words in moves:
        topic = memwrite.resolve(target, AGENT)
        if topic == source or topic.parent != MEM / "project" or source.parent != MEM / "project":
            raise Refused(DENIED, f"DENIED: a split moves lines from one project/ file to other project/ files, and {target} is not one.")
        held, made = view.read(topic), view.has(topic)
        for part in words:
            fits = [entry for entry in opening(text, part) if entry not in taken]
            if len(fits) == 1:
                if not made:
                    steps.append(("create", topic, heading(topic, comments)))
                    made = True
                if not memwrite.find(held, fits[0]):
                    steps.append(("append", topic, fits[0]))
                steps.append(("delete", source, fits[0]))
                taken.append(fits[0])
            elif not fits and len(opening(held, part)) == 1:
                moved += 1  # gone from the original and once in the topic file
            else:
                left.append(f"\"{part}\" fits {len(fits) or 'no'} line{'s' if len(fits) > 1 else ''}")
    return steps, left, moved


def planned(item: Item, view: View):
    """(steps, a note for the record) for an item this script applies: a memory file, ORG.md or this repo's Steering."""
    if item.kind == "org":
        if item.action not in ("add", "edit", "delete"):
            raise Refused(REJECTED, f"REJECTED: \"{item.said}\" is not add, edit or delete, which is all an ORG.md or Steering item can be.")
        return plan(item, view.read(ORG), ORG, "ORG.md", "ORG.md"), ""
    if item.kind == "steering":
        if item.action not in ("add", "edit", "delete"):
            raise Refused(REJECTED, f"REJECTED: \"{item.said}\" is not add, edit or delete, which is all an ORG.md or Steering item can be.")
        if STEERING_MD.exists():
            # Only the part the item names: a line another section holds is not this one's, and is never the one changed
            steps = plan(item, part_of(view.read(STEERING_MD), item.part), STEERING_MD, STEERING_MD.name, STEERING_MD.name)
            return [step + (Part(item.part),) for step in steps], ""
        if DREAM_MD is None:
            raise Refused(STALE, f"STALE: this run is for no repository of {MEM.name} (the working directory is in none of its roots.txt), "
                                 "and the store has no STEERING.md, so there is no Steering to change. Nothing was written.")
        steps = plan(item, view.read(DREAM_MD), DREAM_MD, name_of(DREAM_MD), None)
        return steps, f"changed, not committed: {DREAM_MD}" if steps else ""
    path = memwrite.resolve(item.target, AGENT)
    if item.action == "split":
        steps, left, moved = plan_split(item, view, path)
        note = "left where they are: " + "; ".join(left) if left else ""
        if left and not steps and not moved:
            raise Refused(STALE, f"STALE: no line of this split could be moved ({note}). Nothing was written.")
        return steps, note
    name = str(path.relative_to(MEM))
    became = {old: new for (target, old), new in view.became.items() if target == item.target}
    return plan(item, view.read(path), path, name, name, new_file=not view.has(path), became=became), ""


def carry_note(item: Item, who: tuple, view: View, dry: bool) -> dict:
    """The checked facts of one note: each `+` line appended to its file as an add would be, each `-` line left out. One part per fact."""
    rows = [row for row in item.details if row and not row.startswith("why:")]
    facts = [dream_proposal.FACT.match(row) for row in rows]
    if not rows or not all(facts):
        raise Refused(REJECTED, f"REJECTED: under a carry item every line is `+ project/<file>.md \"<line>\"` (or `-` to leave that fact out), "
                                f"and this is not: {next((row for row, fact in zip(rows, facts) if not fact), 'the item has no such line')}")
    parts, commits = [], []
    for sign, file, line, _ in (fact.groups() for fact in facts):
        if sign == "-":
            parts.append(dict(sign=sign, file=file, line=line, **outcome(LEFT_OUT, "its + was turned into a -")))
            continue
        try:
            path = memwrite.resolve(file, AGENT)
            name = str(path.relative_to(MEM))
            if not name.startswith("project/"):
                raise Refused(REJECTED, f"REJECTED: a fact from a note goes into a file in project/, and {file} is not one.")
            fact = Item(item.line, item.section, True, f'{name} — add — "{line}"', [f"why: {item.why}"])
            steps = plan(fact, view.read(path), path, name, name, new_file=not view.has(path))
            if dry:
                rehearse(steps, who, view)
                done = outcome(APPLIED if steps else ALREADY)
            else:
                done = run(steps, who)
        except Refused as stop:
            done = outcome(REFUSED, str(stop))
        parts.append(dict(sign=sign, file=file, line=line, **done))
        commits += done["commits"]
    refused = [part for part in parts if part["result"] == REFUSED]
    taken = [part for part in parts if part["sign"] == "+"]
    if refused:
        return dict(outcome(REFUSED, f"{len(refused)} of {len(taken)} facts were refused; the first: {refused[0]['reason']}", commits), parts=parts)
    if not taken:
        return dict(outcome(ALREADY, "every fact was left out"), parts=parts)
    wrote = any(part["result"] == APPLIED for part in parts)
    return dict(outcome(APPLIED if wrote else ALREADY, f"{len(parts) - len(taken)} left out" if len(taken) < len(parts) else "", commits), parts=parts)


def reopened(item: Item, before: dict) -> bool:
    """A carry item that was applied with a fact left out, and whose `-` the person has since turned back to `+`: that fact is still to be written."""
    taken = {fact.group(2, 3) for fact in map(dream_proposal.FACT.match, item.details) if fact and fact[1] == "+"}
    return any(part["result"] == LEFT_OUT and (part["file"], part["line"]) in taken for part in before.get("parts", []))


def after(step: tuple, text: str, agent: str, name: str) -> str:
    """What the file holds once this step is done. The same functions memwrite writes with, so the same refusals."""
    if isinstance(step[-1], Part):  # STEERING.md: the step acts in the part the item names, and nowhere else in the file
        return within(text, step[-1].section, lambda part: after(step[:-1], part, agent, name))
    verb, project = step[0], step[1] != DREAM_MD and memwrite.in_project(step[1])
    if verb == "append":
        return memwrite.written(text, step[2], agent, name, append=True, project=project)
    if verb == "create":
        if text:
            raise Refused(STALE, f"STALE: {name} was to be a new file and is there now. Nothing was written.")
        return memwrite.written(text, step[2], agent, name, project=project)
    if verb == "edit":
        return memwrite.edited(text, step[2], step[3], agent, name, project)[0]
    return memwrite.removed(text, step[2], name)[0]


def name_of(path: Path) -> str:
    return f"the Steering block of {path}" if path == DREAM_MD and path is not None else str(path.relative_to(MEM))


def rehearse(steps: list, who: tuple, view: View):
    """The steps done on the view only. Raises what the real write would refuse."""
    memwrite.actor(*who)
    for step in steps:
        path = step[1]
        view.texts[path] = after(step, view.read(path), who[0], name_of(path))


def carry(step: tuple, who: tuple) -> memwrite.Written:
    """One step as one write. In the store: a memwrite write, checked, logged and committed there. Steering: this repo's file."""
    verb, path = step[0], step[1]
    if path == STEERING_MD:  # in its part: a write of the whole file, checked, logged and committed as memwrite's are
        why = memwrite.actor(*who)
        with memcheck.locked(MEM):
            old, eol = memwrite.read(path)
            q = memwrite.quoted
            detail = (f" · added: {q(step[2])}" if verb == "append" else f" · was: {q(step[2])} · now: {q(step[3])}" if verb == "edit"
                      else f" · removed: {q(step[2])} · after: {q(memwrite.removed(part_of(old, step[-1].section), step[2], path.name)[1])}")
            return memwrite.record(path, after(step, old, who[0], name_of(path)), memwrite.h(path), who[0], who[1], why, detail, eol)
    if path == DREAM_MD and path is not None:
        memwrite.actor(*who)
        head, title, block = steering(path)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(head + STEERING + title + "\n" + after(step, block, who[0], name_of(path)))
        os.replace(tmp, path)
        return memwrite.Written("", "", "", "")
    if verb == "append":
        return memwrite.write(path, step[2], *who, append=True, hold=False)  # the person approved this line, whatever is like it
    if verb == "create":
        return memwrite.write(path, step[2], *who, expect="new")
    if verb == "edit":
        return memwrite.edit(path, step[2], step[3], *who, hold=False)
    return memwrite.delete(path, step[2], *who)


def outcome(result: str, reason: str = "", commits: list = None) -> dict:
    return {"result": result, "reason": reason, "commits": commits or []}


def run(steps: list, who: tuple, note: str = "") -> dict:
    """Does the steps in order. A refusal part-way says how far it got; the next run goes on from what the file then holds."""
    commits = []
    for done, step in enumerate(steps):
        try:
            written = carry(step, who)
        except Refused as stop:
            return outcome(REFUSED, f"{stop}" + (f" ({done} of {len(steps)} writes were made before this.)" if done else ""), commits)
        commits += [written.commit] if written.commit else []
        note = "; ".join(part for part in (note, written.warning) if part)
    return outcome(APPLIED if steps else ALREADY, note, commits)


# ── one item, in a run by an agent ──
def inbox_item(item: Item, who: tuple, dry: bool) -> dict:
    if item.action == "accept":
        return outcome(SESSION, f"do what inbox/{Path(item.target).name} proposes unless a numbered item already does, then `git -C memory rm` it")
    if item.action != "reject":
        raise Refused(REJECTED, f"REJECTED: an inbox item is accepted or rejected, and this one says \"{item.said}\".")
    path = memwrite.resolve("inbox/" + Path(item.target).name, AGENT)
    why = memwrite.actor(*who)
    if not path.exists():
        return outcome(ALREADY)
    if dry:
        return outcome(APPLIED)
    with memcheck.locked(MEM):
        path.unlink()
        inside = memcheck.git(MEM, "rev-parse", "--is-inside-work-tree").returncode == 0
        error = memcheck.commit(MEM, f"memory({AGENT}): {why}\n\nsession: {who[1]}", [str(path)]) if inside else ""
        commit = memcheck.git(MEM, "rev-parse", "--short", "HEAD").stdout.strip() if inside and not error else ""
    return outcome(APPLIED, f"WARN: removed, but git commit failed: {error}" if error else "", [commit] if commit else [])


def handle(item: Item, who: tuple, view: View, dry: bool) -> dict:
    """What a run by an agent makes of one ticked item."""
    try:
        if item.kind == "unreadable":
            raise Refused(REJECTED, f"REJECTED: line {item.line + 1} is ticked but is not written as an item (`- [x] ` at the very start of the line), "
                                    f"so it was not applied: {item.raw.strip()}")
        if item.kind == "needs":
            raise Refused(REJECTED, f"REJECTED: line {item.line + 1} stands under \"{needs.HEADING}\": what needs the person is not memory and is never applied. "
                                    "Close it with `python3 scripts/dream_proposal.py done \"<words only it holds>\"` once it is dealt with.")
        if item.kind == "carry":
            return carry_note(item, who, view, dry)
        if item.mine:
            return outcome(YOURS, f"lift the lock and run it yourself: {lift()} && python3 scripts/dream_apply.py --yours")
        if item.kind == "skill":
            if REPO is None:
                raise Refused(STALE, f"STALE: this run is for no repository of {MEM.name} (the working directory is in none of its roots.txt), "
                                     "so there is no repository to write the skill into. Run the apply from inside the repository.")
            return outcome(SESSION, f"write {REPO / item.target} and leave it uncommitted")
        if item.kind == "scratchpad":
            return outcome(SESSION, f"condense agents/{item.target}/scratch.md to what the item keeps")
        if item.kind == "inbox":
            return inbox_item(item, who, dry)
        steps, note = planned(item, view)
        if not dry:
            return run(steps, who, note)
        rehearse(steps, who, view)
        return outcome(APPLIED if steps else ALREADY, note)
    except Refused as stop:
        return outcome(REFUSED, str(stop))


# ── which proposal, and what a finished item of a closed one means. dream_proposal.py keeps the state of every proposal. ──
def the_proposal() -> Path:
    """The proposal a run that applies works on: the store's open one. With none open it stops."""
    held = dream_proposal.open_one(MEM)
    if held:
        return DREAMS / held["name"]
    sys.exit(f"{MEM.name} has no open proposal, so there is nothing to apply.")


def with_waiting() -> list:
    """For --yours and --done: every proposal that may still hold an item for the person or a session, oldest first, the open one last.

    Each as (path, the ids of its items that still wait, or None for the open proposal, whose record alone says).
    Closing a proposal carries its approved items on until they are done or withdrawn, through any number of
    later proposals, so an item two proposals back is reached like one in the latest. For a closed proposal the
    store's record of proposals says which items still wait, with a number or without: one that was withdrawn, or done, is not offered again.
    """
    ledger = dream_proposal.load(MEM)
    found = []
    for known in ledger["proposals"]:
        path = DREAMS / known["name"]
        if known["state"] == "open":
            found.append((path, None))
        elif known["state"] == "applied" and path.exists():
            found.append((path, {entry["id"] for entry in ledger["left"] if entry["from"] == known["name"] and entry["waiting"]}))
    return found


def waits(item: Item, row, ids, results: tuple) -> bool:
    """Does this ticked item still wait, with one of these results in its proposal's record?"""
    return row is not None and row["result"] in results and (ids is None or item.key in ids)


def unreachable(who: str) -> list:
    """Lines for what waits for `who` and that no command can act on any more (its proposal is gone, its record says otherwise), each with why."""
    return [f"  {entry['item']}\n      approved in {entry['from']}, and nothing can do it any more: {gone}. Clear it: python3 scripts/dream_proposal.py withdraw \"<words only it holds>\""
            for entry in dream_proposal.waiting(MEM) if entry["waiting"] == who for gone in [dream_proposal.stuck(MEM, entry)] if gone]


NONE_WAITING = f"{MEM.name} has no open proposal, and no applied one with an item still waiting for you or a session. Nothing was done."


def for_a_session() -> list:
    """(proposal, row) for every item that a closed proposal left waiting for a session, oldest proposal first."""
    found = []
    for path, ids in with_waiting():
        if ids:
            record = Record(path)
            found += [(path, record.rows[item.key]) for item in items(path.read_text())
                      if item.ticked and waits(item, record.rows.get(item.key), ids, (SESSION,))]
    return found


def say_earlier(found: list, opening: str, also: bool = False):
    """What earlier proposals left for a session, under the proposal each came from, and how each is reported."""
    print(opening)
    for path in dict.fromkeys(path for path, _ in found):
        print(f"From {path.name}:")
        for other, row in found:
            if other == path:
                print(f"  {row['item']}: {row['target']} — {row['action'][:60]}" + ("\n      " + row["reason"].replace("\n", "\n      ") if row["reason"] else ""))
    print(("Do these as well" if also else "Do only these") + ", and after each run python3 scripts/dream_apply.py --done <item> --of <proposal file name>, with the item as it is written here.")


def leftovers():
    """A run that applies, with no proposal open: what earlier proposals left for a session, so the session can do exactly those.

    An approved item that needs a session (a skill to write, a scratchpad to condense, an inbox item) stays
    until it is done or withdrawn, and this is how a session gets to it once its proposal is closed.
    """
    found, lost = for_a_session(), unreachable("a session")
    mine = sum(entry["waiting"] == "you" for entry in dream_proposal.waiting(MEM))
    yours = (f"Yours (ORG.md and Steering): {mine} approved earlier wait for the person, who runs: {lift()} && python3 scripts/dream_apply.py --yours"
             if mine else "")
    if lost:
        print("Approved earlier for a session, and not offered:\n" + "\n".join(lost))
    if not found:
        sys.exit(f"{MEM.name} has no open proposal, so there is nothing to apply, and nothing approved earlier waits for a session."
                 + (" " + yours if yours else ""))
    say_earlier(found, f"{MEM.name} has no open proposal. Approved earlier and still waiting for a session:")
    print("There is no proposal to close afterwards.")
    if yours:
        print(yours)


def settle(proposal: Path, rows: list, finished: list = ()):  # finished: the keys of the items this run completed
    """The end of every run that wrote the record: how the ticked items stand, and for a closed proposal, what is no longer carried.

    This neither closes a proposal nor holds one open. Closing is a step of its own, the last of /dream apply
    (`python3 scripts/dream_proposal.py applied`); it reads the record: `applied` and `already applied` are
    done, the rest is carried on. So when --yours or --done finishes an item (`finished`, by its key) of a
    proposal that is closed already, the carried entry is dropped here, or it would wait for ever.
    """
    count = lambda result: sum(1 for row in rows if row["result"] == result)
    print(f"In all: {count(APPLIED)} applied, {count(ALREADY)} already applied, {count(REFUSED)} refused, {count(YOURS)} yours, {count(SESSION)} need a session.")
    held = dream_proposal.open_one(MEM)
    if finished and not (held and held["name"] == proposal.name):
        dropped = [row["item"] for row in rows if row["key"] in finished and dream_proposal.item_done(MEM, proposal.name, row["key"])]
        if dropped:
            print(f"{proposal.name} is closed: {' '.join(shlex.quote(item) for item in dropped)} will not be handed to the next dream again.")


# ── the record ──
class Record:
    """What became of each item, by the item's key. Saved after every item; committed once, at the end."""

    def __init__(self, proposal: Path):
        self.proposal = proposal
        self.path = proposal.with_name(proposal.name[:-len(SUFFIX)] + "-applied.json")
        self.rows = {}
        if self.path.exists():
            try:
                self.rows = {row["key"]: row for row in json.loads(self.path.read_text())["items"]}
            except (ValueError, KeyError, TypeError) as error:
                sys.exit(f"{self.path} can't be read ({error}). It says which items are applied, so nothing was done. Repair it or move it away: "
                         "without it, every ticked item is looked at again and only what the files still lack is written.")

    def put(self, item: Item, result: dict, ticked: list):
        """One item's outcome, on disk at once. A run that changes nothing leaves the row, and its time, as it was."""
        before = self.rows.get(item.key)
        same = before and all(before[part] == result[part] for part in ("result", "reason", "commits"))
        at = before["at"] if same else datetime.datetime.now().isoformat(timespec="seconds")
        self.rows[item.key] = {"key": item.key, "item": item.id, "line": item.line, "section": item.section, "target": item.target,
                               "action": item.said, "kind": item.kind, **result, "at": at}
        self.save(ticked)

    def listed(self, ticked: list) -> list:
        """Rows for what is ticked now, in the proposal's order, then what was applied from lines no longer ticked or since rewritten."""
        now = [item.key for item in ticked if item.key in self.rows]
        return [self.rows[key] for key in now] + [row for key, row in self.rows.items() if key not in now and row["result"] in FINAL]

    def save(self, ticked: list):
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps({"proposal": self.proposal.name, "items": self.listed(ticked)}, indent=2, ensure_ascii=False) + "\n")
        os.replace(tmp, self.path)

    def commit(self, ticked: list):
        rows = [self.rows[item.key] for item in ticked if item.key in self.rows]
        counts = ", ".join(f"{sum(1 for row in rows if row['result'] == result)} {result}" for result in (APPLIED, ALREADY, REFUSED, YOURS, SESSION)
                           if any(row["result"] == result for row in rows))
        with memcheck.locked(MEM):
            if memcheck.git(MEM, "rev-parse", "--is-inside-work-tree").returncode == 0:
                error = memcheck.commit(MEM, f"dream apply: {self.proposal.name}: {counts}", [str(self.proposal), str(self.path)])
                if error:
                    print(f"WARN: the record is written, but git commit failed: {error}")


@contextlib.contextmanager
def one_apply():
    """Two applies at once would each find a line missing and each add it. One per store, like one dream per store."""
    state = Path.home() / ".local" / "state" / "agent-memory"
    state.mkdir(parents=True, exist_ok=True)
    with open(state / f"{MEM.name}.apply.lock", "w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            sys.exit(f"An apply is already running for {MEM.name}. Nothing was started.")
        yield


def say(rows: list, proposal: Path, dry: bool = False):
    print(f"{proposal.name}: {len(rows)} ticked" + (" (dry run: nothing was written)" if dry else ""))
    for row in rows:
        result = {APPLIED: "would apply", REFUSED: "would refuse"}.get(row["result"], row["result"]) if dry and not row.get("earlier") else row["result"]
        commits = f" ({', '.join(row['commits'])})" if row["commits"] else ""
        reason = "\n      " + row["reason"].replace("\n", "\n      ") if row["reason"] else ""
        print(f"  {row['item']}: {result}{' earlier' if row.get('earlier') else ''}  {row['target']} — {row['action'][:60]}{commits}{reason}")
        for part in row.get("parts", []):  # the facts of a carry item, each with what became of it
            print(f"      {part['sign']} {part['file']}: {part['result']}" + (f" ({part['reason']})" if part["result"] == REFUSED else ""))
    mine = [row["item"] for row in rows if row["result"] == YOURS]
    left = [row["item"] for row in rows if row["result"] == SESSION]
    if mine:
        print(f"Yours (ORG.md and Steering): {' '.join(mine)}. An agent can't apply these and can't lift the lock. "
              f"Do it yourself: {lift()} && python3 scripts/dream_apply.py --yours")
    if left:
        print(f"Needs a session: do only these, and after each run python3 scripts/dream_apply.py --done <item>, with the item as it is written here: "
              + " ".join(shlex.quote(item) for item in left))


def lift() -> str:
    """The command that lifts the lock on the person's files: ORG.md, and STEERING.md when the store has one."""
    return f"chflags nouchg {ORG}" + (f" {STEERING_MD}" if STEERING_MD.exists() else "")


def lock_org():
    """Every run that is not the person's pass starts here: ORG.md or STEERING.md found unlocked (a pass that was killed, a hand edit left open) is locked."""
    for path in (ORG, STEERING_MD):
        if path.exists() and memcheck.is_locked(path) is False:
            memcheck.lock_org(MEM)
            print(f"{path} was not locked. It is locked again.")


def apply(proposal: Path, dry: bool):
    """A run by an agent, or its dry run: every ticked item that isn't applied yet."""
    ticked = [item for item in items(proposal.read_text()) if item.ticked]
    if not ticked:
        print(f"{proposal.name}: nothing is ticked, so nothing was done.")
        return
    stem = proposal.name[:-len(SUFFIX)]
    record, view, rows = Record(proposal), View(corrections(ticked)), []
    for item in ticked:
        before = record.rows.get(item.key)
        again = bool(before) and reopened(item, before)
        if before and not again and (before["result"] in FINAL or (item.mine and before["result"] == REFUSED)):  # what the person's pass refused is theirs to see again
            rows.append({**before, "line": item.line, "earlier": before["result"] in FINAL})
            continue
        result = handle(item, (AGENT, f"dream-{stem}", f"dream {stem} item {item.id}: {item.why}"), view, dry)
        if again:  # the facts written before are found in place; the row keeps the commits that wrote them
            result["commits"] = before["commits"] + result["commits"]
        if dry:
            rows.append({"item": item.id, "target": item.target, "action": item.said, **result})
        else:
            record.put(item, result, ticked)
            rows.append(record.rows[item.key])
    say(rows, proposal, dry)
    earlier = for_a_session()  # what the listing and the reminder promise: an apply in a session does what earlier proposals left for one
    if earlier:
        say_earlier(earlier, "Approved in earlier proposals and still waiting for a session:", also=True)
    if not dry:
        print(f"Record: {record.path}")
        record.commit(ticked)
        settle(proposal, rows)


def done(ids: list, of: str = None):
    """The session did a leftover. Only an item this script handed to a session can be reported this way, from whichever proposal it waits in."""
    sources = [(path, numbers, Record(path), [item for item in items(path.read_text()) if item.ticked])
               for path, numbers in with_waiting() if of is None or path.name == of]
    found = []
    for wanted in ids:
        match = [(path, record, ticked, item) for path, numbers, record, ticked in sources for item in ticked
                 if item.id == wanted and waits(item, record.rows.get(item.key), numbers, (SESSION,))]
        if not match:
            if not dream_proposal.waiting(MEM) and not dream_proposal.open_one(MEM):
                sys.exit(NONE_WAITING)
            sys.exit(f"\"{wanted}\" is not one item waiting for a session" + (f" in {of}" if of else "") + ". Nothing was recorded. "
                     "`python3 scripts/dream_proposal.py` lists what waits.")
        if len(match) > 1:
            sys.exit(f"\"{wanted}\" waits for a session in more than one proposal: {', '.join(path.name for path, *_ in match)}. Nothing was recorded. "
                     "Say which: --done <item> --of <proposal file name>.")
        found.append(match[0])
    for path, record, ticked, item in found:
        record.put(item, outcome(APPLIED, "done by the apply session"), ticked)
    for path in dict.fromkeys(path for path, *_ in found):  # each proposal's record once, in the order given
        record, ticked = next((record, ticked) for other, record, ticked, _ in found if other == path)
        record.commit(ticked)
        settle(path, [record.rows[item.key] for item in ticked if item.key in record.rows], [item.key for other, _, _, item in found if other == path])
    print(f"recorded as applied: {' '.join(shlex.quote(item) for item in ids)}")


def under(text: str, entry: str) -> str:
    """The line an entry stands under: the nearest line above it that is indented less. Empty for a line at the margin."""
    found = memwrite.find(text, entry)
    lines = text.split("\n")
    if not found:
        return ""
    start = found[0][0]
    return next((lines[at].strip() for at in range(start - 1, -1, -1) if lines[at].strip() and memwrite.depth(lines[at]) < memwrite.depth(lines[start])), "")


def shown(item: Item, steps: list, before: str) -> str:
    """One change as the person is asked about it: the file, where in it, and the text going out and in."""
    path = steps[0][1]
    rows = [f"  {item.id}. {item.label()}", f"      in: {path}"]
    for step in steps:
        verb, old = step[0], step[2]
        if verb == "append":
            rows.append("      added at the end, as a line of its own under no other line:")
            rows.append(f"        {json.dumps(old, ensure_ascii=False)}")
        else:
            parent = under(before, old)
            rows.append(f"      {'changed' if verb == 'edit' else 'taken out'}, " + (f"under {json.dumps(parent, ensure_ascii=False)}:" if parent else "a line under no other line:"))
            rows.append(f"        was: {json.dumps(old, ensure_ascii=False)}")
            if verb == "edit":
                rows.append(f"        now: {json.dumps(step[3], ensure_ascii=False)}")
    return "\n".join(rows)


def stop_on_signal():
    """A kill becomes an ordinary exit, so the lock on ORG.md is set again on the way out."""
    def leave(number, frame):
        sys.exit(f"Stopped by signal {number}.")
    for number in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(number, leave)


def yours(sources: list = None):
    """The ticked ORG.md and Steering items that wait, of every proposal that has any: read once, shown with the proposal each came from, and after a yes written exactly as shown."""
    if not ORG.exists():
        sys.exit(f"{ORG} is not there.")
    state = memcheck.org_locked(MEM)
    if state is None:
        sys.exit("Files have no immutable flag on this system, so there is no lock on ORG.md for a person to lift. Edit ORG.md and Steering by hand.")
    if state or (STEERING_MD.exists() and memcheck.is_locked(STEERING_MD)):
        held = " and ".join(str(path) for path in (ORG, STEERING_MD) if path.exists() and memcheck.is_locked(path))
        sys.exit(f"{held} {'is' if ' and ' not in held else 'are'} locked, and this command never lifts the lock: that is how the store knows a person is here. "
                 f"Lift it yourself, then run this again; it is locked again when this ends:\n  {lift()}")
    stop_on_signal()
    try:
        if not sys.stdin.isatty():
            sys.exit("ORG.md and Steering are the person's. Run this yourself in a terminal; it shows each change and asks before it writes.")
        view, plans, texts = View(), [], {}
        for proposal, numbers in with_waiting() if sources is None else sources:  # oldest first, so changes land in the order they were approved
            text = texts[proposal] = proposal.read_text()
            ticked = [item for item in items(text) if item.ticked]
            record, stem = Record(proposal), proposal.name[:-len(SUFFIX)]
            todo = [item for item in ticked if item.mine and record.rows.get(item.key, {}).get("result") not in FINAL
                    and (numbers is None or item.key in numbers)]
            if todo:
                print(f"From {proposal.name}:")
            for item in todo:  # everything is worked out here, before the question; nothing below reads a proposal again
                who = ("human", "-", f"dream {stem} item {item.id}: {item.why}")
                try:
                    source = ORG if item.kind == "org" else STEERING_MD if STEERING_MD.exists() else DREAM_MD
                    before = view.read(source) if source else ""
                    steps, note = planned(item, view)
                    rehearse(steps, who, view)
                    plans.append((proposal, record, ticked, item, who, steps, note, None))
                    print(shown(item, steps, before) if steps else f"  {item.id}. {item.label()}\n      already there")
                except Refused as stop:
                    plans.append((proposal, record, ticked, item, who, [], "", stop))
                    print(f"  {item.id}. {item.label()}\n      can't be applied: " + str(stop).replace("\n", "\n      ")
                          + ("" if numbers is None else "\n      It stays approved and waiting. To take the approval back: python3 scripts/dream_proposal.py withdraw \"<words only it holds>\""))
        count = sum(1 for plan in plans if plan[5])
        lost = unreachable("you")
        if lost:
            print("Approved earlier, yours, and not offered:\n" + "\n".join(lost))
        if not plans:
            print("Nothing of yours is left that this can apply." if lost else "Nothing of yours is left to apply.")
            return
        if count:
            print(f"Apply these {count} as shown? Type yes: ", end="", flush=True)
            if sys.stdin.readline().strip() != "yes":
                sys.exit("Nothing was changed.")
            for proposal, text in texts.items():
                if proposal.read_text() != text:
                    sys.exit(f"{proposal.name} changed while this waited, so what was shown may no longer be what is ticked. Nothing was changed. Run it again.")
        for proposal in dict.fromkeys(plan[0] for plan in plans):  # one proposal's items, its record and what it no longer carries, then the next
            mine = [plan for plan in plans if plan[0] == proposal]
            record, ticked, rows = mine[0][1], mine[0][2], []
            for _, _, _, item, who, steps, note, stop in mine:
                record.put(item, outcome(REFUSED, str(stop)) if stop else run(steps, who, note), ticked)
                rows.append(record.rows[item.key])
            say(rows, proposal)
            print(f"Record: {record.path}")
            record.commit(ticked)
            settle(proposal, [record.rows[item.key] for item in ticked if item.key in record.rows], [row["key"] for row in rows if row["result"] in FINAL])
    finally:
        memcheck.lock_org(MEM)
        print(f"{ORG} is locked again." if not STEERING_MD.exists() else f"{ORG} and {STEERING_MD} are locked again.")


def main():
    ap = argparse.ArgumentParser(allow_abbrev=False)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--done", nargs="+", metavar="ITEM")
    mode.add_argument("--yours", action="store_true")
    ap.add_argument("--of", metavar="PROPOSAL", help="with --done: the file name of the proposal the item is in, when its number waits in more than one")
    a = ap.parse_args()
    if a.of and not a.done:
        ap.error("--of goes with --done")
    if os.environ.get("AGENT_MEMORY_PROPOSE_ONLY"):
        print("READ-ONLY: this dream only proposes. Applying is started by a person.")
        sys.exit(memwrite.READ_ONLY)
    waiting = bool(a.yours or a.done)
    if waiting and not with_waiting():
        sys.exit(NONE_WAITING)
    if not waiting and not dream_proposal.open_one(MEM):
        leftovers()  # nothing is applied and nothing is locked: it says what a session can still do, or that there is nothing
        return
    proposal = None if waiting else the_proposal()  # before the lock is taken: a run with nothing to work on leaves nothing behind
    if a.dry_run:
        apply(proposal, True)
        return
    with one_apply():
        if a.yours:
            yours()
            return
        lock_org()
        if a.done:
            done(a.done, a.of)
        else:
            apply(proposal, False)


if __name__ == "__main__":
    main()

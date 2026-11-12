#!/usr/bin/env python3
"""Stops a proposal from being committed that holds what this store never keeps, or that is not written as a proposal a person can tick.

  python3 scripts/proposal_check.py [proposal]   default: the proposal of the dream in progress, which its batch names

A proposal quotes sessions, scratchpads and Claude's own notes, and it is committed to the store's
history with the file beside it (dream_proposal.side_file: what the dream read and did not propose).
memwrite.py checks a line when it is applied; by then the proposal is already in git. So every line
of both files is checked first. It prints where each problem is and what it is, never the content,
and exits 1 when there are any. dream_finish.py calls refusal() and commits nothing until it is empty.

Two kinds of problem.

What the store never keeps, in either file: a line its reject.txt or the credential pattern refuses;
an item the person has declined (dream_proposal.declined) or has approved already and that waits.
These are refused however often the dream comes back.

How the proposal is written (its shape):
- The sections come in ORDER. The Summary has SUMMARY_LINES lines at most, of SUMMARY_CHARS characters.
  What was read and not proposed is in the side file, and the Summary's `Not proposed` line counts it.
- "Needs you" holds plain lines, each a sentence and its evidence, with no tick box. Such a line is
  printed in the person's terminal later, so it is held to what memory is held to and more: nothing
  that reads as an instruction to an agent, no address to fetch, nothing piped to a shell, and no
  command but one the kit itself gives (KIT_COMMAND). What is wrong and what kind of action it
  needs is said in words. A line `- resolved: …` must name a need that is open (needs.py).
- A memory line (the quoted line of an add, edit or merge, and each fact of a carry item) is held to
  memwrite's own limit, memwrite.MAX_LINE; an edit may be as long as the line it replaces. A `why:`
  line has MAX_WHY characters at most.
- Claude's own notes. Checked facts stand under one `carry` item per note, each a line
  `+ project/<file>.md "<line>"` that ends with how it is known: `(checked: …)`, or `(by <who>, from
  Claude's own note <file>)` for something the person decided. The dream writes every sign as `+`.
  A line that says it was checked on this machine names the claim (`· machine: <n>`), that claim's
  path, phrase or command occurs in the line, and machine_check.py answered true for it in this dream.
  Facts nothing could check are the items of the last section, UNCHECKED: each an `add` whose line
  ends `(by the owner's tick, from Claude's own note <file>)`, unticked, because the person's tick is the check.
- Every note the dream was given has one row in the side file (own_notes.rows) that adds up, counts
  as many checked facts as its carry item holds, as many unchecked ones as items name it, and as many
  stale ones as lines under "Not carried over". A row of no facts for a note that holds lines says why.

A dream gets SHAPE_ROUNDS lists of shape problems. If the proposal still has one after that, the
answer is that the dream has failed, with no list: an unattended run that can't get the shape
right stops and alerts, and doesn't go round for ever.
"""
import json, os, re, sys
from pathlib import Path

import dream_apply
import dream_due
import dream_owner
import dream_proposal
import machine_check
import memwrite
import needs
import own_notes

ROOT = Path(__file__).resolve().parent.parent
MEM = Path(os.environ.get("AGENT_MEMORY_DIR", ROOT / "memory")).expanduser().resolve()
BATCH = MEM / "dreams" / "_batch"
SUMMARY_LINES, SUMMARY_CHARS = 4, 300
MAX_WHY, MAX_NEED = 300, 300
SHAPE_ROUNDS = 2  # lists of shape problems one dream is given
ROUNDS = "_shape-rounds"  # in the batch: how many it has had
OWN = "Claude's own notes"
ORDER = ("Summary", needs.HEADING, "Proposed changes", "ORG.md and Steering", OWN, "Scratchpads", "Inbox items", "Trend", dream_proposal.UNCHECKED)
MOVED = ("Rejected", "Not carried over")  # sections and lines that belong in the side file
MEMORY_LINE = ("add", "edit", "merge")  # the actions whose quoted line is a line memory will hold
NOT_CARRIED = re.compile(r"^- (?P<note>.+?) — .+ — stale: .+$")
NOT_CARRIED_FORM = "- <note file> — <the subject in a few words> — stale: <what the code shows instead, or that the path is not there>"
NO_CHECK = "Could not be checked"  # the side file's list of the facts that are items of the last section
NO_CHECK_LINE = re.compile(r"^- (?P<note>.+?) — .+ — item (?P<item>\d+)$")
NO_CHECK_FORM = "- <note file> — <the subject in a few words> — item <its number in the proposal>"
CHECKED = re.compile(r"\((?:checked:|by\s)(?:[^()]|\([^()]*\))*\)\.?$")
VOUCHED = "by the owner's tick"  # who stands for a fact nothing could check: fixed words, never a name the dream chooses
FROM_NOTE = re.compile(rf"\({VOUCHED}, from Claude's own note [^()]+\)\.?$")
ON_MACHINE = re.compile(r"on this machine\)\.?$")
COUNT = re.compile(r"^(\d+) checked facts?$")
URL = re.compile(r"https?://|\bwww\.", re.I)
TO_SHELL = re.compile(r"\|\s*(?:sudo\s+)?(?:sh|bash|zsh|fish|python\d?|perl|ruby|node)\b|\$\(|\b(?:curl|wget|sudo)\b", re.I)
SPAN = re.compile(r"`([^`]*)`")
# A script of the kit's is one in the kit's own folder, written as the kit's files write it: the name alone would pass a script of that name anywhere.
# The kit's folder is always named agent-memory-kit: ~/tools/agent-memory-kit by default, or in a clone of the release.
KIT_COMMAND = re.compile(r"^(?:python3 scripts/(\w+)\.py|amk [a-z-]+|sh (?:~/tools|~?[\w./-]*)/agent-memory-kit/(?:retire-auto-memory|run-dream|schedule-dream|sync-kit|install-store|link-worktrees)\.sh)(?:\s|$)")
RUN = re.compile(r"\b(?:run|execute|paste|type)s?:\s*(?!`)", re.I)


PROPOSAL_TEMPLATE, SIDE_TEMPLATE = "_proposal-template.md", "_side-template.md"  # in the batch: what the dream fills in


def proposal_template(proposal: str) -> str:
    """The proposal as a dream fills it in, for the batch: the sections, the shape of each kind of item and the limits this file checks."""
    side = dream_proposal.side_file(proposal)
    return f"""<!-- Fill this in and write it to memory/dreams/{proposal}. Don't copy the lines in these brackets.
The headings are exact and come in this order. Leave out a section that has nothing in it; Summary and Trend are always there, and
`## {OWN} (auto memory)` whenever you were given notes. Every numbered item is a tick line, then lines indented by six spaces, the
first starting `why:`. The numbers run on from one section to the next. <M> counts every numbered item; <N> is `sessions` in _metrics.json.
A memory line has {memwrite.MAX_LINE} characters at most with its date and ending (an edit may be as long as the line it replaces); a `why:` {MAX_WHY}.
An `edit` has `replaces: "<exact current line>"` after its why. A `delete` quotes the line to delete. A `merge` quotes the merged line, and its
`replaces:` gives each line it stands for by its opening words, enough to fit only that line, separated by ` ; `. A `split` quotes the new files, then
one line per new file: the file, a colon, the lines that move there by their opening words. An item proposed again from {dream_proposal.HANDOVER}
has `left open: <number>` after its why. Never tick anything. -->
# Dream <date> — <N> sessions, <M> proposed changes
## Summary
<{SUMMARY_LINES} lines at most, {SUMMARY_CHARS} characters each: biggest pattern · estimated wasted turns>
<memory health from _health.md (characters a session loads of the limit, the largest project/ file, stale lines, new lines that went unchecked) · the net change in entries if every item were ticked (adds, minus deletions, minus the lines each merge saves)>
{pointer(side, "<s>", "<r>")}
## {needs.HEADING}
- <what is wrong and what kind of action it needs, in words>{needs.EVIDENCE}<ids>
- {needs.RESOLVED}<a need {dream_proposal.HANDOVER} lists, in its words>{needs.EVIDENCE}<where a session shows it put right>
## Proposed changes (tick to approve)
- [ ] 1. <file> — add|delete — "<exact line>"
      why: <finding> · seen: <k>/<N> sessions · cost: ~<turns> · evidence: <ids>
- [ ] 2. <file> — edit — "<exact new line>"
      why: <finding> · seen: <k>/<N> sessions · cost: ~<turns> · evidence: <ids>
      replaces: "<exact current line>"
- [ ] 3. project/<file>.md — merge — "<the one line that replaces them>"
      why: <n> lines say the same thing · evidence: <what they share>
      replaces: <opening words of the first> ; <opening words of the second>
- [ ] 4. project/<file>.md — split — "project/<topic-a>.md, project/<topic-b>.md"
      why: <n> entries, over 60 · evidence: _health.md
      project/<topic-a>.md: <opening words of a line> ; <opening words of the next>
      project/<topic-b>.md: <the same for this file>
## ORG.md and Steering (yours to change)
- [ ] 5. <ORG.md or Steering> — add|edit|delete — "<exact new line, exact replacement line, or the line to delete>"
      why: <what is no longer true> · evidence: <the file:line or command that shows it>
      replaces: "<exact current line>"
## {OWN} (auto memory)
<one line: notes read (the `In all` line of _health.md: a note that several repos hold is one note) and facts checked, unchecked, already in memory, stale, rejected>
- [ ] 6. notes <note file> — {dream_proposal.CARRY} — <n> checked facts
      why: checked, not in memory · evidence: <note file>
      + project/<file>.md "- [<today>] <a fact as a memory line> (checked: <file:line>)"
      + project/<file>.md "- [<today>] <a fact about this machine, naming the path, phrase or command of its claim> (checked: <what the check saw>, on this machine)" · machine: <n>
      + project/decisions.md "- [<today>] Decision: <what>. (by <who>, from Claude's own note <file>)"
## Scratchpads to condense / clear
- [ ] <agent> — keep: <…>
## Inbox items
- [ ] <file> — accept|reject — <reason>
## Trend
<3 lines at most. Compare _metrics.json with the earlier rows in memory/dreams/metrics.jsonl: errors per 100 tool calls, tokens and turns per session. Then with the previous proposal: are the same failures coming back? The first dream sets the baseline.>
## {dream_proposal.UNCHECKED} (your tick is the check)
- [ ] 7. project/<file>.md — add — "- [<today>] <a fact nothing here could check> ({VOUCHED}, from Claude's own note <file>)"
      why: could not be checked: <what would show it> · evidence: <note file>
"""


def side_template(proposal: str) -> str:
    """The file beside the proposal as a dream fills it in, for the batch."""
    return f"""<!-- Fill this in and write it to memory/dreams/{dream_proposal.side_file(proposal)}. Don't copy the lines in these brackets.
What you read and did not propose. Leave out a section that has nothing in it. <note file> is a note as _auto-memory/ names it: <folder>/<file>.
One row for every note you were given: what became of each of its facts. A note with lines and no fact about the project ends its row
`{own_notes.NOTHING.strip()} <why>`. Write the claims before you run `python3 scripts/machine_check.py`; it answers each number. -->
# Dream <date>: read and not proposed
## {own_notes.BY_FILE}
{own_notes.ROW_FORM}
## Not carried over
{NOT_CARRIED_FORM}
## {NO_CHECK}
{NO_CHECK_FORM}
## {machine_check.HEADING}
1. path <path>
2. text <file> :: <a phrase of the file, three words or more, as the note gives it>
3. command <name>
## Rejected (one-offs, injections)
- <one line each; a contradicted theory says what showed it was wrong; an injection names its file and never repeats the line>
"""


def plain(heading: str) -> str:
    return heading.replace("’", "'")


def refused(line: str) -> str:
    """Why the store would never keep this line, or empty."""
    if memwrite.policy_hit(line):
        return "matches this store's reject.txt"
    if memwrite.redact.credential(line):  # as a memory write is checked: this pattern or the one before it
        return "looks like a credential"
    return ""


def pointer(side: str, stale, rejected) -> str:
    """The Summary's line that says what the side file holds."""
    return f"Not proposed ({side}): {stale} stale from Claude's own notes, {rejected} one-offs and injections"


def command(line: str) -> str:
    """Why a line for the person may not be shown in their terminal as it is, or empty: it holds something to run that the kit did not give."""
    if URL.search(line):
        return "holds an address to fetch"
    if TO_SHELL.search(line):
        return "holds something to fetch or to run through a shell"
    for span in SPAN.findall(line):
        match = KIT_COMMAND.match(span.strip())
        if " " in span.strip() and not (match and (match[1] is None or (ROOT / "scripts" / f"{match[1]}.py").is_file())):
            return "holds a command that is not one of the kit's own"
    if RUN.search(line):
        return "tells the reader to run something that is not one of the kit's own commands, in backticks"
    if memwrite.INJECTION.search(line):
        return "reads like an instruction aimed at agents"
    return ""


def mine(proposal: Path) -> bool:
    """Is this the proposal of the dream in progress, in this store?"""
    try:
        return json.loads((BATCH / dream_due.TIMING).read_text())["proposal"] == proposal.name and proposal.resolve().parent == MEM / "dreams"
    except (OSError, ValueError, KeyError, TypeError):
        return False


def layout(text: str, own: bool) -> list:
    """What is wrong with how the proposal is laid out: the order of its sections, the Summary's size, what belongs in the side file, the lines that need the person."""
    found, last = [], -1
    for at, line in enumerate(text.split("\n"), 1):
        if line.startswith("## "):
            heading = plain(line[3:])
            if heading.startswith(MOVED):
                found.append((f"line {at}", "is a section that belongs in the side file, not in the proposal a person ticks"))
            place = next((n for n, start in enumerate(ORDER) if heading.startswith(start)), None)
            if place is not None and place < last:
                found.append((f"line {at}", f"is out of order: the sections come as {', '.join(ORDER)}"))
            last = max(last, place if place is not None else last)
        elif line.startswith("- Not carried over:"):
            found.append((f"line {at}", "belongs in the side file, under `## Not carried over`"))
    summary = dream_proposal.section(text, "Summary")
    if len(summary) > SUMMARY_LINES:
        found.append((f"line {summary[SUMMARY_LINES][0]}", f"is line {SUMMARY_LINES + 1} of the Summary, which has {SUMMARY_LINES} lines at most"))
    found += [(f"line {at}", f"is a Summary line of {len(line):,} characters; {SUMMARY_CHARS} at most") for at, line in summary if len(line) > SUMMARY_CHARS]
    open_needs = {entry["key"] for entry in needs.load(MEM)} if own else None
    for at, line in dream_proposal.section(text, needs.HEADING):
        if line.startswith("      "):
            continue  # a line under a line
        if dream_proposal.ANY.match(line):
            found.append((f"line {at}", f"is a tick item under \"{needs.HEADING}\": what needs the person is never applied, so it has no tick box. "
                                         "A change to memory goes in its own section"))
        elif not line.startswith("- ") or needs.EVIDENCE not in line:
            found.append((f"line {at}", f"is not written as a line that needs the person (`- <what is wrong and what kind of action it needs>{needs.EVIDENCE}<where it shows>`)"))
        elif len(line) > MAX_NEED:
            found.append((f"line {at}", f"is {len(line):,} characters; a line under \"{needs.HEADING}\" has {MAX_NEED} at most"))
        elif command(line):
            found.append((f"line {at}", f"{command(line)}: a line under \"{needs.HEADING}\" is shown in the person's terminal, so it says in words what is wrong and "
                                         "what kind of action it needs, and names no command but the kit's own"))
        elif open_needs is not None and line[2:].startswith(needs.RESOLVED) and needs.key(line[2:]) not in open_needs:
            found.append((f"line {at}", f"says a need is resolved that is not on record as open: use the words `{dream_proposal.HANDOVER}` gives it in"))
    return found


def machine_numbers(why: str) -> list:
    """The claims of the side file a why line says confirm its item."""
    said = next((part[len("machine:"):] for part in (why or "").split(" · ") if part.startswith("machine:")), "")
    return [int(n) for n in re.findall(r"\d+", said)]


def on_machine(where: str, line: str, numbers: list, claims: dict, own: bool) -> list:
    """What is wrong with how a line says it was checked on this machine, given the claims it names."""
    found = []
    if ON_MACHINE.search(line) and not numbers:
        found.append((where, "says it was checked on this machine and names no claim: a line that ends so carries `· machine: <n>`, the number of the claim that showed it"))
    for number in numbers:
        if number not in claims:
            found.append((where, f"names check {number} on this machine, and the side file has no claim with that number"))
            continue
        kind, what = claims[number]
        about = [part.strip() for part in what.split(machine_check.WORDS)] if kind == "text" else [what]
        if not any(part and part in line for part in about):
            found.append((where, f"names check {number} on this machine, and that claim is about something the line doesn't mention: "
                                 "the claim's path, phrase or command must stand in the line it confirms"))
        elif own and not machine_check.confirmed(BATCH, claims[number]):
            found.append((where, f"names check {number} on this machine, and `python3 scripts/machine_check.py` has not answered true for that claim in this dream"))
    return found


def too_long(where: str, line: str, replaces: str = "") -> list:
    allowed = max(memwrite.MAX_LINE, len(replaces))
    return [(where, f"proposes a memory line of {len(line):,} characters; {allowed} at most"
                    + (" (an edit may be as long as the line it replaces)" if replaces else "") + ". Say the one thing a later agent needs, or make it two")] if len(line) > allowed else []


def unquoted(text: str):
    text = text.strip()
    return text[1:-1].strip() if len(text) > 1 and text[0] == text[-1] == '"' else None


def items(text: str, claims: dict, own: bool) -> tuple:
    """(what is wrong with single items, {note: checked facts in its carry item}, {note: items of the last section that name it})."""
    found, carried, unchecked = [], {}, {}
    for item in dream_apply.items(text):
        where, section = f"line {item.line + 1}", plain(item.section)
        why = item.detail("why")
        if why is not None and len(why) > MAX_WHY:
            found.append((f"line {item.line + 2}", f"is a why of {len(why):,} characters; {MAX_WHY} at most: the finding in a few words, then seen, cost and evidence"))
        if item.raw is not None or item.kind == "needs":
            continue  # what is ticked and can't be read as an item is refused when applied; a tick under what needs the person is named by layout()
        if item.kind == "carry":
            note = item.target[len("notes "):].strip() if item.target.startswith("notes ") else None
            rows = [(at, row) for at, row in enumerate(item.details, item.line + 2) if row and not row.startswith("why:")]
            facts = [(at, dream_proposal.FACT.match(row)) for at, row in rows]
            if note is None or not section.startswith(OWN):
                found.append((where, f"is a carry item, which is `notes <note file> — carry — <n> checked facts` under `## {OWN} (auto memory)`"))
            elif note in carried:
                found.append((where, f"is a second carry item for {note}: a note's checked facts stand under one item"))
            else:
                carried[note] = len(facts)
            said = COUNT.match(item.rest)
            if not said or int(said[1]) != len(facts) or not facts:
                found.append((where, f"says `{item.rest}` and has {len(facts)} fact lines under it: the item ends `<n> checked facts`, with that many lines"))
            for at, fact in facts:
                spot = f"line {at}"
                if not fact:
                    found.append((spot, "is not written as a fact of a carry item (`+ project/<file>.md \"<line>\"`, then ` · machine: <n>` when a check on this machine showed it)"))
                    continue
                sign, file, line, number = fact.groups()
                if sign != "+":
                    found.append((spot, "is written with `-`: the dream writes every fact with `+`, and only the person turns one to `-` to leave it out"))
                if not file.startswith("project/"):
                    found.append((spot, "names a file outside project/"))
                if not CHECKED.search(line):
                    found.append((spot, "does not say how it is known: a checked fact ends `(checked: …)`, or `(by <who>, from Claude's own note <file>)` for what the person decided"))
                found += too_long(spot, line) + on_machine(spot, line, [int(number)] if number else [], claims, own)
            continue
        line = unquoted(item.rest) if item.action in MEMORY_LINE and item.kind in ("memory", "org", "steering") else None
        if section.startswith(OWN):
            found.append((where, f"stands under `## {OWN} (auto memory)` and is not a carry item: a note's checked facts stand under one "
                                 f"`notes <note file> — carry — <n> checked facts`, and a fact that could not be checked under `## {dream_proposal.UNCHECKED}`"))
        if line is None:
            continue
        found += too_long(where, line, unquoted(item.detail("replaces") or "") or "" if item.action == "edit" else "")
        found += on_machine(where, line, machine_numbers(why), claims, own)
        if section.startswith(dream_proposal.UNCHECKED):
            note = next((part[len("evidence:"):].strip() for part in (why or "").split(" · ") if part.startswith("evidence:")), "")
            unchecked[note] = unchecked.get(note, 0) + 1
            if item.action != "add" or item.kind != "memory" or not FROM_NOTE.search(line):
                found.append((where, f"is a fact that could not be checked, which is an `add` to a file in project/ whose line ends `({VOUCHED}, from Claude's own note <file>)`: "
                                     "the person's tick is the check"))
            elif item.ticked:
                found.append((where, "is a fact that could not be checked and is ticked: only the person ticks one"))
    return found, carried, unchecked


def beside(proposal: Path, text: str, own: bool) -> tuple:
    """(what is wrong with the side file and with what the proposal says of it, the side file's claims about this machine)."""
    name = dream_proposal.side_file(proposal.name)
    if name is None:  # not named as a proposal, so no file is beside it
        return [], [], {}
    try:
        side = proposal.with_name(name).read_text(errors="ignore")
    except FileNotFoundError:
        side = ""
    content = [(f"{name} line {at}", why) for at, why in ((at, refused(line)) for at, line in enumerate(side.splitlines(), 1)) if why]
    rows, wrong = own_notes.rows(side)
    claims, unclear = machine_check.claims(side)
    found = [(f"{name} line {at}", why) for at, why in wrong + unclear]
    counts, carried, unchecked = items(text, claims, own)
    stale, listed = {}, {}
    for heading, pattern, form, into in (("Not carried over", NOT_CARRIED, NOT_CARRIED_FORM, stale), (NO_CHECK, NO_CHECK_LINE, NO_CHECK_FORM, listed)):
        for at, line in dream_proposal.section(side, heading):
            match = pattern.match(line)
            if match:
                into[match["note"]] = into.get(match["note"], 0) + 1
            else:
                found.append((f"{name} line {at}", f"is not written as a line of `## {heading}` ({form})"))
    rejected = sum(line.startswith("- ") for _, line in dream_proposal.section(side, "Rejected"))
    lines = {note["given"]: note["lines"] for files in own_notes.handed_to(BATCH).values() for note in files.values() if note["given"]} if own else {}
    for note, row in rows.items():
        spot = f"{name} line {row['line']}"
        if row["facts"] != row["checked"] + row["unchecked"] + row["present"] + row["stale"] + row["rejected"]:
            found.append((spot, "counts facts that the parts after it don't add up to"))
        if row["checked"] != carried.get(note, 0):
            found.append((spot, f"says {row['checked']} checked, and the proposal's carry item for that note holds {carried.get(note, 0)} facts"))
        if row["unchecked"] != unchecked.get(note, 0) or row["unchecked"] != listed.get(note, 0):
            found.append((spot, f"says {row['unchecked']} unchecked; `## {dream_proposal.UNCHECKED}` of the proposal has {unchecked.get(note, 0)} items whose evidence is that note, "
                                f"and `## {NO_CHECK}` has {listed.get(note, 0)} lines for it"))
        if row["stale"] != stale.get(note, 0):
            found.append((spot, f"says {row['stale']} stale, and `## Not carried over` has {stale.get(note, 0)} lines for that note"))
        if not row["facts"] and not row["why"] and lines.get(note):
            found.append((spot, f"says the note holds no fact, and the note has {lines[note]} lines that say something: end the row `{own_notes.NOTHING.strip()} <why>`"))
    found += [(name, f"has no row for {note}, and the proposal has a carry item for that note") for note in carried if note not in rows]
    found += [(name, f"has no row for {note or 'the note'}, which an item under `## {dream_proposal.UNCHECKED}` gives as its evidence (`evidence: <note file>` in its why)")
              for note in unchecked if note not in rows]
    found += [(name, f"has a line under `## Not carried over` for a note that has no row: {note}") for note in stale if note not in rows]
    found += [(name, f"has no row under `## {own_notes.BY_FILE}` for {note}, a note this dream was given ({own_notes.ROW_FORM})") for note in lines if note not in rows]
    if lines and not any(plain(line[3:]).startswith(OWN) for line in text.split("\n") if line.startswith("## ")):
        found.append(("the proposal", f"has no section `## {OWN} (auto memory)`, and this dream was given {len(lines)} of Claude's own notes"))
    if (stale or rejected) and pointer(name, sum(stale.values()), rejected) not in [line for _, line in dream_proposal.section(text, "Summary")]:
        found.append(("the proposal", f"does not say what {name} holds. One line of the Summary is exactly: {pointer(name, sum(stale.values()), rejected)}"))
    return content, found + counts, claims


def problems(proposal: Path) -> tuple:
    """(what the store never keeps, what is wrong with the shape): each a list of (where, what is wrong there)."""
    text = proposal.read_text(errors="ignore")
    content, no = [], dream_proposal.declined(MEM)
    approved = {entry["key"]: entry for entry in dream_proposal.waiting(MEM)}
    for number, line in enumerate(text.splitlines(), 1):
        tick = dream_proposal.TICK.match(line) or (dream_proposal.ANY.match(line) and (None, None, None, dream_proposal.ANY.match(line)[2]))
        if tick and dream_proposal.key(tick[3].strip()) in approved:
            entry = approved[dream_proposal.key(tick[3].strip())]
            content.append((f"line {number}", f"is an item the person approved already ({dream_proposal.label(entry)} of {entry['from']}) and that waits to be done: take the item out, "
                                              "so they are not asked again"))
        elif tick and dream_proposal.key(tick[3].strip()) in no:
            content.append((f"line {number}", "is an item the person passed over twice when applying, so it is declined: take the item out "
                                              "(only the person can allow it again, with `python3 scripts/dream_proposal.py forget`)"))
        elif refused(line):
            content.append((f"line {number}", refused(line)))
    own = mine(proposal)
    side, shape, _ = beside(proposal, text, own)
    return content + side, layout(text, own) + shape


def rounds(proposal: Path, shape: list) -> int:
    """How many lists of shape problems the dream in progress has now had for this proposal, this one counted. 0 for any other proposal."""
    if not shape or not mine(proposal):
        return 0
    path = BATCH / ROUNDS
    had = int(path.read_text()) if path.exists() else 0
    path.write_text(str(had + 1))
    return had + 1


def refusal(proposal: Path) -> str:
    """Why this proposal can't be committed, in words the dream can act on. Empty when it can."""
    content, shape = problems(proposal)
    if not content and not shape:
        return ""
    had = rounds(proposal, shape)
    if had > SHAPE_ROUNDS:
        return (f"{proposal.name} was given back {SHAPE_ROUNDS} times for how it is written and is still not written as a proposal. This dream has failed. "
                "Stop here: change nothing more and don't run this or the finish again. Nothing was committed and the batch was kept, "
                "so the person is alerted and the next dream reads the same sessions.")
    return (f"{proposal.name} can't be committed as it is. Put each of these right, then run this again: rewrite a line without what the store never keeps, "
            "take out an item it says is declined or approved, and move, shorten or mark what the others name"
            + (f" (list {had} of {SHAPE_ROUNDS} for how it is written: after that the dream has failed)" if had else "") + ":\n"
            + "\n".join(f"  {where}: {why}" for where, why in content + shape))


def main():
    if len(sys.argv) > 2:
        sys.exit("usage: proposal_check.py [proposal]")
    dream_owner.renew()  # the dream that owns the batch is at work: a sign of life. For anyone else it does nothing
    if len(sys.argv) == 2:
        proposal = Path(sys.argv[1])
    else:
        proposal = MEM / "dreams" / dream_due.stamp(BATCH)["proposal"]
    if not proposal.is_file():
        sys.exit(f"{proposal} is not a file.")
    stop = refusal(proposal)
    print(stop or f"{proposal.name}: nothing this store refuses, and nothing out of shape.")
    sys.exit(1 if stop else 0)


if __name__ == "__main__":
    main()

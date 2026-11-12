#!/usr/bin/env python3
"""Rebuild INDEX.md from what is actually in the store, so nobody has to maintain it.

  python3 scripts/build_index.py

memwrite.py runs this after every write and dream_finish.py after every dream. The index is
loaded into every session, so it has a fixed room in characters (ROOM), shared by all the
project/ files. What fills it:

  1. pinned lines: a dream proposed the pin and the person approved it. A pinned line ends in
     ` (pin)`, in the file itself, so it is one grep away and stays through any edit of the line.
     At most PINS lines are pinned in a store (memwrite.py refuses one more). A pinned line
     shows its cure as well as its symptom (CURE characters).
  2. every other line, newest first, by what it is about (WIDTH characters).

What doesn't fit is left out from the end of that order, never from the end of the page, and
every file keeps at least FLOOR of its best lines so no topic drops out of sight. Lines that
say the same thing (same()) show once, under the best of them, with a count; a line somebody
wrote with --distinct always has a title of its own.
The hook that loads the index (memory_context.py) asks for a smaller one when ORG.md and
Steering leave less room: the same order decides what goes.
Nothing here reads what sessions did: the order can't be moved by text a session fetched.
"""
import collections, hashlib, json, os, re
from pathlib import Path

import dream_proposal

ROOM = 4500  # characters of the whole index, shared by every project/ file (it used to be 36 titles, about 4,200 characters)
FLOOR = 3  # each file shows at least this many of its best lines, so no topic drops out of sight
PINS = 8  # lines a store can pin: with their cures they take about a third of the room
WIDTH = 90  # characters of a title that says what a line is about
CURE = 160  # characters of a title that carries the cure too
SYMPTOM = 70  # of which the symptom takes at most this, so the cure always has room
SAME = 0.3  # two lines say the same thing when they share this part of their words (shared / all the words of the two)
SHARED = 5  # and at least this many words, so two short lines about one file are not one line
MANY = 4  # a word in more than a third of a file's entries, and in at least this many, is the file's way of speaking, not what one line says
ENTRY = re.compile(r"^-\s+(?:\[(\d{4}-\d{2}-\d{2})\]\s*)?(.+)$")
PIN = re.compile(r"\s+\(pin\)$")  # how a line says it is pinned: its last words, after any evidence
QUOTED = r'"(?:[^"\\]|\\.)*"'  # a JSON string, as memwrite.py writes an entry into the log
# In a row of CHANGELOG.md: a line its writer called a lesson of its own, and an edit, which may carry such a line on.
APART = re.compile(rf" · distinct: {QUOTED}.*? · (?:added|now): ({QUOTED}) \|")
MOVED = re.compile(rf" · was: ({QUOTED}) · now: ({QUOTED}) \|")


# How a line was checked or who decided is evidence for the reader of the file, not part of its title.
# Only a closing group that itself starts with "checked:" or "by " counts; one more pair of brackets may sit inside it.
EVIDENCE = re.compile(r"\.?\s*\((?:checked:|by\s)(?:[^()]|\([^()]*\))*\)\.?$")
# Where the title ends in each usual shape: symptom → fix, Rule … Because …, Decision … Why: …
CUT = re.compile(r"→|\. Because\b|\. Why:")
WORD = re.compile(r"[^\W_]{3,}")
# Words every line has, so sharing them says nothing about two lines.
STOP = frozenset("the and for that this with from not are was but its has have had you your can will when then than them they "
                 "into out one two all any may must should would could been being which what who how why where there here their "
                 "after before only also never always still more most each other some such does did done use used using run "
                 "runs ran because while until both over under about just like same new old yes via per".split())

Line = collections.namedtuple("Line", "file date text pinned")  # one entry: its file's name, its date or "", its words without date and pin
Said = collections.namedtuple("Said", "words cure")  # what a line says, for telling whether another says the same: all its words, and those of its cure
Title = collections.namedtuple("Title", "line others")  # what the index shows for lines that say the same: the best one, and the rest


def short(text: str, width: int) -> str:
    """The text, or its start with … where it was cut. A cut never falls inside a `command`: it comes before it."""
    if len(text) <= width:
        return text
    cut = text[:width].rsplit(" ", 1)[0]
    if cut.count("`") % 2:
        cut = cut[:cut.rindex("`")].rstrip() or cut  # a command that starts the text and is longer than the width is cut after all
    return cut + "…"


def title(text: str, cure: bool = False) -> str:
    """What the index shows of a line. With `cure`, a `symptom → fix` line keeps its fix, as far as CURE characters go."""
    text = EVIDENCE.sub("", PIN.sub("", text.strip()))
    # The evidence can close the whole line or the part before the cut, so it comes off on both sides.
    head = EVIDENCE.sub("", CUT.split(text, 1)[0].strip())
    if not cure:
        return short(head, WIDTH)
    if "→" not in text or CUT.search(text)[0] != "→":
        return short(head, CURE)  # a rule or a decision is its own cure: the reason stays in the file
    head = short(head, SYMPTOM)
    return head + " → " + short(text.split("→", 1)[1].strip(), CURE - len(head) - 3)


def lines(path: Path) -> list:
    """Every entry of one file, in the file's order."""
    found = []
    for row in path.read_text(errors="ignore").splitlines():
        m = ENTRY.match(row.strip())
        if m:
            found.append(Line(path.name, m[1] or "", PIN.sub("", m[2].strip()), bool(PIN.search(m[2].strip()))))
    return found


def entries(path: Path) -> list:
    """(date, title) of every entry of one file, newest first. For whoever counts a file's entries."""
    found = [(line.date, title(line.text)) for line in lines(path)]
    # Dated lines newest first; sorted() is stable, so undated lines keep their order.
    return sorted(found, key=lambda e: e[0], reverse=True)


def words(text: str) -> frozenset:
    """The words that tell one line from another: no date, no evidence, nothing every line has, no bare number."""
    text = EVIDENCE.sub("", PIN.sub("", text.strip()))
    return frozenset(word for word in WORD.findall(text.casefold()) if word not in STOP and not word.isdigit())


def said(text: str, usual: frozenset = frozenset()) -> Said:
    """What a line says: its words, and the words of its cure (what follows the arrow; none in a line without one), without its file's `usual` words."""
    return Said(words(text) - usual, (words(text.split("→", 1)[1]) if "→" in text else frozenset()) - usual)


def usual(texts: list) -> frozenset:
    """The words most entries of one file hold: the file's subject, or a sentence shape its lines are written in.

    Lines written to one pattern share its words and nothing else, and lines about one tool all name it. Such a word
    doesn't tell two lines apart or make them the same, so it is left out before lines are compared. A word counts
    as usual when more than a third of the entries hold it and at least MANY do, so two lines in a small file still
    compare by all their words.
    """
    held = collections.Counter(word for text in texts for word in words(text))
    return frozenset(word for word, count in held.items() if count >= MANY and 3 * count > len(texts))


def alike(one: frozenset, other: frozenset, least: int) -> bool:
    shared = len(one & other)
    return shared >= least and shared >= SAME * len(one | other)


def same(one: Said, other: Said, across: bool = False) -> bool:
    """Whether two lines say the same thing. Cheap and the same every time: no model reads them.

    They share at least SHARED words, and those are at least SAME of all the words of the two. `across` files that is not enough:
    a line about how a service is built and a trap in that service name the same things. There the cures
    must be alike too, so a line without a cure is never the same as a line in another file.
    """
    return alike(one.words, other.words, SHARED) and (not across or alike(one.cure, other.cure, 1))


def key(text: str) -> str:
    """A line by what it says, whatever its date, its spacing and whether it is pinned."""
    return hashlib.sha256(" ".join(PIN.sub("", text.strip()).split()).encode()).hexdigest()[:12]


def named(cell: str):
    """The key of the entry a cell of the log quotes. None when what it quotes is not an entry."""
    found = ENTRY.match(json.loads(cell).split("\n")[0].strip())
    return key(found[2]) if found else None


def apart(mem: Path) -> set:
    """The lines (by key) somebody wrote with --distinct: they said why it is a lesson of its own, so it is never shown under another.

    Read from CHANGELOG.md, where memwrite.py records the reason beside the line. An edit carries it on to what the line became.
    """
    log, found = mem / "CHANGELOG.md", set()
    for row in log.read_text(errors="ignore").splitlines() if log.exists() else []:
        if " · distinct: " in row and APART.search(row):
            found.add(named(APART.search(row)[1]))
        elif " · was: " in row and MOVED.search(row) and named(MOVED.search(row)[1]) in found:
            found.add(named(MOVED.search(row)[2]))
    return found - {None}


def row(found: Title) -> str:
    """One title as its line of the index."""
    return "- " + title(found.line.text, found.line.pinned) + (f" (+{len(found.others)} like it)" if found.others else "")


def titles(found: list, own: set, room: int) -> list:
    """The titles that could be shown in `room` characters, best first: pinned lines, then the newest.

    A line that says the same as a title stands under it (under the best one, when several fit) and is counted there.
    It is compared with titles only, never with a line that itself stands under one, so a chain of lines each a
    little like the next is not one title. A line in `own` neither stands under a title nor takes lines under it.
    Only as many titles are made as could fit, so a store of thousands of lines costs little more than a small one:
    a line that is like no title and could not be shown is left to its file's count of the rest.
    """
    # Newest first, then the pinned: sorted() is stable, so equals stay newest first and undated lines keep the file's order.
    ordered = sorted(sorted(found, key=lambda line: line.date, reverse=True), key=lambda line: not line.pinned)
    plain = {file: usual([line.text for line in found if line.file == file]) for file in {line.file for line in found}}
    heads, says, spent, shown = [], [], 0, collections.Counter()
    for line in ordered:
        mine, alone = said(line.text, plain[line.file]), key(line.text) in own
        under = None if alone else next((head for head, theirs in zip(heads, says)
                                         if theirs and same(theirs, mine, head.line.file != line.file)), None)
        if under:
            under.others.append(line)
        elif line.pinned or shown[line.file] < FLOOR or spent <= room:
            heads.append(Title(line, []))
            says.append(None if alone else mine)
            shown[line.file] += 1
            spent += len(row(heads[-1])) + 1
    return heads


def order(shown: list) -> list:
    """The titles in the order they are given room: the pinned, then what each file lacks of its best FLOOR, then the rest; best first within each."""
    floor, seen = set(), collections.Counter()
    for at, found in enumerate(shown):
        seen[found.line.file] += 1
        if seen[found.line.file] <= FLOOR:
            floor.add(at)
    part = lambda at: 0 if shown[at].line.pinned else 1 if at in floor else 2
    return [shown[at] for at in sorted(range(len(shown)), key=part)]


def page(names: list, counts: dict, taken: list, foot: list) -> str:
    """The index with these titles on it: `taken` holds (title, its line of the index)."""
    out = ["# Memory index (generated by scripts/build_index.py, don't edit)", "",
           "Pinned lines first, with their cure, then the newest. A title that ends in … is cut: the file has the whole line. "
           "For anything else: `grep -i \"<word>\" memory/project/*.md`."]
    stood_for = collections.Counter(line.file for found, _ in taken for line in [found.line] + found.others)
    for name in names:
        out += ["", f"## `project/{name}` ({counts[name]} entries)"]
        out += [text for found, text in taken if found.line.file == name]
        # A line that stands under a title that is shown is accounted for there, whichever file it is in.
        if counts[name] > stood_for[name]:
            out.append(f"- {counts[name] - stood_for[name]} more: grep the file")
    return "\n".join(out + foot) + "\n"


def working(mem: Path) -> list:
    """The last section of the index: where the working files are."""
    agents = sorted(p.parent.name for p in (mem / "agents").glob("*/scratch.md") if p.parent.name != "_template")
    inbox = [p for p in (mem / "inbox").glob("*.md")]
    # The index is built inside every memory write, so it never depends on the record of proposals being readable:
    # a record that isn't is said here, in memcheck and by the daily check, and the write goes on.
    broken = dream_proposal.problem(mem)
    latest = None if broken else dream_proposal.newest(mem)
    return ["", "## Working files",
            "- `agents/<name>/scratch.md`: agents with notes: " + (", ".join(agents) or "none yet"),
            f"- `inbox/`: {len(inbox)} proposal(s) waiting for dreaming",
            "- `dreams/`: " + ("the record of proposals (`dreams/proposals.json`) can't be read: run `python3 scripts/memcheck.py`" if broken
                             else f"latest proposal is `{latest['name']}`" if latest else "no dreams yet")]


def fitted(mem: Path, room: int = ROOM) -> tuple:
    """(the index with as many titles as fit in `room` characters, how many that is).

    The page without any title is the least there is: it comes back as it is, even when it is longer than `room`.
    """
    paths = sorted((mem / "project").glob("*.md"))
    found = [line for path in paths for line in lines(path)]
    counts = collections.Counter(line.file for line in found)
    ranked = [(shown, row(shown)) for shown in order(titles(found, apart(mem), room))]
    names, foot = [path.name for path in paths], working(mem)
    for keep in range(len(ranked), -1, -1):  # the most titles that fit: what goes is the end of the order
        text = page(names, counts, ranked[:keep], foot)
        if len(text) <= room or not keep:
            return text, keep


def render(mem: Path) -> str:
    return fitted(mem)[0]


def write(mem: Path) -> Path:
    path = mem / "INDEX.md"
    tmp = path.with_name("INDEX.md.tmp")
    tmp.write_text(render(mem))
    os.replace(tmp, path)
    return path


if __name__ == "__main__":
    root = Path(__file__).resolve().parent.parent
    print(write(Path(os.environ.get("AGENT_MEMORY_DIR", root / "memory")).expanduser().resolve()))

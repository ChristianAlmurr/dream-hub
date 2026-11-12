#!/usr/bin/env python3
"""Memory writes with provenance (versioning + concurrency + permissioning).

  python3 scripts/memwrite.py read  project/gotchas.md
      -> prints the file, then a final line:  HASH: <sha>
  python3 scripts/memwrite.py write project/gotchas.md --expect <sha> --agent <name> --why '<one line>' [--append [--distinct '<why>'] | --force] <<'MEMWRITE'
  - [2026-11-02] the new line
  MEMWRITE
  python3 scripts/memwrite.py edit project/gotchas.md --agent <name> --why '<what showed it was wrong>' <<'MEMWRITE'
  - [2026-11-02] the line as it is now, word for word
  =>
  - [2026-11-02] the line as it should read
  MEMWRITE
  python3 scripts/memwrite.py delete project/gotchas.md --agent <name> --why '<why it goes>' <<'MEMWRITE'
  - [2026-11-02] the line as it is now, word for word
  MEMWRITE
  python3 scripts/memwrite.py undo project/gotchas.md --row <line number of the row in CHANGELOG.md> --agent <name> --why '<one line>'

Memory text always comes on stdin and never as an argument: a line of memory holds backticks,
`$` and apostrophes, and the shell would run them. The why goes in single quotes for the same
reason, so it holds no apostrophe. The heredoc's delimiter is quoted, so the shell
passes the text through untouched. For an edit the old and the new entry are parted by a line
that holds only `=>`. No line of an entry can be that line or the delimiter, because every line
of an entry starts with `- ` or is indented.

Paths are relative to the memory dir (a leading "memory/" is accepted too).
Memory dir = $AGENT_MEMORY_DIR if set, else <repo>/memory. Point every worktree at the
same dir (see README) so parallel agents share one store and one lock.

Two ways to say what you expect to find, checked under the store's one lock:
  - write presents the hash of the whole file (--expect). Anything written since: STALE.
  - edit and delete present the entry itself. An entry is a list item: a line that starts with
    `- `, with the lines indented under it. It must be in the file exactly once, word for word,
    at the moment of the write; only that text is replaced or removed. So two agents correcting
    different lines of one file both succeed without a retry, and one who presents a line somebody
    else has changed gets STALE and nothing is written. There is no --expect on these two.

Rules:
  - STALE (exit 3): the file changed since you read it, or the line you named is not in it now. Re-read, merge, retry.
  - DENIED (exit 4): outside project/ agents/ inbox/, or not .md. ORG.md is human-only.
  - REJECTED (exit 5): looks like a secret or an injected instruction, holds a control character, is not in the
    form above, or a replace would delete >50% of the file without --force. --force is only for approved dream
    changes, so it needs --agent dreamer.
  - READ-ONLY (exit 6): this run can't write to the store: the nightly sandbox, or a scheduled dream, which only proposes.
  - FAILED (exit 7): git could not commit the write, so it was undone: nothing was written or logged. Write it again.
  - AMBIGUOUS (exit 8): the line you named is in the file more than once, word for word.
  - SIMILAR (exit 9): an append to project/, or an edit there, would leave a line that says what another line of memory
    already says (build_index.same). Nothing was written. The lines like it are shown, the ones in the same file first,
    with the edit that corrects one of them. A lesson that really is another one goes in with the same command and
    --distinct '<why it is a different lesson>': the reason is kept in the log beside the line, and the index gives
    that line a title of its own. An entry that is in the file already but for its date is REJECTED either way.
A line of project/ that ends in ` (pin)` is pinned: the index always shows it, with its cure (build_index.py). A dream
proposes the pin and the person approves it, so the words are put on or taken off only by --agent dreamer, with an edit
that changes nothing else. Every other edit keeps a line pinned or unpinned as it was, whoever makes it and whether or
not the new text carries the words. A pinned line is named with or without them, by edit, delete and a dream's items.
At most build_index.PINS lines are pinned in a store.
Every accepted write is logged to CHANGELOG.md and committed (that file, the log and the
regenerated INDEX.md) if the memory dir is inside a git repo. A write is whole or it didn't
happen, for every verb: whatever stops it after the file is written, a commit that fails
included, the file, the log and the index go back to the bytes they held. The why cell of an edit's row ends
` · was: "<old>" · now: "<new>"`; of a delete's, ` · removed: "<old>" · after: "<the line above it>"`; of an
append of one entry, ` · added: "<new>"`. The old text is the entry as the file held it, its pin included. So a
removal is never silent, change() reads the texts back exactly, and undo puts an edit back in place, a deleted
entry back under the line it stood under, and takes an appended entry out again (as a delete, whatever was
appended after it). A row for a line written with --distinct holds, before those, ` · distinct: "<the reason>"`.
A file keeps its own line endings.
"""
import argparse, collections, datetime, difflib, errno, hashlib, json, os, re, sys, unicodedata
from pathlib import Path

import build_index
import memcheck
import redact

ROOT = Path(__file__).resolve().parent.parent
MEM = Path(os.environ.get("AGENT_MEMORY_DIR", ROOT / "memory")).expanduser().resolve()
WRITABLE = [MEM / "project", MEM / "agents", MEM / "inbox"]
LOG = MEM / "CHANGELOG.md"
AGENT_NAME = re.compile(r"^[\w.-]{1,64}$")
MAX_WRITE = 20_000  # a lesson is a line; only an approved dream rewrites a whole file
# A line of project/ or ORG.md that is new. The index shows the start of a line as its title and a grep the whole of it; of 218 lines
# in six stores the median had 311 characters and a fifth passed this. A line that is there and longer stays, and may be edited down.
MAX_LINE = 400
SECRET = redact.SECRET  # one pattern for everything the kit keeps: memory, proposals, Claude's own notes, the session archive, the dream batch


# Every session loads titles from this store, so a poisoned line would reach all agents.
INJECTION = re.compile(r"(?i)(ignore|disregard|forget)\s+(all\s+|any\s+|your\s+)?(previous|prior|above|earlier)\s+(instructions?|prompts?|rules?)"
                       r"|do\s+not\s+(tell|inform)\s+the\s+user|(curl|wget)\s+[^\n|]*\|\s*(ba|z)?sh\b|new\s+system\s+prompt")
STALE, DENIED, REJECTED, READ_ONLY, FAILED, AMBIGUOUS, SIMILAR = 3, 4, 5, 6, 7, 8, 9  # the exit codes
PINNER = "dreamer"  # who applies what the person approved in a dream, a pin among it
PINNED = " (pin)"  # the words build_index.PIN reads
SHOWN = 3  # lines like a new one that a held append shows
# Words that restate --distinct. A reason must hold three words besides these: nobody judges it, but it is logged, and a dream reads it.
SAYS_NOTHING = frozenset("distinct distinctly different differently differs differ difference lesson lessons same another other others separate unique "
                         "new own not its this that these those line lines one ones from the and are was has just really very quite thing".split())
# Everything but tab and newline that ends a line for some reader or can't be seen: a carriage return
# would put a second line into one entry, U+2028 or U+0085 would break a log row in two, and a
# zero-width character or a direction override makes a line read as something it isn't.
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u200b-\u200f\u2028-\u202e\u2060-\u2064\u2066-\u2069\ufeff]")
NOT_REPEATED = "[content this store refuses, not repeated]"
SEPARATOR = "=>"  # between the old and the new entry of an edit, on a line of its own
DELIMITER = "MEMWRITE"
MARKS = (" · was: ", " · now: ", " · removed: ", " · after: ", " · restored: ", " · distinct: ", " · added: ")  # how the log marks a change
QUOTED = r'"(?:[^"\\]|\\.)*"'  # a JSON string, as quoted() writes one
CHANGE = re.compile(rf"^(.*?)(?: · was: ({QUOTED}) · now: ({QUOTED})| · removed: ({QUOTED}) · after: ({QUOTED})"
                    rf"| · restored: ({QUOTED}) · after: ({QUOTED})| · added: ({QUOTED}))?$")
FORMS = {
    "edit": f"python3 scripts/memwrite.py edit <file> --agent <name> --why '<one line>' <<'{DELIMITER}'\n"
            f"<the entry as it is, word for word>\n{SEPARATOR}\n<the entry as it should read>\n{DELIMITER}",
    "delete": f"python3 scripts/memwrite.py delete <file> --agent <name> --why '<one line>' <<'{DELIMITER}'\n"
              f"<the entry as it is, word for word>\n{DELIMITER}",
}

Written = collections.namedtuple("Written", "before after commit warning")


class Uncommitted(Exception):
    """git could not commit a write."""


class Refused(Exception):
    """A write the store does not take. `code` is the exit code, the message starts with its label."""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


def h(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()[:12] if p.exists() else "new"


def read(p: Path):
    """(the file's text with \\n line ends, the line ending the file itself uses)."""
    text = p.read_bytes().decode() if p.exists() else ""
    return text.replace("\r\n", "\n"), "\r\n" if "\r\n" in text else "\n"


def resolve(path: str, agent: str = None) -> Path:
    """The file in the store that `path` names, if agents may write it (and, with `agent`, if this one may)."""
    path = re.sub(r"^(\./)?memory/", "", path)
    p = (Path(path) if os.path.isabs(path) else MEM / path).resolve()
    if p.suffix != ".md" or not any(p.is_relative_to(w) for w in WRITABLE):
        raise Refused(DENIED, f"DENIED: {path} is not writable (allowed: project/, agents/, inbox/ *.md). "
                              "ORG-level changes go to inbox/ as a proposal.")
    scratch = re.match(r"agents/([^/]+)/", str(p.relative_to(MEM)))
    if agent and scratch and agent not in (scratch[1], "dreamer"):
        raise Refused(DENIED, f"DENIED: agents/{scratch[1]}/ is that agent's own scratchpad.")
    return p


def policy_hit(text: str) -> str:
    """A store can list what must never be kept in it: reject.txt, one regular expression per line, human-edited."""
    return redact.refused(text, redact.rules(MEM))


def refusal(text: str) -> str:
    """Why this text may not be kept in the store, or empty. One set of checks for every route in."""
    if policy_hit(text):
        return "REJECTED: this store's reject.txt says this kind of content is never kept here."
    if redact.credential(text):  # this pattern or the one before it: what was refused then is refused now
        return "REJECTED: content looks like a credential. Memory must never hold secrets."
    if INJECTION.search(text):
        return "REJECTED: this reads like an instruction aimed at agents. Memory holds observations; describe what happened instead."
    return ""


def unseen(text: str) -> str:
    """Why this text can't be written as it is: a character that ends a line for some reader, or that nobody can see."""
    found = CONTROL.search(text)
    return (f"REJECTED: the text holds a control character, or one that can't be seen or turns the reading direction "
            f"(U+{ord(found[0]):04X}). Memory is plain lines; take it out.") if found else ""


# ── entries: the unit edit and delete work on ──
def depth(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


def is_item(line: str) -> bool:
    return line.lstrip(" \t").startswith("- ")


def span(lines: list, at: int) -> int:
    """Where the entry that starts at lines[at] ends: it takes the lines indented under it, up to a blank one."""
    end = at + 1
    while end < len(lines) and lines[end].strip() and depth(lines[end]) > depth(lines[at]):
        end += 1
    return end


def shape(entry: str) -> list:
    """An entry as its lines, without trailing whitespace. Whitespace at a line's end can't be seen, so it never counts."""
    return [line.rstrip() for line in entry.strip("\n").split("\n")]


def whole(entry: str) -> str:
    return "\n".join(shape(entry))


def spans(text: str) -> list:
    """(start, end) of every entry in the text. A heading or a comment is not one. A nested item is one, inside its parent's."""
    lines = text.split("\n")
    return [(at, span(lines, at)) for at in range(len(lines)) if is_item(lines[at])]


def entries(text: str) -> list:
    """Every entry of the text, whole, in order."""
    lines = text.split("\n")
    return ["\n".join(line.rstrip() for line in lines[start:end]) for start, end in spans(text)]


def one_entry(text: str, what: str = "the text") -> str:
    """The text, checked to be one entry: a list item, and any further lines indented under it."""
    lines = shape(text)
    if not is_item(lines[0]):
        raise Refused(REJECTED, f"REJECTED: {what} is not an entry. An entry is a list item: a line that starts with `- `, with any "
                                "further lines indented under it. A heading or a comment is changed with a whole-file write.")
    if any(not line or depth(line) <= depth(lines[0]) for line in lines[1:]):
        raise Refused(REJECTED, f"REJECTED: {what} is more than one entry. One entry is one list item, with any further lines indented "
                                "under it. A second line of its own is added with write --append.")
    return "\n".join(lines)


def unpinned(entry: str) -> str:
    """An entry without the words that pin it."""
    first, sep, rest = whole(entry).partition("\n")
    return build_index.PIN.sub("", first) + sep + rest


def find(text: str, entry: str) -> list:
    """(start, end) of each place the text holds exactly this entry, pinned or not: the pin is not part of what a line says."""
    lines, wanted = text.split("\n"), unpinned(entry)
    return [(start, end) for start, end in spans(text) if unpinned("\n".join(lines[start:end])) == wanted]


def plain(text: str) -> str:
    """The text as it looks: composed and decomposed letters alike, a no-break space as a space, nothing invisible."""
    return "".join(c for c in unicodedata.normalize("NFKC", text) if unicodedata.category(c) != "Cf")


def near(text: str, entry: str, but: str = None) -> list:
    """(line number, line) of what the file holds that could be the entry meant: the same first line, or one close to it.

    `but` is an entry that doesn't count: the new line of an edit is always close to the old one.
    """
    lines, head = text.split("\n"), shape(entry)[0]
    stem = re.sub(r"(…|\.\.\.)$", "", head).rstrip()  # a line quoted by its start, with or without an ellipsis at the cut
    skip = set(shape(but)) if but is not None else set()
    numbered = {}
    for at, line in enumerate(lines):
        if line.strip() and line.rstrip() not in skip:
            numbered.setdefault(line.rstrip(), at + 1)

    def like(line: str) -> float:
        """How much of the given text this line holds, measured over the line's start of the same length: a long line named by its opening words is that line."""
        if line == head or plain(line) == plain(head):
            return 1.0
        return difflib.SequenceMatcher(None, stem, line[:len(stem)]).ratio()  # 1.0 for a line that starts with the given text

    ranked = sorted(numbered, key=like, reverse=True)[:3]
    return [(numbered[line], line) for line in ranked if like(line) >= 0.6]


def missing(text: str, entry: str, name: str, but: str = None) -> Refused:
    """The refusal for an entry that is not in the file as given, saying what the file holds instead."""
    lines, wanted = text.split("\n"), shape(entry)
    close = near(text, entry, but)
    for number, line in close:
        if line == wanted[0] and len(wanted) == 1 and is_item(line) and span(lines, number - 1) > number:
            listed = "\n".join(lines[number - 1:span(lines, number - 1)])
            return Refused(STALE, f"STALE: that line is in {name} with lines indented under it, and an entry is named whole. Nothing was written. The entry:\n{listed}")
        if line != wanted[0] and plain(line) == plain(wanted[0]):
            at = next((i for i, (a, b) in enumerate(zip(line, wanted[0])) if a != b), min(len(line), len(wanted[0])))
            have = f"U+{ord(line[at]):04X}" if at < len(line) else "nothing more"
            gave = f"U+{ord(wanted[0][at]):04X}" if at < len(wanted[0]) else "nothing more"
            return Refused(STALE, f"STALE: line {number} of {name} looks the same but is not: the difference is in characters that look alike or "
                                  f"can't be seen. At character {at + 1} the file has {have} and you gave {gave}. Nothing was written. "
                                  "Copy the line out of the file itself.")
    listed = "".join(f"\n  line {number}: {json.dumps(line, ensure_ascii=False)}" for number, line in close)
    return Refused(STALE, f"STALE: that line is not in {name} now. Nothing was written. Re-read the file."
                          + (f" The closest lines there:{listed}" if close else " Nothing there is like it."))


def locate(text: str, entry: str, name: str):
    """The one place this entry is, or the reason there isn't one, with what the file holds instead."""
    found = find(text, entry)
    if len(found) == 1:
        return found[0]
    if found:
        where = " and ".join(str(start + 1) for start, _ in found)
        raise Refused(AMBIGUOUS, f"AMBIGUOUS: that line is in {name} {len(found)} times, word for word (lines {where}). "
                                 "Nothing was written. Copies of one line come out with a whole-file write: read, then write --expect.")
    raise missing(text, entry, name)


def above(lines: list, start: int) -> str:
    """The line an entry stands under: the nearest line above it that isn't blank. Empty at the top of the file."""
    return next((lines[at].rstrip() for at in range(start - 1, -1, -1) if lines[at].strip()), "")


def appended(old: str, new: str) -> str:
    return (old.rstrip("\n") + "\n" if old else "") + new.strip("\n") + "\n"


# ── pins, and lines that say what memory already says ──
def top(text: str) -> list:
    """(line number, entry) of every entry of the text that is nobody's sub-item."""
    lines = text.split("\n")
    return [(start + 1, "\n".join(line.rstrip() for line in lines[start:end])) for start, end in spans(text) if not depth(lines[start])]


def is_pinned(entry: str) -> bool:
    return bool(build_index.PIN.search(shape(entry)[0]))


def pins(text: str) -> list:
    """The pinned entries of a text, whole."""
    return [entry for _, entry in top(text) if is_pinned(entry)]


def undated(entry: str) -> str:
    """An entry as what it says: without its date, its pin and its spacing. Two entries that are equal here are one entry written twice."""
    lines = shape(entry)
    found = build_index.ENTRY.match(lines[0].strip())
    return " ".join(" ".join([build_index.PIN.sub("", found[2].strip()) if found else lines[0]] + lines[1:]).split())


def twice(old: str, new: str, name: str):
    """Refuses an append that holds an entry the file already has, whatever date each carries."""
    have = {}
    for number, entry in top(old):
        have.setdefault(undated(entry), number)
    for _, entry in top(new):
        if undated(entry) in have:
            raise Refused(REJECTED, f"REJECTED: {name} already holds that entry, at line {have[undated(entry)]}: a date of its own doesn't make it a new one. "
                                    "Nothing was written. If something about it changed, edit that line.")


def alike(p: Path, text: str, but: int = None) -> list:
    """(file name, line number, entry) of the lines in project/ that say what an entry of `text` says: this file's first, the closest first.

    `but` is a line number of this file that doesn't count: the line an edit is changing.
    """
    files = {path: top(read(path)[0]) for path in sorted((MEM / "project").glob("*.md"), key=lambda path: (path != p, path.name))}
    usual = {path: build_index.usual([undated(entry) for _, entry in entries]) for path, entries in files.items()}  # what each file's lines all say
    mine = [build_index.said(undated(entry), usual.get(p, frozenset())) for _, entry in top(text)]
    found = []
    for path, entries in files.items():
        close = []
        for number, entry in entries:
            theirs = build_index.said(undated(entry), usual[path])
            score = max((len(theirs.words & one.words) / len(theirs.words | one.words) for one in mine
                         if build_index.same(theirs, one, path != p)), default=0)
            if score and not (path == p and number == but):
                close.append((-score, number, entry))
        found += [(str(path.relative_to(MEM)), number, entry) for _, number, entry in sorted(close)]
    return found


def held(found: list) -> Refused:
    """The refusal for a line that says what these lines of memory already say: what to do first, then the lines, then the way past."""
    name = found[0][0]
    listed = "".join(f"\n{file}, line {number}:\n{entry}" for file, number, entry in found[:SHOWN])
    more = f"\n(and {len(found) - SHOWN} more like these)" if len(found) > SHOWN else ""
    return Refused(SIMILAR, "SIMILAR: nothing was written. Memory already has a line like this one. If it is the same lesson, correct that line "
                            "instead of adding a second one:\n"
                            f"python3 scripts/memwrite.py edit {name} --agent <name> --why '<what changed>' <<'{DELIMITER}'\n"
                            f"<the line shown below, exactly>\n{SEPARATOR}\n<the line as it should read now>\n{DELIMITER}{listed}{more}\n"
                            "If yours is a different lesson, say what makes it one: the same command again with "
                            "--distinct '<why it is a different lesson>'.")


def apart(distinct, text: str):
    """The log's words for a line its writer calls a lesson of its own ('' when they didn't): the reason, checked like a why."""
    if distinct is None:
        return ""
    if len(top(text)) != 1:
        raise Refused(REJECTED, "REJECTED: --distinct goes with one line: it says why that line is a lesson of its own. Nothing was written.")
    why = reason(distinct, "--distinct", "Say in one line why this is a different lesson from the lines like it.")
    if len([word for word in build_index.WORD.findall(why.casefold()) if word not in SAYS_NOTHING]) < 3:
        raise Refused(REJECTED, f"REJECTED: --distinct takes a reason of its own: what this line is about that the line like it is not. "
                                f"\"{why}\" only says that it is different. Nothing was written.")
    return f" · distinct: {quoted(why)}"


def pin_room(p: Path, body: str):
    """Refuses a write that would pin one line more than the index has room for."""
    if not in_project(p):
        return
    elsewhere = [entry for path in (MEM / "project").glob("*.md") if path != p for entry in pins(read(path)[0])]
    before, after = set(elsewhere + pins(read(p)[0])), set(elsewhere + pins(body))  # an entry on its way from one file to another is one entry
    if len(after) > build_index.PINS and len(after) > len(before):
        listed = "".join(f"\n{entry.splitlines()[0]}" for entry in sorted(before))
        raise Refused(REJECTED, f"REJECTED: {len(before)} lines are pinned, and {build_index.PINS} is the most the index has room for. Nothing was written. "
                                f"Unpin one first: an edit that takes `{PINNED.strip()}` off its end. Pinned now:{listed}")


# ── the log ──
def quoted(text: str) -> str:
    """Text as one table cell on one line that reads back exactly. What the store refuses is not copied into its log either."""
    cell = json.dumps(NOT_REPEATED if refusal(text) else text, ensure_ascii=False).replace("|", "\\u007c")
    return CONTROL.sub(lambda found: f"\\u{ord(found[0]):04x}", cell)


def change(cell: str) -> dict:
    """The why cell of a CHANGELOG row, taken apart: why, the text before and after, and the line a removed or restored entry stood under."""
    why, was, now, removed, under, restored, back, added = CHANGE.match(cell).groups()
    text = lambda cell: json.loads(cell) if cell else None
    return {"why": why, "was": text(was or removed), "now": text(now or restored), "after": text(under or back), "added": text(added)}


def rows(name: str) -> dict:
    """The cells of every log row for this file, by the row's line number in the log. The log only grows, so a number names one row for good."""
    found = {}
    for number, line in enumerate(LOG.read_text().split("\n") if LOG.exists() else [], 1):
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if line.startswith("| ") and len(cells) == 6 and cells[1] == name:
            found[number] = cells
    return found


def taken_out(name: str, entry: str) -> bool:
    """Whether the log's last word on this exact entry is that it left this file: deleted, or edited into something else.

    A later row that puts it back (an undo, an edit into it) ends that: the entry is in the file again as far as the log knows.
    """
    wanted, out = unpinned(entry), False
    for found in (change(cells[4]) for cells in rows(name).values()):
        if found["now"] is not None and unpinned(found["now"]) == wanted:
            out = False
        elif found["was"] is not None and unpinned(found["was"]) == wanted:
            out = True
    return out


def actor(agent: str, session: str, why: str) -> str:
    """Who writes and why, checked before anything is read. Returns the why as it goes in the log."""
    if os.environ.get("AGENT_MEMORY_PROPOSE_ONLY"):
        raise Refused(READ_ONLY, "READ-ONLY: this dream only proposes. Put the change in the proposal.")
    if not AGENT_NAME.match(agent) or not AGENT_NAME.match(session):
        raise Refused(DENIED, "DENIED: --agent and --session may hold letters, digits, dots, dashes and underscores only.")
    return reason(why, "--why", "Say in one line why this changes.")


def reason(text: str, flag: str, ask: str) -> str:
    """One line a writer gives for a write, as it goes in the log."""
    text = " ".join(text.split()).replace("|", "/")
    if not text:
        raise Refused(REJECTED, f"REJECTED: {flag} is empty. {ask}")
    if refusal(text) or unseen(text):
        raise Refused(REJECTED, refusal(text) or unseen(text))
    if any(mark in text + " " for mark in MARKS):
        raise Refused(REJECTED, f"REJECTED: {flag} holds " + ", ".join(f"`{mark}`" for mark in MARKS[:-1]) + f" or `{MARKS[-1]}`. "
                                "That is how the log marks what a write changed, so a reason can't say it.")
    return text


def record(p: Path, body: str, before: str, agent: str, session: str, why: str, detail: str = "", eol: str = "\n") -> Written:
    """The file, its log row, the index and the commit, all of them or none. The caller holds the store's lock and has checked the text.

    This is the one place a write, an edit, a delete and an undo reach the disk, so one discipline holds for all four,
    and so does the one check that is about the store as a whole: how many lines are pinned.
    """
    pin_room(p, body)
    p.parent.mkdir(parents=True, exist_ok=True)
    index = MEM / "INDEX.md"
    was, logged = p.read_bytes() if p.exists() else None, LOG.stat().st_size if LOG.exists() else None
    indexed = index.read_bytes() if index.exists() else None
    try:
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_bytes(body.replace("\n", eol).encode())
        os.replace(tmp, p)
        after = h(p)
        when = datetime.datetime.now().isoformat(timespec="seconds")
        if not LOG.exists():
            LOG.write_text("# Memory changelog (append-only, written by scripts/memwrite.py)\n\n"
                           "| when | file | agent | session | why | hash before → after |\n|---|---|---|---|---|---|\n")
        with open(LOG, "a") as f:
            f.write(f"| {when} | {p.relative_to(MEM)} | {agent} | {session} | {why}{detail} | {before} → {after} |\n")
        build_index.write(MEM)
        commit = ""
        if memcheck.git(MEM, "rev-parse", "--is-inside-work-tree").returncode == 0:
            # pathspec commit: never sweeps up anything else that happens to be staged
            failed = memcheck.commit(MEM, f"memory({agent}): {why}\n\nsession: {session}", [str(p), str(LOG), str(index)])
            if failed:
                raise Uncommitted(failed)
            commit = memcheck.git(MEM, "rev-parse", "--short", "HEAD").stdout.strip()
    except BaseException as stop:
        # A write is whole or it didn't happen: whatever stops it after the file is written, a commit that fails included,
        # the file, the log and the index go back to the bytes they held, so no line is on disk that is not logged, indexed
        # and committed, and a retry writes it once.
        for path, held in ((p, was), (index, indexed)):
            if held is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(held)  # straight into the file: whatever stopped the write may be what stops a temporary file too
        if logged is None:
            LOG.unlink(missing_ok=True)
        else:
            os.truncate(LOG, logged)
        memcheck.git(MEM, "reset", "-q", "--", str(p), str(LOG), str(index))  # what a failed commit may have staged; the files already hold what is committed
        if isinstance(stop, Uncommitted):
            raise Refused(FAILED, f"FAILED: git could not commit this write ({stop}), so it was undone: nothing was written or logged. Write it again. "
                                  "If it keeps failing and no git command is running in the store, a git that was killed left memory/.git/index.lock behind: "
                                  "tell the person to delete that file.")
        raise
    return Written(before, after, commit, "")


# ── what a write leaves in the file. Pure text, every content check, no lock: a dry run uses these too. ──
def content(new: str, agent: str, force: bool = False):
    """What is wrong with the text of a write whatever the file holds. Said before the hash is looked at."""
    if force and agent != "dreamer":
        raise Refused(DENIED, "DENIED: --force is only for changes you approved in a dream (--agent dreamer).")
    if not new.strip():
        raise Refused(REJECTED, "REJECTED: empty content on stdin.")
    if len(new) > MAX_WRITE and agent != "dreamer":
        raise Refused(REJECTED, f"REJECTED: {len(new)} characters is too much for one write. Memory holds short lessons.")
    if refusal(new) or unseen(new):
        raise Refused(REJECTED, refusal(new) or unseen(new))


def too_long(new: str, name: str, there=(), allowed: int = None):
    """Refuses a line of shared memory that is new and longer than a memory line may be. One rule for agents and dreams.

    New is what memory does not hold yet: `there` are the lines of the file being written, and a line
    some other file of project/ holds word for word is one being moved (a split), not written. `allowed`
    is raised by an edit to the length of the line it replaces, so a long line can be corrected in place.
    """
    if not (name.startswith("project/") or name == "ORG.md"):
        return
    allowed = MAX_LINE if allowed is None else allowed
    unpinned = lambda row: re.sub(r" \(pin\)$", "", row)  # the mark that pins a line is not part of what the line says, and is not counted
    long = [row.rstrip() for row in new.split("\n") if len(unpinned(row.rstrip())) > allowed and row.rstrip() not in there]
    if long:
        held = {row.rstrip() for path in (MEM / "project").glob("*.md") for row in path.read_text(errors="ignore").split("\n")}
        long = [row for row in long if row not in held]
    if long:
        raise Refused(REJECTED, f"REJECTED: a line of {len(unpinned(long[0]))} characters; a memory line has {allowed} at most. "
                                "Say the one thing a later agent needs, or write it as two lines. Nothing was written.")


def written(old: str, new: str, agent: str, name: str, append: bool = False, force: bool = False, project: bool = False) -> str:
    """What a file holds after an append or a whole-file write. `project` says the file is one of project/, where a line can be pinned."""
    new = new.replace("\r\n", "\n")
    content(new, agent, force)
    too_long(new, name, there={row.rstrip() for row in old.split("\n")})
    if append:
        if project and agent != PINNER and pins(new):
            raise Refused(REJECTED, f"REJECTED: the new line ends in `{PINNED.strip()}`. A pin is proposed by a dream and approved by the person; "
                                    "write the line without it.")
        if project:  # a scratchpad or the inbox is the writer's own running notes: a note may come back
            twice(old, new, name)
        return appended(old, new)
    body = new if new.endswith("\n") else new + "\n"
    if project and agent != PINNER and sorted(pins(old)) != sorted(pins(body)):
        raise Refused(REJECTED, f"REJECTED: this write changes which lines of {name} are pinned or what a pinned line says. A whole-file write "
                                "leaves pinned lines as they are; correct one with edit, which keeps its pin.")
    if old and len(body) < 0.5 * len(old) and not force:
        raise Refused(REJECTED, f"REJECTED: replace shrinks {name} from {len(old)} to {len(body)} chars. "
                                "Use --append, or --force if the cut is intended (dreaming).")
    return body


def edited(old: str, entry: str, new: str, agent: str, name: str, project: bool = False, held: bool = False):
    """(the text with `new` in the place of the one entry, the entry as the file held it, `new` as it was written).

    In project/ an edit changes what a line says or whether it is pinned, never both: the pin goes on or off with an
    edit that changes nothing else, and any other edit leaves the line pinned or unpinned as it was.
    `held`: the log shows the store held `new` before (an undo), so its length is not new.
    """
    entry, new = one_entry(entry, "the entry to change"), one_entry(new, "the new text")
    start, end = locate(old, entry, name)
    lines = old.split("\n")
    there = "\n".join(line.rstrip() for line in lines[start:end])
    if project and unpinned(there) == unpinned(new) and is_pinned(there) != is_pinned(new):
        if agent != PINNER:
            raise Refused(REJECTED, f"REJECTED: this edit only puts `{PINNED.strip()}` on a line or takes it off. A pin is proposed by a dream "
                                    "and approved by the person. Nothing was written.")
    elif project:
        if is_pinned(new) and not is_pinned(there):
            raise Refused(REJECTED, f"REJECTED: the new line ends in `{PINNED.strip()}` and the line it replaces is not pinned. A pin goes on with an "
                                    "edit of its own that changes nothing else, proposed by a dream and approved by the person. Nothing was written.")
        first, sep, rest = unpinned(new).partition("\n")
        new = first + (PINNED if is_pinned(there) else "") + sep + rest  # a correction changes what a line says, never whether it is pinned
    content(new, agent)
    if not held:  # an edit may be as long as the line it replaces
        too_long(new, name, allowed=max(MAX_LINE, max(len(unpinned(row).rstrip()) for row in there.split("\n"))))
    if shape(there) == shape(new):
        raise Refused(REJECTED, "REJECTED: the new line is the line that is there. Nothing was written.")
    if [place for place in find(old, new) if place != (start, end)]:
        raise Refused(REJECTED, f"REJECTED: the new line is already in {name}, word for word. Delete the wrong line instead.")
    return "\n".join(lines[:start] + new.split("\n") + lines[end:]), there, new


def removed(old: str, entry: str, name: str):
    """(the text without the one entry, the line the entry stood under, the entry as the file held it)."""
    start, end = locate(old, one_entry(entry, "the entry to take out"), name)
    lines = old.split("\n")
    return "\n".join(lines[:start] + lines[end:]), above(lines, start), "\n".join(line.rstrip() for line in lines[start:end])


def restored(old: str, entry: str, under: str, agent: str, name: str) -> str:
    """The text with a removed entry back under the line it stood under: after that line and any blank lines that follow it."""
    entry = one_entry(entry, "the entry to put back")
    content(entry, agent)
    if find(old, entry):
        raise Refused(REJECTED, f"REJECTED: that entry is in {name} again already. Nothing was written.")
    lines = old.split("\n")
    if under:
        places = [at for at, line in enumerate(lines) if line.rstrip() == under]
        if len(places) != 1:
            raise Refused(STALE, f"STALE: the line this entry stood under is in {name} {len(places)} times now, so there is no telling where "
                                 f"it goes. Nothing was written. Add it at the end with write --append. The line was:\n{under}")
        at = places[0] + 1
    else:
        at = 0
    gap = at
    while gap < len(lines) and not lines[gap].strip():
        gap += 1
    at = gap if gap < len(lines) else at  # blank lines are stepped over only when something follows them
    return "\n".join(lines[:at] + entry.split("\n") + lines[at:])


# ── the writes. The caller has resolved the path (resolve); everything else is checked here. ──
def in_project(p: Path) -> bool:
    """Whether a file is one of project/: the lines every session may be shown, so the ones that can be pinned or held as alike."""
    return p.is_relative_to(MEM / "project")


def write(p: Path, new: str, agent: str, session: str, why: str, expect: str = None, append: bool = False, force: bool = False,
          distinct: str = None, hold: bool = True) -> Written:
    """Append to a file, or replace it whole.

    A replace always presents the file's hash. An append can't lose anyone's line, so code that
    has just read the file may leave `expect` out; the command line always asks for it, so an
    agent reads before it writes. An append to project/ that says what memory already says is held
    (SIMILAR) until the writer gives the reason it is a lesson of its own (`distinct`), which the log
    keeps. `hold=False` is for code that carries out what a person approved line by line.
    """
    why = actor(agent, session, why)
    if expect is None and not append:
        raise Refused(STALE, "STALE: a replace needs the hash of the file it replaces.")
    content(new, agent, force)
    if distinct is not None and not append:
        raise Refused(REJECTED, "REJECTED: --distinct goes with --append. Nothing was written.")
    said = apart(distinct, new)
    MEM.mkdir(parents=True, exist_ok=True)
    with memcheck.locked(MEM):  # check + write + log + commit are one atomic step
        before = h(p)
        if expect is not None and before != expect:
            raise Refused(STALE, f"STALE: expected {expect}, file is now {before}. Re-read, merge, retry.")
        old, eol = read(p)
        name = str(p.relative_to(MEM)) if p.is_relative_to(MEM) else p.name  # as edit and delete name it: the length rule is for project/ and ORG.md
        body = written(old, new, agent, name, append, force, in_project(p))
        like = alike(p, new) if append and hold and not said and in_project(p) else []
        if like:
            raise held(like)
        single = append and len(entries(new)) == 1 and whole(new) == entries(new)[0]  # one entry appended: the log keeps it, so undo can take it out
        return record(p, body, before, agent, session, why, said + (f" · added: {quoted(whole(new))}" if single or said else ""), eol=eol)


def edit(p: Path, entry: str, new: str, agent: str, session: str, why: str, distinct: str = None, hold: bool = True) -> Written:
    """Put `new` in the place of one entry, which must be in the file exactly once as given. An edit that makes a line of project/ say what another already says is held like an append."""
    why = actor(agent, session, why)
    said = apart(distinct, new)
    with memcheck.locked(MEM):
        old, eol = read(p)
        name = str(p.relative_to(MEM))
        body, there, new = edited(old, entry, new, agent, name, in_project(p))
        if hold and not said and in_project(p):
            at = locate(old, entry, name)[0] + 1
            was = {place[:2] for place in alike(p, there, at)}
            like = [place for place in alike(p, new, at) if place[:2] not in was]  # only what this edit makes alike: what was alike before is not its doing
            if like:
                raise held(like)
        return record(p, body, h(p), agent, session, why, f"{said} · was: {quoted(there)} · now: {quoted(new)}", eol)


def delete(p: Path, entry: str, agent: str, session: str, why: str) -> Written:
    """Take one entry out, which must be in the file exactly once as given. The log keeps its text and the line it stood under."""
    why = actor(agent, session, why)
    with memcheck.locked(MEM):
        old, eol = read(p)
        body, under, there = removed(old, entry, str(p.relative_to(MEM)))
        return record(p, body, h(p), agent, session, why, f" · removed: {quoted(there)} · after: {quoted(under)}", eol)


def undo(p: Path, row: int, agent: str, session: str, why: str) -> Written:
    """Turn round the edit, the delete or the append of one entry that one log row records: the row at line `row` of CHANGELOG.md, which must be about this file."""
    why = actor(agent, session, why)
    name = str(p.relative_to(MEM))
    with memcheck.locked(MEM):
        if row not in rows(name):
            raise Refused(STALE, f"STALE: line {row} of CHANGELOG.md is not a row about {name}. Give the line number of the row to undo (grep -n finds it).")
        was = change(rows(name)[row][4])
        if was["added"] is not None and NOT_REPEATED != was["added"]:  # an appended entry comes out as a delete would take it, wherever it now stands
            body, under, there = removed(read(p)[0], was["added"], name)
            return record(p, body, h(p), agent, session, why, f" · removed: {quoted(there)} · after: {quoted(under)}", read(p)[1])
        if was["was"] is None:
            raise Refused(REJECTED, "REJECTED: that row is a whole-file write, an undo, or an append the log did not keep the text of. "
                                    "Only an edit, a delete or the append of one entry is undone here; take a line out with `delete`.")
        if NOT_REPEATED in (was["was"], was["now"]):
            raise Refused(REJECTED, "REJECTED: the log did not keep that text, because the store refuses it. It can't be put back.")
        old, eol = read(p)
        if was["now"] is not None:
            body, there, back = edited(old, was["now"], was["was"], agent, name, in_project(p), held=True)
            return record(p, body, h(p), agent, session, why, f" · was: {quoted(there)} · now: {quoted(back)}", eol)
        body = restored(old, was["was"], was["after"], agent, name)
        return record(p, body, h(p), agent, session, why, f" · restored: {quoted(was['was'])} · after: {quoted(was['after'])}", eol)


def stdin_text(verb: str) -> str:
    """What came on stdin. A terminal there means nothing was piped in, and the command would wait for typing."""
    if sys.stdin.isatty():
        raise Refused(REJECTED, f"REJECTED: {verb} takes its text on stdin, in a heredoc with a quoted delimiter:\n{FORMS[verb]}")
    return sys.stdin.buffer.read().decode().replace("\r\n", "\n")


def parts(text: str) -> tuple:
    """The old and the new entry of an edit, parted by the one line that holds only the separator."""
    lines = text.strip("\n").split("\n")
    cuts = [at for at, line in enumerate(lines) if line.rstrip() == SEPARATOR]
    if len(cuts) != 1 or not lines[:cuts[0]] or not lines[cuts[0] + 1:]:
        raise Refused(REJECTED, f"REJECTED: an edit takes the entry as it is, a line that holds only `{SEPARATOR}`, and the entry as it should "
                                f"read. Stdin held {len(cuts)} such lines, or nothing on one side. Nothing was written. The form:\n{FORMS['edit']}")
    return "\n".join(lines[:cuts[0]]), "\n".join(lines[cuts[0] + 1:])


def main():
    ap = argparse.ArgumentParser(allow_abbrev=False)
    verbs = ap.add_subparsers(dest="cmd", required=True)
    verbs.add_parser("read").add_argument("file")
    for verb in ("write", "edit", "delete", "undo"):
        sub = verbs.add_parser(verb, allow_abbrev=False)
        sub.add_argument("file")
        sub.add_argument("--agent", required=True)
        sub.add_argument("--session", default=os.environ.get("CLAUDE_CODE_SESSION_ID") or "unknown")
        sub.add_argument("--why", required=True)
        if verb == "write":
            sub.add_argument("--expect", required=True)
            sub.add_argument("--append", action="store_true")
        if verb in ("write", "edit"):
            sub.add_argument("--distinct", metavar="WHY", help="why this line is a different lesson from the lines of memory that are like it (kept in the log)")
            sub.add_argument("--force", action="store_true", help="allow a replace that removes >50%% of the file")
        if verb == "undo":
            sub.add_argument("--row", required=True, type=int, help="the line number in CHANGELOG.md of the row to undo")
    a, extra = ap.parse_known_args()
    if extra:  # memory text given as an argument (--line, --replaces) lands here: refused before anything is read
        form = f" Memory text goes on stdin, never in an argument:\n{FORMS[a.cmd]}" if a.cmd in FORMS else ""
        print(f"memwrite.py {a.cmd} takes no {' '.join(part for part in extra if part.startswith('--')) or 'further arguments'}. Nothing was written.{form}", file=sys.stderr)
        sys.exit(2)

    if a.cmd == "read":
        p = resolve(a.file)
        if p.exists():
            sys.stdout.write(p.read_text())
        print(f"\nHASH: {h(p)}")
        return
    p = resolve(a.file, a.agent)
    if a.cmd == "write":
        done = write(p, sys.stdin.read(), a.agent, a.session, a.why, a.expect, a.append, a.force, a.distinct)
    elif a.cmd == "edit":
        done = edit(p, *parts(stdin_text("edit")), a.agent, a.session, a.why, a.distinct)
    elif a.cmd == "delete":
        done = delete(p, stdin_text("delete"), a.agent, a.session, a.why)
    else:
        done = undo(p, a.row, a.agent, a.session, a.why)
    print(f"OK {done.before} → {done.after}")


if __name__ == "__main__":
    try:
        main()
    except Refused as stop:
        print(stop)
        sys.exit(stop.code)
    except OSError as error:
        if error.errno not in (errno.EACCES, errno.EPERM, errno.EROFS):
            raise
        print("READ-ONLY: this run can't write to the memory store. Put the lesson in your final report instead.")
        sys.exit(6)

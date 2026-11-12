#!/usr/bin/env python3
"""Checks a few claims about this machine for a dream, reading only: is a path there, does a file hold a phrase, is a command on PATH.

  python3 scripts/machine_check.py   the claims of the dream in progress: the lines under "## Checked on this machine" of its side file

A note Claude Code kept by itself often says something no repo can show: where a folder outside the
repos is, which script starts a thing, that a tool is installed. The analysts have Read, Grep and Glob
inside the repos, so to them every such fact "can't be checked". This is the one way a dream checks
them. It prints one line per claim, with the claim's number:

  1. path ~/projects/launch-kit                               → 1. true: a folder is there
  2. text ~/projects/launch-kit/run.sh :: exec serve --port 5173 → 2. true: the file holds that phrase
  3. command ffmpeg                                           → 3. not checked: not on this run's PATH, which may be shorter than a terminal's

It is built so that it can't be driven: whoever writes the claims (a dream, which reads notes and
sessions that may hold someone else's words) can learn from it only what a note could legitimately
be about, and never a secret or the shape of a private folder.

- It takes no argument, so nothing a note says ever stands on a command line. The claims are lines
  of the dream's side file (dreams/<stem>-notes.md), and only the dream that owns the batch is answered.
- Where it looks is a short list (places()), not everything but a deny-list: the store's own repos
  (the project), ~/projects (where projects and what they depend on live), ~/tools (tools installed
  for them), or instead the folders listed in ~/.config/agent-memory/places, one per line, and
  ~/Library/LaunchAgents for the name of a job, by `path` only.
  Anywhere else is `not checked` without being touched. So is any path with a part that starts with
  a dot (.ssh, .aws, .config, .claude, .env and every other hidden name), anything the store's
  reject.txt names, and anything under NEVER, which on a Mac can be kept in the cloud.
- A path is walked part by part with lstat, which opens nothing. A link is read and followed only
  while it stays inside the list. A part that is kept in the cloud and not on this disk stops the
  walk: reading it would fetch it, and can hang for a minute.
- `path` says true or false. `text` says true, or `not checked`: a miss proves nothing, because the
  analyst who wrote the phrase could not read the file and may have paraphrased. A phrase is at
  least MIN_WORDS words and matches whole words of one line, in order, so nothing can be spelled
  out a character at a time. A file that holds any line the store would refuse (a credential, a
  reject.txt pattern) answers `not checked` for every phrase, in the same words as a miss, so the
  answer doesn't even say which kind of file it is. The answer never says where or how often.
- A dream gets MAX_CLAIMS answers and TEXT_PER_FILE phrases per file, however often it runs this:
  the answers are kept in the batch (_machine.json) and a claim asked again gets the answer it got.
  Each claim has TIME_LIMIT seconds.
- It writes only that file in the batch, starts nothing and opens no connection. A command is
  looked for on PATH, never run, and one that is not found is `not checked`: an unattended run's
  PATH is shorter than a terminal's.

proposal_check.py believes an item that says a check confirms it (`machine: <n>`) only if this
script answered true for claim <n> in this dream: it reads the answers, and asks nothing itself.
"""
import json, os, re, shutil, signal, stat, sys
from pathlib import Path

import dream_due
import dream_owner
import dream_proposal
import memwrite
import slim

MEM = memwrite.MEM
HEADING = "Checked on this machine"
CLAIM = re.compile(r"^(\d+)\. (path|text|command) (.+)$")
WORDS = " :: "  # in a text claim: the file, then the phrase
ANSWERS = "_machine.json"  # in the batch: every answer this dream has had
MAX_CLAIMS = 40  # answers a dream gets
TEXT_PER_FILE = 3  # phrases a dream may ask of one file
TEXT_BYTES = 1_000_000  # a file past this is not read
MIN_WORDS, MAX_PHRASE = 3, 200
TIME_LIMIT = 2  # seconds for one claim
HOPS = 16  # links followed in one path
COMMAND = re.compile(r"[A-Za-z0-9._+-]+")
DATALESS = 0x40000000  # macOS: the content is in the cloud, not on this disk
NEVER = ("Documents", "Desktop", "Library/Mobile Documents")  # under the home folder: never touched, whatever is asked
TRUE, FALSE, UNKNOWN = "true", "false", "not checked"
FORM = "<n>. path <path> | <n>. text <file> :: <a phrase of the file, three words or more> | <n>. command <name>"
NOT_SAID = "that phrase was not found as written, or the file is one nothing is said about"  # one answer for a miss and for a file that holds a secret


def claims(text: str) -> tuple:
    """({number: (kind, what)}, [(line number, why it is not a claim)]) for the lines under the heading in a side file."""
    found, wrong = {}, []
    for at, line in dream_proposal.section(text, HEADING):
        match = CLAIM.match(line.strip())
        if not match or any(ord(c) < 32 or ord(c) == 127 for c in line):
            wrong.append((at, f"is not written as a claim ({FORM})"))
        elif int(match[1]) in found:
            wrong.append((at, f"has the number {match[1]}, which another claim has"))
        else:
            found[int(match[1])] = (match[2], match[3].strip())
    return found, wrong


def looked_in() -> list:
    """The folders besides the store's repos: one per line of ~/.config/agent-memory/places, else ~/projects and ~/tools."""
    try:
        lines = (Path.home() / ".config" / "agent-memory" / "places").read_text().splitlines()
    except OSError:
        lines = []
    given = [Path(os.path.expanduser(line.strip())) for line in lines if line.strip() and not line.strip().startswith("#")]
    return [p for p in given if p.is_absolute()] or [Path.home() / "projects", Path.home() / "tools"]


def shown(place: Path) -> str:
    home = Path.home()
    return "~/" + str(place.relative_to(home)) if home in place.parents else str(place)


def places() -> tuple:
    """(where a path or a phrase may be asked about, where only a path may): each place as it is written and with every link in it followed."""
    home = Path.home()
    both = [Path(repo) for repo in slim.repos()] + looked_in()
    return [(str(p), os.path.realpath(p)) for p in both], [(str(home / "Library" / "LaunchAgents"), os.path.realpath(home / "Library" / "LaunchAgents"))]


def under(path: str, roots: list) -> bool:
    return any(path == root or path.startswith(root + os.sep) for root in roots)


def refused(path: str) -> str:
    """Why nothing is said about this path whatever is there, or empty. Decided from its name alone: nothing is touched."""
    home = os.path.realpath(Path.home())
    inside = os.path.relpath(path, home) if under(path, [home]) else path  # the home folder's own name is not what is asked about
    if any(part.startswith(".") for part in inside.split(os.sep)):
        return "a part of the path starts with a dot, and hidden files and folders are never looked at"
    if under(path, [os.path.join(home, area) for area in NEVER]):
        return "it is in a folder that is never looked at"
    if memwrite.policy_hit(path):
        return "it is something this store never keeps"
    return ""


def walk(given: str, roots: list) -> tuple:
    """(the path with every link followed, what lstat says of it or None when nothing is there, "") or (None, None, why it is not checked).

    Part by part, from a place on the list, with lstat only: nothing is opened and no folder is listed.
    """
    if not (given.startswith("~/") or os.path.isabs(given)):
        return None, None, "the path must start with ~/ or /"
    path = os.path.normpath(os.path.expanduser(given))
    for hop in range(HOPS):
        # A place may be named as it is written or as it is with its links followed: either way the walk starts at the real place.
        written, root = next(((written, real) for written, real in roots if under(path, [written, real])), (None, None))
        if root is None:
            return None, None, f"it is not in a place this check looks at (the project's repos, {', '.join(map(shown, looked_in()))}; ~/Library/LaunchAgents for a path)"
        path = os.path.join(root, os.path.relpath(path, written if under(path, [written]) else root))
        why = refused(path)
        if why:
            return None, None, why
        at, parts, seen = root, [part for part in os.path.relpath(path, root).split(os.sep) if part != "."], None
        for number, part in enumerate([""] + parts):
            at = os.path.join(at, part) if part else at
            try:
                seen = os.lstat(at)
            except (FileNotFoundError, NotADirectoryError):
                return path, None, ""
            except OSError as error:
                return None, None, f"it can't be looked at ({type(error).__name__})"
            if getattr(seen, "st_flags", 0) & DATALESS:
                return None, None, "it is kept in the cloud, not on this disk"
            if stat.S_ISLNK(seen.st_mode):
                target = os.path.normpath(os.path.join(os.path.dirname(at), os.readlink(at)))
                path = os.path.join(target, *parts[number:])
                break
        else:
            return path, seen, ""
    return None, None, "too many links in the path"


def path_claim(given: str) -> tuple:
    both, only_path = places()
    real, seen, why = walk(given, both + only_path)
    if real is None:
        return UNKNOWN, why, None
    if seen is None:
        return FALSE, "nothing is there", None
    return TRUE, "a folder is there" if stat.S_ISDIR(seen.st_mode) else "a file is there", None


def text_claim(given: str, asked: dict) -> tuple:
    file, split, phrase = given.partition(WORDS)
    words = phrase.split()
    if not split or len(words) < MIN_WORDS or len(phrase) > MAX_PHRASE:
        return UNKNOWN, f"a text claim is `text <file>{WORDS}<phrase>`, the phrase {MIN_WORDS} words or more of the file as the note gives them, {MAX_PHRASE} characters at most", None
    if memwrite.policy_hit(phrase) or memwrite.redact.credential(phrase):
        return UNKNOWN, "that phrase is something this store never keeps", None
    real, seen, why = walk(file.strip(), places()[0])
    if real is None:
        return UNKNOWN, why, None
    if seen is None or not stat.S_ISREG(seen.st_mode) or seen.st_size > TEXT_BYTES:
        return UNKNOWN, f"no plain file of {TEXT_BYTES:,} bytes or less is there", None
    if asked.get(real, 0) >= TEXT_PER_FILE:
        return UNKNOWN, f"this dream has asked its {TEXT_PER_FILE} phrases of that file", None
    try:
        with open(real, "rb") as handle:
            lines = handle.read(TEXT_BYTES).decode("utf-8", errors="ignore").splitlines()
    except OSError as error:
        return UNKNOWN, f"the file can't be read ({type(error).__name__})", real
    if any(memwrite.redact.credential(line) or memwrite.policy_hit(line) for line in lines):
        return UNKNOWN, NOT_SAID, real
    for line in lines:
        tokens = line.split()
        if any(tokens[at:at + len(words)] == words for at in range(len(tokens) - len(words) + 1)):
            return TRUE, "the file holds that phrase", real
    return UNKNOWN, NOT_SAID, real


def command_claim(name: str) -> tuple:
    if not COMMAND.fullmatch(name):
        return UNKNOWN, "a command is one bare name", None
    return (TRUE, "it is on PATH", None) if shutil.which(name) else (UNKNOWN, "not on this run's PATH, which may be shorter than a terminal's", None)


def late(*_):
    raise TimeoutError


def verdict(claim: tuple, asked: dict) -> tuple:
    """(true | false | not checked, what was seen in a few words, the file a phrase was looked for in or None) for one (kind, what), within TIME_LIMIT."""
    kind, what = claim
    before = signal.signal(signal.SIGALRM, late)
    signal.setitimer(signal.ITIMER_REAL, TIME_LIMIT)
    try:
        return path_claim(what) if kind == "path" else command_claim(what) if kind == "command" else text_claim(what, asked)
    except TimeoutError:
        return UNKNOWN, f"it took more than {TIME_LIMIT} seconds", None
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, before)


def name_of(claim: tuple) -> str:
    return f"{claim[0]} {claim[1]}"


def answers(batch: Path) -> dict:
    """{claim: {"result", "seen", "file"}}: every answer the dream that owns this batch has had. Empty when it has asked nothing."""
    try:
        return json.loads((batch / ANSWERS).read_text())
    except FileNotFoundError:
        return {}


def answer(found: dict, batch: Path) -> dict:
    """{number: (result, seen)} for the claims of a side file, asking only what this dream has not asked and may still ask. Keeps every answer in the batch."""
    had, out = answers(batch), {}
    for number in sorted(found):
        name = name_of(found[number])
        if name not in had:
            if len(had) >= MAX_CLAIMS:
                out[number] = (UNKNOWN, f"this dream has had its {MAX_CLAIMS} answers")
                continue
            asked = {}
            for row in had.values():
                if row["file"]:
                    asked[row["file"]] = asked.get(row["file"], 0) + 1
            result, seen, file = verdict(found[number], asked)
            had[name] = {"result": result, "seen": seen, "file": file}
            tmp = batch / (ANSWERS + ".tmp")
            tmp.write_text(json.dumps(had, indent=1))
            os.replace(tmp, batch / ANSWERS)
        out[number] = (had[name]["result"], had[name]["seen"])
    return out


def confirmed(batch: Path, claim: tuple) -> bool:
    """Did this dream's check answer true for this claim? For proposal_check.py, which asks nothing itself."""
    return answers(batch).get(name_of(claim), {}).get("result") == TRUE


def main():
    if len(sys.argv) != 1:
        sys.exit("usage: machine_check.py   (the claims are read from the side file of the dream in progress, never from the command line)")
    batch = MEM / "dreams" / "_batch"
    name = dream_due.stamp(batch)["proposal"]
    if not dream_owner.renew():  # the dream that owns the batch is at work: a sign of life. Nobody else is answered
        sys.exit(f"The batch in {batch} is not this dream's, and only the dream that owns it is answered. Nothing was looked at.")
    side = MEM / "dreams" / dream_proposal.side_file(name)
    try:
        text = side.read_text(errors="ignore")
    except FileNotFoundError:
        sys.exit(f"{side} is not there. Write the claims into it first, under `## {HEADING}`: {FORM}")
    found, wrong = claims(text)
    for number, (result, seen) in answer(found, batch).items():
        print(f"{number}. {result}: {seen}")
    for at, why in wrong:
        print(f"line {at} of {side.name} {why}")
    if not found and not wrong:
        print(f"{side.name} has no claims under `## {HEADING}`.")
    sys.exit(1 if wrong else 0)


if __name__ == "__main__":
    main()

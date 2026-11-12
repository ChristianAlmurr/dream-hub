#!/usr/bin/env python3
"""Claude Code's own auto memory, for the repos of this store: where its notes are, whether it is on, and which notes a dream has read.

  python3 scripts/own_notes.py                the state of every repo of this store, then one line for the store: were all its notes read by a dream that accounted for their facts
  python3 scripts/own_notes.py local <repo>   true, false or unset: what the repo's own .claude/settings.local.json says
  python3 scripts/own_notes.py reread         the person has the next dream read again the notes a dream read without saying what became of them

Claude Code keeps notes by itself, per repository, outside this store. A dream reads them once, as
unverified notes, and proposes what is worth carrying over. Everything the kit knows about those
notes is here, so the collector, the worktree hook and retire-auto-memory.sh can't disagree.

What the record says of a note (dreams/own-notes.json, {repo: {file: entry}}). An entry is either
- {"hash", "dream", "facts", "proposed"}: the dream that wrote the proposal `dream` read the note with
  this content and said what became of every one of its `facts` (its row in that proposal's side
  file): `proposed` of them were put in front of the person there, the rest were in memory already,
  stale or rejected. The note is dealt with: nothing in it is left that a person has not been shown
  or told is false. A note that holds lines and of which a dream says it holds no fact about the
  project is dealt with too, but its row has to say why, and the store's line counts those apart.
- or the content hash alone, as every entry was before dreams said this: the note was read by a dream
  that kept only what a repo file confirmed and every agent needed, and listed the rest as not
  carried. Facts may be left in it that no dream has placed. `reread` drops these entries, so the next
  dream reads those notes again; it is the person's command, in the store's history, and refused
  inside an unattended dream.

Whether auto memory is on is worked out the way Claude Code documents it (code.claude.com/docs/en/settings
and /memory), for a session a person starts in the repo with no flags:

- The settings levels, highest first: managed settings, the repo's .claude/settings.local.json, the
  repo's .claude/settings.json, the user's ~/.claude/settings.json. A file that isn't a JSON object
  is skipped, as Claude Code skips it.
- CLAUDE_CODE_DISABLE_AUTO_MEMORY in the `env` block of the highest level that sets it decides, in
  either direction (1 is off, 0 is on). Otherwise `autoMemoryEnabled` from the highest level that
  sets it to true or false. Otherwise it is on.
- Not read, because they belong to one session and not to the repo: `--settings`, `--bare`, and
  CLAUDE_CODE_DISABLE_AUTO_MEMORY in the shell. An unattended dream runs with that variable set so
  it doesn't take the notes for its own memory; the repo's notes are still there to be read.
- Not read, because they aren't on disk: settings an organisation delivers from the claude.ai console.

The folder is `autoMemoryDirectory` from the highest level that sets it, else
~/.claude/projects/<slug>/memory, where the slug is the repo's path with every character that isn't
a letter or digit made a dash. All worktrees of a repo share that folder. The key is taken from
managed settings, the repo's settings.local.json and the user's settings only, as Claude Code does:
the repo's committed .claude/settings.json can't set it, or a cloned repo could point the dream at
any folder on the machine.
"""
import hashlib, json, os, plistlib, re, shutil, subprocess, sys
from pathlib import Path

import dream_proposal
import memcheck
import memwrite

ROOT = Path(__file__).resolve().parent.parent
MEM = Path(os.environ.get("AGENT_MEMORY_DIR", ROOT / "memory")).expanduser().resolve()
KEY, FOLDER_KEY, SWITCH = "autoMemoryEnabled", "autoMemoryDirectory", "CLAUDE_CODE_DISABLE_AUTO_MEMORY"
YES, NO = {"1", "true", "yes", "on"}, {"0", "false", "no", "off"}  # how Claude Code reads a variable that is a switch
# Where an organisation's settings sit on this machine. A policy delivered by MDM replaces the files.
MANAGED_FILES = Path("/Library/Application Support/ClaudeCode" if sys.platform == "darwin" else "/etc/claude-code")
MANAGED_PLISTS = (Path("/Library/Managed Preferences") / os.environ.get("USER", "") / "com.anthropic.claudecode.plist",
                  Path("/Library/Managed Preferences/com.anthropic.claudecode.plist"))
RECORD = Path("dreams") / "own-notes.json"  # in the store: every note a finished dream has read, by repo and file: see the top of this file
PENDING = "_own-notes.json"  # in the batch: {repo: {file: {"hash", "given", "lines"}}}: what this dream was given, where in the batch, and how many lines of the note say something
BY_FILE = "Claude's own notes, file by file"  # the section of a proposal's side file that says what became of each note
NOTHING = " · nothing here because: "  # what a row of no facts adds when its note holds lines
ROW = re.compile(r"^- (?P<note>.+?) — facts: (?P<facts>\d+) · checked: (?P<checked>\d+) · unchecked: (?P<unchecked>\d+) · already in memory: (?P<present>\d+)"
                 r" · stale: (?P<stale>\d+) · rejected: (?P<rejected>\d+)(?: · nothing here because: (?P<why>\S.*))?$")
ROW_FORM = "- <note file> — facts: <n> · checked: <n> · unchecked: <n> · already in memory: <n> · stale: <n> · rejected: <n>"
SAYS_NOTHING = re.compile(r"\s*(#.*|---+|[-*]?\s*\[[^\]]*\]\([^)]*\)\s*)?")  # a line of a note that states nothing: blank, a heading, a rule, a link to another note
MAX_NOTE_BYTES = 100_000  # a note is a few lines; a file past this is not a note
MAX_NOTES = 200  # per repo and dream; the rest wait for the next dream
REMOVED = "[credential removed]"


def slug(path: str) -> str:
    """The folder name Claude Code gives a project under ~/.claude/projects."""
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def settings_file(path: Path) -> dict:
    """One settings file as Claude Code would use it: a file that is missing or isn't a JSON object sets nothing."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def managed(files: Path = MANAGED_FILES, plists=MANAGED_PLISTS) -> dict:
    """The organisation's settings on this machine: an MDM policy if there is one, else the managed files merged."""
    for path in plists:
        try:
            with open(path, "rb") as handle:
                data = plistlib.load(handle)
        except (OSError, plistlib.InvalidFileException, ValueError):
            continue
        if isinstance(data, dict) and data:
            return data
    merged = {}
    drop_ins = sorted(p for p in (files / "managed-settings.d").glob("*.json") if not p.name.startswith("."))
    for path in [files / "managed-settings.json"] + drop_ins:  # later files win, and `env` merges variable by variable
        data = settings_file(path)
        env = {**(merged.get("env") if isinstance(merged.get("env"), dict) else {}), **(data.get("env") if isinstance(data.get("env"), dict) else {})}
        merged.update(data)
        if env:
            merged["env"] = env
    return merged


def levels(repo: str, policy=None) -> list:
    """The settings that apply to a session started in `repo`, highest level first."""
    base = Path(repo) / ".claude"
    return [managed() if policy is None else policy, settings_file(base / "settings.local.json"),
            settings_file(base / "settings.json"), settings_file(Path.home() / ".claude" / "settings.json")]


def enabled(repo: str, policy=None) -> bool:
    """Does Claude Code keep its own notes for this repo? See the top of this file for the rule."""
    found = levels(repo, policy)
    for data in found:
        env = data.get("env")
        value = str(env.get(SWITCH, "")).strip().lower() if isinstance(env, dict) else ""
        if value in YES | NO:
            return value in NO
    for data in found:
        if isinstance(data.get(KEY), bool):
            return data[KEY]
    return True


def folder(repo: str, policy=None) -> Path:
    """Where Claude Code keeps its notes for this repo and all its worktrees."""
    policy_level, local, _project, user = levels(repo, policy)
    for data in (policy_level, local, user):  # not the committed project settings: see the top of this file
        value = data.get(FOLDER_KEY)
        if isinstance(value, str) and (value.startswith("~/") or os.path.isabs(value)):
            return Path(value).expanduser()
    return Path.home() / ".claude" / "projects" / slug(repo) / "memory"


def local_setting(repo: str):
    """True, False or None: what the repo's own local settings say. A new worktree is given the same."""
    value = settings_file(Path(repo) / ".claude" / "settings.local.json").get(KEY)
    return value if isinstance(value, bool) else None


def notes(repo: str, policy=None) -> list:
    """The note files of a repo. Empty when the folder is missing, or is a link: a link could lead anywhere."""
    source = folder(repo, policy)
    if source.is_symlink() or not source.is_dir():
        return []
    return sorted(p for p in source.glob("*.md") if p.is_file() and not p.is_symlink())


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def dealt(entry) -> bool:
    """Did the dream that read this note say what became of every fact in it? See the top of this file."""
    return isinstance(entry, dict)


def content(entry) -> str:
    """The content hash an entry of the record holds, in either of its two forms."""
    return entry["hash"] if dealt(entry) else entry


def entry_ok(entry) -> bool:
    return isinstance(entry, str) or (isinstance(entry, dict) and set(entry) == {"hash", "dream", "facts", "proposed"} and isinstance(entry["hash"], str)
                                      and isinstance(entry["dream"], str) and all(type(entry[k]) is int and entry[k] >= 0 for k in ("facts", "proposed")))


def record(mem: Path) -> dict:
    """{repo: {file: entry}} for every note a finished dream has read. See the top of this file for the two forms of an entry."""
    path = mem / RECORD
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
        assert isinstance(data, dict) and all(isinstance(r, dict) and all(entry_ok(e) for e in r.values()) for r in data.values())
    except (ValueError, AssertionError):
        raise SystemExit(f"{path} is not a record of read notes this version can read. Fix it, or delete it to have every note read again.")
    return data


def save(mem: Path, seen: dict):
    path = mem / RECORD
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(seen, indent=1, sort_keys=True) + "\n")
    os.replace(tmp, path)


def rows(side: str) -> tuple:
    """({note: {"line", "facts", "checked", "unchecked", "present", "stale", "rejected", "why"}}, [(line number, why it is no row)]) from a side file's text.

    One row per note the dream was given, named as it is in the batch (`<folder>/<file>` under _auto-memory/).
    """
    found, wrong = {}, []
    for at, line in dream_proposal.section(side, BY_FILE):
        match = ROW.match(line)
        if not match:
            wrong.append((at, f"is not written as a note's row ({ROW_FORM})"))
        elif match["note"] in found:
            wrong.append((at, f"is a second row for {match['note']}"))
        else:
            found[match["note"]] = dict(line=at, why=match["why"], **{k: int(match[k]) for k in ("facts", "checked", "unchecked", "present", "stale", "rejected")})
    return found, wrong


def handed_to(batch: Path) -> dict:
    """{repo: {file: {"hash", "given", "lines"}}}: the notes the dream that owns this batch was given. Empty when it was given none, or there is no batch."""
    try:
        return json.loads((batch / PENDING).read_text())
    except FileNotFoundError:
        return {}


def copy(repos: list, out: Path, mem: Path) -> list:
    """Copies into the batch the notes no dream has read yet. Returns one row per repo that has anything to report.

    A row: folder (its name under _auto-memory/, or "" when nothing was copied), repo, files, chars,
    read (left out as already read), earlier (those of them a dream read without saying what became of
    their facts: see `reread`), off (not read because auto memory is off there), same (notes of
    this repo that are, byte for byte, a note already in the batch: each as its file name and where
    that note is under _auto-memory/) and skipped (reasons, in plain words, for anything else left out).
    Only the repos this store lists: another project's notes are none of this dream's business. A repo
    is filed under its folder name; repos that share a folder name are filed under their whole slug,
    so one never overwrites the other. Credentials are replaced as they are in the session archive.

    One store can cover several repos, and Claude Code often keeps the same note in each. A note is its
    content, whatever it is called: the same bytes are given to the dream once, so they are checked and
    paid for once, and every copy is still recorded as given, so mark_read() marks each of them read.
    Notes that share a name and differ are different notes. That holds across dreams too: a note whose
    content a finished dream has read in any repo of the store is not given again when a copy turns up in
    another; the copy is counted as read and recorded with this dream. A copy is not one of the
    MAX_NOTES a dream reads of a repo, so none is left to be read again later.
    """
    rules = memwrite.redact.rules(mem)
    seen, pending, rows, given = record(mem), {}, [], {}
    known = {content(entry) for files in seen.values() for entry in files.values()}  # the content of every note a dream has read, in any repo
    names = [Path(r).name for r in repos]
    for repo, name in zip(repos, names):
        row = dict(folder="", repo=repo, files=0, chars=0, read=0, earlier=0, off=0, same=[], skipped=[])
        if not enabled(repo):
            row["off"] = len(notes(repo))
        elif folder(repo).is_symlink():
            row["skipped"].append(f"{folder(repo)} is a link, so it was not followed")
        else:
            fresh = []
            for note in notes(repo):
                if note.stat().st_size > MAX_NOTE_BYTES:
                    row["skipped"].append(f"{note.name} is over {MAX_NOTE_BYTES:,} bytes, so it was left out")
                    continue
                data = note.read_bytes()
                was = seen.get(repo, {}).get(note.name)
                if was is not None and content(was) == digest(data):
                    row["read"] += 1
                    row["earlier"] += not dealt(was)
                else:
                    fresh.append((note.name, data))
            target, wait = out / "_auto-memory" / (name if names.count(name) == 1 else slug(repo).strip("-")), 0
            for file, data in fresh:
                if data in given:
                    row["same"].append((file, given[data]))
                elif digest(data) in known:
                    row["read"] += 1
                elif row["files"] >= MAX_NOTES:
                    wait += 1
                    continue
                said = sum(not SAYS_NOTHING.fullmatch(row) for row in data.decode("utf-8", errors="ignore").splitlines())
                pending.setdefault(repo, {})[file] = {"hash": digest(data), "given": given.get(data), "lines": said}  # no place yet: a new note, or one whose content an earlier dream read
                if data in given or digest(data) in known:
                    continue
                target.mkdir(parents=True, exist_ok=True)
                text = memwrite.redact.scrub(data.decode("utf-8", errors="ignore"), rules)  # credentials, and what the store's reject.txt lists
                (target / file).write_text(text, encoding="utf-8")
                given[data] = pending[repo][file]["given"] = f"{target.name}/{file}"
                row.update(folder=target.name, files=row["files"] + 1, chars=row["chars"] + len(text))
            if wait:
                row["skipped"].append(f"{wait:,} more notes wait for the next dream ({MAX_NOTES} are read at a time)")
        if row["files"] or row["read"] or row["off"] or row["same"] or row["skipped"]:
            rows.append(row)
    (out / PENDING).write_text(json.dumps(pending, indent=1))
    return rows


def mark_read(mem: Path, proposal: str) -> int:
    """Adds the notes this dream was given to the record. dream_finish.py calls it when a proposal was written, before it clears the batch.

    Only then: a dream that wrote no proposal has judged nothing, so its notes are read again. And only
    a note the dream said the outcome of: its row in the side file of `proposal` (proposal_check.py
    refuses a proposal that lacks one, so a finish never gets here without). A note with no row is not
    recorded and is read again. A copy of a note an earlier dream read in another repo takes that
    dream's entry: it was dealt with there, or it was not.
    After it, a note comes back only if its content changes, so an item left unticked stays a no.
    """
    batch = mem / "dreams" / "_batch"
    if not (batch / PENDING).exists():  # a proposal written with no batch behind it read no notes
        return 0
    handed, seen = handed_to(batch), record(mem)
    before = {content(entry): entry for files in seen.values() for entry in files.values()}
    try:
        said = rows((mem / "dreams" / dream_proposal.side_file(proposal)).read_text(errors="ignore"))[0]
    except FileNotFoundError:
        said = {}
    count = 0
    for repo, files in handed.items():
        for file, note in files.items():
            if note["given"] is None:
                entry = before.get(note["hash"])  # gone when the person ran `reread` while this dream ran: read again next time
                if entry is None:
                    continue
            elif note["given"] in said:
                row = said[note["given"]]
                entry = {"hash": note["hash"], "dream": proposal, "facts": row["facts"], "proposed": row["checked"] + row["unchecked"]}
            else:
                continue
            seen.setdefault(repo, {})[file] = entry
            count += 1
    save(mem, seen)
    return count


def state(mem: Path, repos: list) -> dict:
    """Where the store's notes stand: {"files", "dealt", "facts", "unread", "earlier", "empty", "waiting", "proposal"}.

    `files` are the notes on disk in the store's repos. Each is `dealt` with (see the top of this file),
    `unread` (new, or changed since a dream read it) or read the `earlier` way. `facts` is how many facts
    the dreams that dealt with them accounted for, by their own count. `empty` are the dealt
    ones in which the dream that read them found no fact about the project. `waiting` counts the
    facts of dealt notes that are tick items of the store's open proposal, `proposal`: placed in front
    of the person, and not yet answered.
    """
    seen, found = record(mem), dict(files=0, dealt=0, facts=0, unread=0, earlier=0, empty=0, waiting=0, proposal=None)
    held = dream_proposal.open_one(mem)
    for repo in repos:
        for note in notes(repo):
            entry = seen.get(repo, {}).get(note.name)
            found["files"] += 1
            if entry is None or content(entry) != digest(note.read_bytes()):
                found["unread"] += 1
            elif not dealt(entry):
                found["earlier"] += 1
            else:
                found["dealt"] += 1
                found["empty"] += not entry["facts"]
                found["facts"] += entry["facts"]
                if held and entry["dream"] == held["name"] and entry["proposed"]:
                    found.update(waiting=found["waiting"] + entry["proposed"], proposal=held["name"])
    return found


def summary(found: dict) -> str:
    """One sentence for the person: are all of the store's notes dealt with, so that turning auto memory off loses nothing?"""
    plural = lambda n, one, many: f"{n:,} {one if n == 1 else many}"
    files = plural(found["files"], "note file", "note files")
    waits = (f" {plural(found['waiting'], 'fact', 'facts')} from them {'waits' if found['waiting'] == 1 else 'wait'} for your tick in {found['proposal']}."
             if found["waiting"] else "")
    waits += f" In {found['empty']:,} of them the dream that read it found no fact about this project." if found["empty"] else ""
    left = found["unread"] + found["earlier"]
    if not found["files"]:
        return "no notes of Claude's own on disk."
    if not left:
        return (f"{files}, all read by a dream, which accounted for {plural(found['facts'], 'fact', 'facts')}: "
                f"each is in memory, was put to you, or was found stale or rejected.{waits}")
    why = [part for part in (found["unread"] and f"{found['unread']:,} not read by a dream yet (the next dream reads them while auto memory is on)",
                             found["earlier"] and f"{found['earlier']:,} read by a dream that kept only what every agent needs and a repo file confirms "
                                                  "(`python3 scripts/own_notes.py reread` has the next dream read them again)") if part]
    return f"{left:,} of {files} still hold facts no dream has placed: {', and '.join(why)}.{waits}"


def reread(mem: Path) -> int:
    """The person's command: forget that a dream read the notes it did not deal with, so the next dream reads them again. Returns how many.

    In the store's history, like every change of the record a person makes. Refused inside an unattended dream.
    """
    if os.environ.get("AGENT_MEMORY_PROPOSE_ONLY"):
        raise SystemExit("READ-ONLY: this dream only proposes. Having notes read again is the person's step. Nothing was changed.")
    with memcheck.locked(mem):
        seen = record(mem)
        again = sum(not dealt(entry) for files in seen.values() for entry in files.values())
        if not again:
            return 0
        save(mem, {repo: kept for repo, kept in ((repo, {file: entry for file, entry in files.items() if dealt(entry)}) for repo, files in seen.items()) if kept})
        error = memcheck.commit(mem, f"own notes: {again} read the earlier way will be read again", [str(RECORD)])
        if error:
            raise SystemExit(f"{mem / RECORD} was written, but the commit failed: {error}")
    return again


def main():
    args = sys.argv[1:]
    if len(args) == 2 and args[0] == "local":
        print({True: "true", False: "false", None: "unset"}[local_setting(args[1])])
        return
    if args == ["reread"]:
        again = reread(MEM)
        print(f"{again} of Claude's own notes will be read again by the next dream of {MEM.name}, as if no dream had read them. Nothing else changed."
              if again else f"Nothing to read again in {MEM.name}: every note on record was dealt with by the dream that read it.")
        return
    if args:
        sys.exit("usage: own_notes.py [local <repo> | reread]")
    import slim
    seen, repos = record(MEM), slim.repos()
    for repo in repos:
        found = notes(repo)
        unread = sum(1 for n in found if repo not in seen or n.name not in seen[repo] or content(seen[repo][n.name]) != digest(n.read_bytes()))
        print(f"{repo}: auto memory {'on' if enabled(repo) else 'off'}, {len(found)} notes in {folder(repo)}, {unread} not yet read by a dream")
    try:  # the lines above say what is on disk; this one also needs the record of proposals, and says so when that can't be read
        print(f"{MEM.name}: {summary(state(MEM, repos))}")
    except SystemExit as stop:
        print(f"{MEM.name}: can't say whether its notes are dealt with. {stop}")


if __name__ == "__main__":
    main()

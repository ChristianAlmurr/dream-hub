#!/usr/bin/env python3
"""Keep this project's sessions after Claude Code deletes their transcripts, so they can be searched.

  python3 scripts/archive.py     bring the archive up to date, print one line of counts

Claude Code removes a transcript after 30 days. This keeps a slimmed copy of each session for
180: one file per session in ~/.local/state/agent-memory/sessions/<store>/, outside every repo
and outside the store, so it never enters git. The folders and files are readable by you only.

The same scope as dreaming (slim.py): only what happened inside this store's repos and their
worktrees, and no /dream sessions. Each file starts with header lines (session, started, ended,
repo, source, title; a subagent's file also has `agent:` and carries its parent's session id),
then a blank line, then the whole session, never more than 200,000 characters in all. Anything
that looks like a credential is replaced before it is written: a key shown once in a session
must not sit on disk for half a year.

The archive always matches the store's repos as they are now. When a repo is added or taken
out, every transcript still on disk is read again, and a session with nothing left in scope goes.

The files are the archive. `_seen.json` beside them only remembers which transcripts were looked
at (their modification time, whether anything in them was in scope, and which file they feed),
so a run opens just the transcripts that are new or grew. past.py searches the archive.
"""
import contextlib, datetime, fcntl, json, math, os, re, stat, time
from pathlib import Path

import memwrite
import slim

MEM = slim.MEM
MAX_CHARS = 200_000  # per session file, header included
KEEP_DAYS = 180  # a session that ended longer ago than this is removed
MANIFEST = "_seen.json"
TITLE = 120
REMOVED = "[credential removed]"
# Characters that can redraw or reorder a terminal line: C0 and C1 controls, and the bidi overrides.
UNPRINTABLE = re.compile("[\x00-\x1f\x7f-\x9f‪-‮⁦-⁩]")
FILE_ROW = {"file": str, "started": (int, float), "ended": (int, float), "owners": list, "alone": bool}


def state() -> Path:
    return Path.home() / ".local" / "state" / "agent-memory"


def folder() -> Path:
    """Where this store's archive lives."""
    return state() / "sessions" / MEM.name


def now() -> float:
    """The current time. AGENT_MEMORY_NOW (epoch seconds) replaces it, so tests can move the clock."""
    text = os.environ.get("AGENT_MEMORY_NOW")
    if not text:
        return time.time()
    try:
        at = float(text)
    except ValueError:
        at = math.nan
    if not math.isfinite(at):
        raise SystemExit(f"AGENT_MEMORY_NOW is {text!r}, which is not a time in epoch seconds. It is only for tests: unset it.")
    return at


def iso(at: float) -> str:
    return datetime.datetime.fromtimestamp(at, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def epoch(text: str) -> float:
    return datetime.datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def clean(text: str) -> str:
    """On one line, with nothing in it that could redraw or reorder what a terminal shows."""
    return UNPRINTABLE.sub("", " ".join(text.split()))


@contextlib.contextmanager
def locked(out: Path):
    """One run at a time per store. A lock of its own: a long first run must not hold up memory writes."""
    for path in (state(), out.parent, out):
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    for path in (out.parent, out):
        path.chmod(0o700)  # private whatever the umask was when they were made
    with os.fdopen(os.open(out / "_lock", os.O_WRONLY | os.O_CREAT, 0o600), "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def write(path: Path, text: str):
    """The whole file or nothing, so a reader never sees half of one. Private, like the transcript it came from."""
    tmp = path.with_name(".writing")  # one name is enough: only the run holding the lock writes
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8", errors="replace") as handle:
        handle.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def fields(lines) -> dict:
    """The header of one archived session, from its first lines. Empty when they aren't a header."""
    found = {}
    for line in lines:
        key, colon, value = line.rstrip("\n").partition(": ")
        if not colon:
            break
        found.setdefault(key, value)  # a session fed by two transcripts has two source lines; the first will do
    try:
        found["started_at"], found["ended_at"] = epoch(found["started"]), epoch(found["ended"])
    except (KeyError, ValueError):
        return {}
    return found if all(key in found for key in ("session", "repo", "source", "title")) else {}


def header(path: Path) -> dict:
    with open(path, encoding="utf-8", errors="replace") as handle:
        return fields(handle)


def load(out: Path) -> dict:
    """The manifest, checked: a row in the wrong shape would otherwise fail somewhere far from its cause."""
    path = out / MANIFEST
    if not path.exists():
        return {"repos": [], "seen": {}}
    try:
        manifest = json.loads(path.read_text())
    except ValueError:
        manifest = None
    good = (isinstance(manifest, dict) and isinstance(manifest.get("repos"), list) and isinstance(manifest.get("seen"), dict)
            and all(isinstance(row, dict) and type(row.get("mtime")) is int and type(row.get("in_scope")) is bool
                    and ("file" not in row or (all(isinstance(row.get(key), kind) for key, kind in FILE_ROW.items())
                                               and "/" not in row["file"] and row["file"].endswith(".md")))
                    for row in manifest["seen"].values()))
    if not good:
        raise SystemExit(f"{path} is not a manifest this version can read. Move it away and run again: it is rebuilt from the archive and the transcripts.")
    return manifest


def name_of(f: Path) -> str:
    """The session file a transcript feeds. Same naming as the dream batch."""
    parent = slim.parent_of(f)
    return f"{parent + '--' if parent else ''}{f.stem}.md"


def transcripts() -> dict:
    """Every transcript on disk with its modification time, grouped by the session file it feeds.

    A session resumed from another folder can have a transcript under each: one session, one file.
    """
    groups = {}
    for f in sorted((Path.home() / ".claude" / "projects").rglob("*.jsonl")):
        try:
            found = f.stat()
        except FileNotFoundError:
            continue  # Claude Code deleted it while this ran
        if stat.S_ISREG(found.st_mode):
            groups.setdefault(name_of(f), {})[str(f)] = found.st_mtime_ns
    return groups


def adopt(out: Path, files: set, seen: dict, counts: dict):
    """A session file no row points at (the manifest was lost) gets its row back from its own header, so it still expires."""
    for name in sorted(files - {row.get("file") for row in seen.values()}):
        h = header(out / name)
        if not h:
            counts["strange"] += 1
            continue
        # No modification time on record, so a transcript that is still there is read again.
        seen[h["source"]] = {"mtime": 0, "in_scope": True, "file": name, "started": h["started_at"],
                             "ended": h["ended_at"], "owners": [h["repo"]], "alone": True}


def drop(out: Path, name: str, ctx: dict):
    """Remove one session file and every row that points at it."""
    (out / name).unlink(missing_ok=True)
    ctx["files"].discard(name)
    for source in ctx["feeders"].pop(name, []):
        del ctx["seen"][source]


def page(name: str, sources: list, s: dict, roots: list) -> str:
    """One session as it is stored: header lines, a blank line, the slimmed session. Never longer than MAX_CHARS."""
    parent, dashes, stem = name[:-3].partition("--")
    who = [f"session: {parent}", f"agent: {stem}"] if dashes else [f"session: {parent}"]
    # The most specific root, so a session in a worktree inside the repo names the worktree.
    repo = max((r for r in roots if slim.inside(s["cwds"][0], [r])), key=len)
    redact = lambda text: memwrite.SECRET.sub(REMOVED, text)
    title = clean(redact(s["asked"] or ""))[:TITLE]
    head = "\n".join(who + [f"started: {iso(s['started'])}", f"ended: {iso(s['ended'])}", f"repo: {repo}"]
                     + [f"source: {source}" for source in sources] + [f"title: {title}"]) + "\n\n"
    # Replaced in each entry's own text, before the entries are joined and trimmed: a credential lives inside one
    # entry, and a cut can't leave half a key behind. Never over the joined text, where an entry ending in "token:"
    # would match together with the next entry's role marker.
    body = slim.trim([redact(l) for l in s["lines"]], [redact(l) for l in s["humans"]], MAX_CHARS - len(head) - 1)
    return head + body + "\n"


def keep(name: str, on_disk: dict, was: dict, ctx: dict):
    """Leave a stored session as it is, and note its transcripts as looked at."""
    seen = ctx["seen"]
    for source, mtime in on_disk.items():
        # Whether a transcript holds entries of its own was settled when the file was written. One that joined since: assume so.
        seen[source] = dict(was, mtime=mtime, alone=seen[source]["alone"] if "file" in seen.get(source, {}) else True)
    ctx["feeders"][name] = sorted(set(ctx["feeders"][name]) | set(on_disk))


def refresh(out: Path, name: str, on_disk: dict, ctx: dict):
    """Bring one session file in line with its transcripts. `on_disk` is source → modification time."""
    seen, counts, owners = ctx["seen"], ctx["counts"], ctx["owners"]
    rows = [seen.get(source) for source in on_disk]
    was = seen[ctx["feeders"][name][0]] if name in ctx["files"] and name in ctx["feeders"] else None
    same = all(row and row["mtime"] == mtime for row, mtime in zip(rows, on_disk.values()))
    # Nothing to do: every transcript is as it was last time, and the file they feed (if any) is still there.
    if same and not ctx["changed"] and (was or not any("file" in row for row in rows)):
        return
    # Untouched for so long that any session in them has expired.
    if max(on_disk.values()) / 1e9 < ctx["cutoff"]:
        return
    # A transcript that fed this file is gone, and it held entries no other one has. They exist only in the
    # stored file now, so it stays exactly as it is. (A change of repos overrides this: see below.)
    frozen = was and not ctx["changed"] and any(seen[source]["alone"] for source in ctx["feeders"][name] if source not in on_disk)
    if frozen:
        return keep(name, on_disk, was, ctx)
    counts["opened"] += len(on_disk)
    live = sorted(source for source in on_disk if not slim.is_dream(Path(source), ctx["memo"]))
    s = slim.read([Path(source) for source in live], ctx["roots"]) if live else {"lines": []}
    in_scope = bool(s["lines"])
    if in_scope and s["ended"] is None:
        counts["undated"] += 1
    storable = in_scope and s["ended"] is not None and s["ended"] >= ctx["cutoff"]
    # A stored session is not swapped for one that is empty or begins later. That happens when a worktree was
    # removed: the part that ran there is out of scope now and would be lost. A change of repos overrides this,
    # because then the archive has to match the repos as they are now.
    if was and not ctx["changed"] and not (storable and s["started"] <= was["started"]):
        return keep(name, on_disk, was, ctx)
    drop(out, name, ctx)
    counts["removed"] += bool(was and not storable)
    note = {"in_scope": in_scope}
    if storable:
        write(out / name, page(name, live, s, ctx["roots"]))
        ctx["files"].add(name)
        ctx["feeders"][name] = list(on_disk)
        counts["written"] += 1
        note.update(file=name, started=s["started"], ended=s["ended"],
                    owners=sorted({owners[r] for r in owners for cwd in s["cwds"] if slim.inside(cwd, [r])}))
    for source, mtime in on_disk.items():
        seen[source] = dict(note, mtime=mtime, alone=source in s["alone"]) if storable else dict(note, mtime=mtime)


def scan(out: Path, manifest: dict, counts: dict):
    seen = manifest["seen"]
    owners = slim.owners()
    repos = sorted(set(owners.values()))
    gone = set(manifest["repos"]) - set(repos)
    ctx = dict(seen=seen, counts=counts, owners=owners, roots=sorted(owners), memo={}, cutoff=now() - KEEP_DAYS * 86400,
               changed=manifest["repos"] != repos, files={p.name for p in out.glob("*.md")}, feeders={})
    manifest["repos"] = repos
    adopt(out, ctx["files"], seen, counts)
    for source, row in seen.items():
        if "file" in row:
            ctx["feeders"].setdefault(row["file"], []).append(source)
    # Expiry is by the session's last entry, whichever transcript it came from.
    for name in sorted({row["file"] for row in seen.values() if "file" in row and row["ended"] < ctx["cutoff"]}):
        drop(out, name, ctx)
        counts["expired"] += 1
    groups = transcripts()
    for name, on_disk in groups.items():
        try:
            refresh(out, name, on_disk, ctx)
        except FileNotFoundError:
            continue  # Claude Code deleted a transcript while this ran; what was archived before stays
    present = {source for on_disk in groups.values() for source in on_disk}
    for source, row in [(source, row) for source, row in seen.items() if source not in present]:
        if source not in seen:
            continue  # went with its session file a moment ago
        if "file" not in row:
            del seen[source]  # only a note not to reopen a transcript, and that transcript is gone
        elif row["file"] not in ctx["files"]:
            drop(out, row["file"], ctx)  # the file was deleted by hand and nothing is left to rebuild it from
        elif set(row["owners"]) & gone and row["file"] not in groups:
            drop(out, row["file"], ctx)  # its repo left the store and takes its sessions with it
            counts["removed"] += 1


def update() -> dict:
    """Bring the archive up to date. Returns the counts that archive.py prints."""
    if not MEM.is_dir():
        raise SystemExit(f"No memory store at {MEM}, so there are no sessions to keep.")
    out = folder()
    counts = dict(opened=0, written=0, expired=0, removed=0, undated=0, strange=0)
    now()  # before anything is touched, so a bad clock stops the run cleanly
    with locked(out):
        manifest = load(out)
        before = json.dumps(manifest, sort_keys=True)
        try:
            scan(out, manifest, counts)
        finally:
            # Also after an interrupted run, so the transcripts it got through aren't read again.
            after = json.dumps(manifest, sort_keys=True)
            if after != before:
                write(out / MANIFEST, after)
        counts["kept"] = len({row["file"] for row in manifest["seen"].values() if row.get("file")})
    return counts


def main():
    c = update()
    extra = [f"{c[key]} {words}" for key, words in (
        ("removed", "no longer in this store's repos removed"), ("undated", "with no timestamps left out"),
        ("strange", "in the folder that are not session files, left alone")) if c[key]]
    print(f"archive: {c['kept']} session files in {folder()} ({c['opened']} transcripts opened, {c['written']} written, "
          f"{c['expired']} expired" + "".join(f", {e}" for e in extra) + ")")


if __name__ == "__main__":
    main()

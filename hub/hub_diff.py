"""What a session changed in its folder, read with git and nothing else: diff, numstat, status and ls-files. Never a
command that writes: no index refresh (--no-optional-locks), no external diff or textconv driver a repository's config
could name, no fsmonitor. The hub never commits, stages or reverts here.

A session's folder is the one the hub started it in (its worktree or its project's folder), and it must still be one
of its project's folders or a worktree inside one. A file is shown only when git lists it as changed, and never one the
hub refuses to serve anywhere (git internals, keys, dotenv files): those are listed by name with no content.
"""
import os, re
from pathlib import Path

from hub_base import ENV, HOME, Refused, run

EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"  # what git diffs against before a first commit
GIT_ENV = {**ENV, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "cat", "LC_ALL": "C"}
SAFE = ("-c", "core.quotepath=off", "-c", "core.fsmonitor=false", "-c", "color.ui=false")
DIFF_FLAGS = ("--no-color", "--no-ext-diff", "--no-textconv", "--no-renames")
UNTRACKED_MAX, UNTRACKED_BYTES = 300, 512_000
FILE_LINES, BUDGET_LINES, EAGER_FILES = 3000, 12000, 80
HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")


def git(folder: str, *args, timeout=20):
    import subprocess
    from hub_base import refused_vcs
    never = refused_vcs(["git", "-C", folder, *args], text=False)  # a repo the hub never runs git in: as if it weren't one
    if never:
        return never
    return subprocess.run(["git", "--no-optional-locks", *SAFE, "-C", folder, *args], env=GIT_ENV, capture_output=True, timeout=timeout)


def hidden(path: str) -> bool:
    """A changed file whose content the hub never shows: the same rule as the files a session may open."""
    from hub_data import NEVER_SERVED
    parts = Path(path).parts
    name = parts[-1] if parts else ""
    return bool(NEVER_SERVED & set(parts)) or name.startswith(".env") or Path(name).suffix.lower() in (".pem", ".key", ".p12", ".keychain")


def base_of(folder: str) -> str:
    return "HEAD" if git(folder, "rev-parse", "--verify", "-q", "HEAD").returncode == 0 else EMPTY_TREE


def is_repo(folder: str) -> bool:
    if not folder or not Path(folder).is_dir():
        return False
    done = git(folder, "rev-parse", "--is-inside-work-tree", timeout=10)
    return done.returncode == 0 and done.stdout.strip() == b"true"


def _split_z(data: bytes) -> list:
    return [part.decode("utf-8", "replace") for part in data.split(b"\0") if part]


def tracked(folder: str, base: str) -> list:
    """[(path, added, removed, binary)] for every tracked file that differs from the last commit, staged or not."""
    done = git(folder, "diff", "--numstat", "-z", *DIFF_FLAGS, base)
    if done.returncode != 0:
        return []
    out = []
    for part in _split_z(done.stdout):
        bits = part.split("\t", 2)
        if len(bits) == 3:
            added, removed, path = bits
            binary = added == "-"
            out.append((path, 0 if binary else int(added), 0 if binary else int(removed), binary))
    return out


def untracked(folder: str) -> list:
    done = git(folder, "ls-files", "--others", "--exclude-standard", "-z")
    return _split_z(done.stdout)[:UNTRACKED_MAX] if done.returncode == 0 else []


def inside(folder: str, rel: str):
    """The file a changed path names, only while it is really inside the folder (a link out of it doesn't count)."""
    root = Path(folder).resolve()
    path = (root / rel)
    try:
        real = path.resolve()
    except OSError:
        return None
    return real if real != root and root in real.parents and real.is_file() else None


def new_text(folder: str, rel: str):
    """An untracked file's lines, or None when it isn't text the hub may show."""
    if hidden(rel):
        return None
    path = inside(folder, rel)
    if path is None or path.stat().st_size > UNTRACKED_BYTES:
        return None
    data = path.read_bytes()
    if b"\0" in data[:8000]:
        return None
    return data.decode("utf-8", "replace").splitlines()


def numstat(folder: str):
    """{added, removed, files} against the last commit, counting new files' lines too. None if it isn't a git folder."""
    if not is_repo(folder):
        return None
    base = base_of(folder)
    rows = tracked(folder, base)
    added, removed = sum(r[1] for r in rows), sum(r[2] for r in rows)
    fresh = untracked(folder)
    for rel in fresh:
        lines = new_text(folder, rel)
        added += len(lines) if lines else 0
    return {"added": added, "removed": removed, "files": len(rows) + len(fresh)}


def parse_patch(text: str, limit: int):
    """Hunks as rows the page draws: [kind, old line, new line, text], kind one of ' ', '+', '-', '\\'."""
    hunks, rows, old, new, cut = [], 0, 0, 0, False
    for line in text.splitlines():
        match = HUNK.match(line)
        if match:
            old, new = int(match[1]), int(match[3])
            hunks.append({"old": old, "new": new, "head": match[5].strip()[:120], "rows": []})
            continue
        if not hunks or line.startswith(("diff --git", "index ", "--- ", "+++ ", "new file", "deleted file", "old mode", "new mode", "similarity", "Binary files")):
            continue
        if rows >= limit:
            cut = True
            break
        kind = line[:1] if line[:1] in (" ", "+", "-", "\\") else " "
        body = line[1:]
        if kind == "+":
            hunks[-1]["rows"].append(["+", None, new, body]); new += 1
        elif kind == "-":
            hunks[-1]["rows"].append(["-", old, None, body]); old += 1
        elif kind == "\\":
            hunks[-1]["rows"].append(["\\", None, None, body])
        else:
            hunks[-1]["rows"].append([" ", old, new, body]); old += 1; new += 1
        rows += 1
    return hunks, cut


def folder_of(sid: str) -> str:
    """The session's folder, checked: still one of its project's folders, or a worktree the hub made inside one."""
    import hub_data as hub
    meta = hub.session_meta(sid)
    folder = meta.get("folder") or meta.get("repo") or ""
    if not folder or not Path(folder).is_dir():
        raise Refused("This session's folder isn't there any more.")
    real = Path(folder).resolve()
    repos = [Path(r).resolve() for r in hub.repos_of(hub.store_of(meta["project"]))]
    if not any(real == repo or (repo / ".claude" / "worktrees") in real.parents for repo in repos):
        raise Refused("This session's folder isn't one of its project's folders.")
    return str(real)


def file_entry(folder: str, base: str, path: str, added: int, removed: int, binary: bool, new: bool, budget: int) -> dict:
    entry = {"path": path, "status": "added" if new else "changed", "added": added, "removed": removed, "binary": binary,
             "hidden": hidden(path), "hunks": None, "cut": False}
    if entry["hidden"] or binary or budget <= 0:
        return entry
    if new:
        lines = new_text(folder, path)
        if lines is None:
            entry["binary"] = True
            return entry
        take = lines[:min(FILE_LINES, budget)]
        entry["hunks"] = [{"old": 0, "new": 1, "head": "", "rows": [["+", None, i + 1, text] for i, text in enumerate(take)]}] if take else []
        entry["cut"] = len(take) < len(lines)
        return entry
    done = git(folder, "diff", *DIFF_FLAGS, "-U3", base, "--", f":(literal){path}")
    if done.returncode != 0:
        return entry
    text = done.stdout.decode("utf-8", "replace")
    if "\ndeleted file mode" in text[:400]:
        entry["status"] = "deleted"
    elif "\nnew file mode" in text[:400]:
        entry["status"] = "added"
    entry["hunks"], entry["cut"] = parse_patch(text, min(FILE_LINES, budget))
    return entry


def diff(sid: str, path: str = "") -> dict:
    """The session's changes against the last commit. With `path`, that one file, which must be a changed one."""
    folder = folder_of(sid)
    if not is_repo(folder):
        return {"folder": folder.replace(str(HOME), "~", 1), "git": False, "files": [], "added": 0, "removed": 0}
    if path:
        if not isinstance(path, str) or path.startswith(("/", "~")) or "\0" in path or ".." in Path(path).parts or len(path) > 1000:
            raise Refused("That file isn't one of this session's changes.")
    base = base_of(folder)
    rows = [(p, a, r, b, False) for p, a, r, b in tracked(folder, base)]
    for rel in untracked(folder):
        lines = new_text(folder, rel)
        rows.append((rel, len(lines) if lines else 0, 0, lines is None and not hidden(rel), True))
    rows.sort(key=lambda row: row[0])
    if path:
        row = next((r for r in rows if r[0] == path), None)
        if row is None:
            raise Refused("That file isn't one of this session's changes.")
        return file_entry(folder, base, *row, budget=FILE_LINES)
    files, budget = [], BUDGET_LINES
    for index, row in enumerate(rows):
        entry = file_entry(folder, base, *row, budget=budget if index < EAGER_FILES else 0)
        if entry["hunks"] is not None:
            budget -= sum(len(h["rows"]) for h in entry["hunks"])
        entry["later"] = entry["hunks"] is None and not entry["hidden"] and not entry["binary"]
        files.append(entry)
    return {"folder": folder.replace(str(HOME), "~", 1), "git": True, "base": "HEAD" if base == "HEAD" else "",
            "files": files, "added": sum(f["added"] for f in files), "removed": sum(f["removed"] for f in files)}


# ── review comments, sent to the session as one paste ──
def review(sid: str, body: dict):
    import hub_data as hub
    hub.live_claude(sid)
    comments = (body or {}).get("comments")
    if not isinstance(comments, list) or not comments:
        raise Refused("There are no comments to send.")
    if len(comments) > 60:
        raise Refused("That's more comments than one message should carry. Send them in parts.")
    lines = ["Review comments on your uncommitted changes. Address each one, then say what you changed:", ""]
    for index, c in enumerate(comments, 1):
        if not isinstance(c, dict):
            raise Refused("A comment couldn't be read.")
        file, text, line = c.get("path"), c.get("text"), c.get("line")
        if not isinstance(file, str) or not file or not isinstance(text, str) or not text.strip() or len(text) > 4000:
            raise Refused("Each comment needs a file and some words.")
        where = f"{file}:{line}" if isinstance(line, int) and not isinstance(line, bool) and line > 0 else file
        lines.append(f"{index}. {where}")
        quote = c.get("quote")
        if isinstance(quote, str) and quote.strip():
            lines.append(f"   > {quote.strip()[:300]}")
        lines += [f"   {row}" for row in text.strip().splitlines()]
    message = "\n".join(lines)
    target = f"hub-{sid}"
    if hub.typed_now(target):
        raise Refused("Something is already typed in this session. Send or clear it there first.")
    hub.paste_prompt(target, message, f"hub-review-{sid}")
    return {"ok": True, "sent": len(comments)}

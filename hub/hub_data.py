"""Everything Dream Hub knows, read from the files and tools that already hold it. What it knows of the agent memory
kit comes through hub_kit.py and from nowhere else."""
import base64, hashlib, json, os, re, secrets, shlex, shutil, threading, time
from pathlib import Path

from collections import Counter

import hub_agents as agents
import hub_status as status
import hub_kit as kit
from hub_base import APP, CONFIG, ENV, HOME, PORT, PROJECTS, SOCKET_NAME, STATE, STORES, BadInstance, Malformed, Refused, damaged, listed, number_in, off_limits, pasteable, read_instance, run, text_in

META = STATE / "sessions"
EVENTS = STATE / "events"
PROFILES = STATE / "projects"
UPLOADS = STATE / "uploads"
RECORDS = STATE / "records"  # a copy of each closed session's transcript: Claude Code deletes its own after 30 days
HOOKS = STATE / "claude-hooks.json"
ACTIONS = STATE / "person-actions.jsonl"  # what was done through this hub that only the person may do, one line each
CLAUDE_SESSIONS = HOME / ".claude" / "sessions"
TMUX = shutil.which("tmux", path=ENV["PATH"]) or "tmux"
CLAUDE = shutil.which("claude", path=ENV["PATH"]) or str(HOME / ".local" / "bin" / "claude")
SOCKET = ["-L", SOCKET_NAME, "-f", str(APP / "tmux.conf")]
NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
SESSION_ID = re.compile(r"^[a-f0-9]{8}\Z")
FIRST_RUN = ("trust this folder", "Enter to confirm", "Space to select", "Esc to reject")
write_lock = threading.Lock()
MODES = ("plan", "manual", "acceptEdits", "auto")
# The models a session can start with: Claude Code's --model aliases, and the name its /model menu shows for each.
# Keep this in step with MODELS in ui/app.js, which holds what the picker says about each.
MODELS = {"opus": "Opus", "fable": "Fable", "sonnet": "Sonnet", "haiku": "Haiku"}
EFFORTS = ("low", "medium", "high", "xhigh", "max")
MODE_FOOTER = (("plan mode on", "plan"), ("auto mode on", "auto"), ("manual mode on", "manual"), ("accept edits on", "acceptEdits"))


_version = ""  # set when the server starts (prepare)


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:12]


def tmux(*args):
    return run(TMUX, *SOCKET, *args)


def prepare():
    global _version
    _version = _built()
    for folder in (META, EVENTS, PROFILES, UPLOADS, RECORDS):
        folder.mkdir(parents=True, exist_ok=True)
    hook = [{"hooks": [{"type": "command", "command": f"/usr/bin/python3 {shlex.quote(str(APP / 'hub_event.py'))}"}]}]
    names = ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "PostToolUseFailure", "PreCompact", "PostCompact", "PermissionRequest", "Stop", "StopFailure", "SessionEnd", "Notification")
    text = json.dumps({"hooks": {name: hook for name in names}})
    if not HOOKS.exists() or HOOKS.read_text() != text:  # running sessions read this file, so it is never seen half written
        spare = HOOKS.with_suffix(".tmp")
        spare.write_text(text)
        os.replace(spare, HOOKS)


# ── the person's tier: what only the person may do, done through this hub ──
def the_person(action: str):
    """The one place that decides whether a request for something only the person may do comes from the person.

    Today it decides nothing more than the server already has: the request carried the owner's login, the app's Host
    and its Origin. That keeps other people on the tailnet and other sites in the browser out. It does not tell the
    person from a program running as them on this Mac, which can read the same three values and send them. A real
    proof of a person (a password, Touch ID) belongs here and nowhere else: raise Refused when it fails.
    """
    return None


def version() -> str:
    """Which build of the hub this server runs: the interface and the code it was started with. A page loaded from an
    earlier build asks to be reloaded when this changes."""
    return _version


def _built() -> str:
    seen = hashlib.sha256()
    for path in sorted([*APP.glob("*.py"), *(APP / "ui").glob("app.*"), APP / "ui" / "index.html"]):
        seen.update(path.read_bytes())
    return seen.hexdigest()[:12]


def record_action(name: str, action: str, what: str):
    """Write down that something only the person may do was done through this hub, with the store's commit it left.
    The store's own history says that it happened; this says by which door, so a call the person didn't make shows."""
    head = run("git", "-C", STORES / name, "rev-parse", "HEAD").stdout.strip()
    ACTIONS.parent.mkdir(parents=True, exist_ok=True)
    with write_lock, open(ACTIONS, "a") as log:
        log.write(json.dumps({"at": int(time.time()), "project": name, "action": action, "what": what[:300], "commit": head}) + "\n")


PERSON_COMMITS = (("memory(human): ", "Principles edited"), ("needs: what ", "Closed something that needed you"), ("proposal: dismissed ", "Dismissed"), ("proposal: a declined item may be proposed again", "Allowed a declined item again"))
WITHDRAWN = re.compile(r"^proposal: (.+) of (\S+-proposal\.md) was withdrawn$")


def recent_actions(name: str, store: Path) -> list:
    """The last things done to this memory that only the person may do: newest first, from the store's own history
    (so it holds whichever way they were done), each marked when it came through this hub. Giving up a dream leaves
    no commit, so those come from the hub's own list."""
    mine = []
    for line in (ACTIONS.read_text().splitlines() if ACTIONS.exists() else []):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("project") == name:
            mine.append(row)
    through = {row.get("commit") for row in mine if row.get("action") != "release"}
    found = []
    for row in run("git", "-C", store, "log", "-200", "--format=%H%x09%ct%x09%s").stdout.splitlines():
        commit, _, rest = row.partition("\t")
        when, _, subject = rest.partition("\t")
        said = next((f"{words} {subject[len(start):]}".strip() if start.endswith("dismissed ") else words for start, words in PERSON_COMMITS if subject.startswith(start)), None)
        taken = WITHDRAWN.match(subject)
        if taken:
            said = f"Took back {taken[1]} of {taken[2]}"
        if said and when.isdigit():
            found.append({"at": int(when), "what": said, "hub": commit in through})
    found += [{"at": row["at"], "what": "Gave up a dream that was running", "hub": True} for row in mine if row.get("action") == "release" and isinstance(row.get("at"), int)]
    return sorted(found, key=lambda row: row["at"], reverse=True)[:12]


# ── projects ──
def store_of(name: str) -> Path:
    if not NAME.match(name or ""):
        raise Refused("That isn't a project name.")
    store = STORES / name
    if not (store / "ORG.md").exists():
        raise Refused("There's no project with that name.")
    return store


def repos_of(store: Path) -> list:
    listed = store / "roots.txt"
    lines = listed.read_text().splitlines() if listed.exists() else []
    return [line for line in lines if line.strip() and Path(line).is_dir()]


def entries(path: Path) -> int:
    return sum(1 for line in path.read_text(errors="ignore").splitlines() if line.startswith("- ")) if path.exists() else 0


def steering_of(repo: str):
    dream = Path(repo) / ".claude" / "commands" / "dream.md"
    text = dream.read_text() if dream.exists() else ""
    return text.split("\n## Steering", 1)[1].split("\n", 1)[1].strip() if "\n## Steering" in text else None


def trusted(repo: str) -> bool:
    try:
        return bool(_claude_json().get("projects", {}).get(repo, {}).get("hasTrustDialogAccepted"))
    except (OSError, ValueError):
        return True


_claude_cache = (0, {})


def _claude_json():
    global _claude_cache
    path = HOME / ".claude.json"
    mtime = path.stat().st_mtime
    if mtime != _claude_cache[0]:
        _claude_cache = (mtime, json.loads(path.read_text()))
    return _claude_cache[1]


def notes_carried(store: Path) -> bool:
    """Whether the newest dream already lists Claude's own notes, so what is left is to apply it and turn them off."""
    known = kit.record_of(store)["proposals"]
    path = store / "dreams" / known[-1]["name"] if known else None
    return bool(path and path.is_file()) and any(section_kind(line[3:].strip()) == "own-notes" for line in path.read_text(errors="ignore").splitlines() if line.startswith("## "))


def setup_of(name: str, store: Path, repos: list) -> list:
    """Only what's still undone, so a finished project shows nothing."""
    todo = []
    if (store / "ORG.md").read_text() == kit.untouched_principles():
        todo.append({"key": "org", "text": "Write your principles", "go": "memory/ORG.md"})
    steering = steering_of(repos[0]) if repos else None
    if steering is None or "<e.g." in steering or "<!-- default" in steering:
        todo.append({"key": "steering", "text": "Tell dreaming what matters", "go": "memory/steering"})
    if entries(store / "project" / "gotchas.md") < 3:
        todo.append({"key": "gotchas", "text": "Note 3 traps you already know", "go": "sessions"})
    for repo in repos:
        short = Path(repo).name
        if not trusted(repo):
            todo.append({"key": "trust", "text": f"Trust {short}: start a session and accept", "go": "sessions"})
        if not off_limits(repo) and not run("git", "-C", repo, "ls-files", "scripts/memwrite.py").stdout.strip():  # a repo off limits keeps its kit untracked
            todo.append({"key": "commit", "text": f"Commit the kit in {short}", "go": "commit", "repo": repo})
    if repos and kit.keeps_own_notes(store, repos):
        todo.append({"key": "own-notes", "text": "Move Claude's own notes into memory", "go": "dreams", "carried": notes_carried(store),
                     "then": kit.retire_command(name)})
    return todo


def stores() -> list:
    """Every project: a folder in ~/agent-memory whose name a project can have, holding its principles."""
    return [s for s in sorted(STORES.iterdir()) if NAME.match(s.name) and (s / "ORG.md").exists()] if STORES.exists() else []


def folders() -> list:
    used = set()
    for store in STORES.iterdir():
        if store.is_dir():
            used.update(repos_of(store))
    return sorted(p.name for p in PROJECTS.iterdir() if (p / ".git").exists() and str(p) not in used and not p.name.startswith("."))


def create_project(body: dict):
    name = str(body.get("name", "")).strip()
    if not NAME.match(name):
        raise Refused("Use lowercase letters, numbers and dashes.")
    if (STORES / name).exists():
        raise Refused("A project with that name already exists.")
    chosen = [str(f) for f in body.get("folders", [])]
    fresh = False
    repos = []
    for folder in chosen:
        path = (PROJECTS / folder).resolve()
        if path.parent != PROJECTS.resolve() or not path.is_dir():
            raise Refused(f"{folder} isn't a folder in {pasteable(PROJECTS)}.")
        repos.append(path)
    if not repos:
        path = PROJECTS / name
        if path.exists():
            raise Refused(f"{pasteable(PROJECTS / name)} already exists. Pick it from the list instead.")
        path.mkdir()
        run("git", "init", "-q", "-b", "main", path)
        run("git", "-C", path, "commit", "-q", "--allow-empty", "-m", "Start")
        repos = [path]
        fresh = True
    result = kit.install(name, repos)
    if result.returncode != 0:
        raise Refused(result.stderr.strip()[:300] or "The install failed.")
    if fresh:  # a brand-new repo has nobody else's work on it, so the kit goes straight onto main
        commit_paths(str(repos[0]))
    return {"name": name}


KIT_PATHS = ("CLAUDE.md", "scripts", ".claude/settings.json", ".claude/commands", ".claude/agents")


def commit_paths(repo: str):
    run("git", "-C", repo, "add", "--", *KIT_PATHS)
    result = run("git", "-C", repo, "commit", "-q", "-m", "Add the agent memory kit", "--", *KIT_PATHS)
    if result.returncode != 0:
        raise Refused(result.stderr.strip()[:200] or "Nothing to commit.")


def commit_kit(name: str, body: dict):
    store = store_of(name)
    repo = str(body.get("repo", ""))
    if repo not in repos_of(store):
        raise Refused("That folder isn't part of this project.")
    if off_limits(repo):
        raise Refused(f"The hub never commits in {Path(repo).name}: its kit files stay untracked there.")
    if (Path(repo) / ".jj").exists():
        raise Refused("This repo uses jj, so commit the kit there with jj.")
    commit_paths(repo)
    return {"ok": True}


# ── memory ──
TIERS = {"ORG.md": "yours", "INDEX.md": "generated", "CHANGELOG.md": "log"}


def memory_files(name: str) -> list:
    store = store_of(name)
    paths = [store / f for f in TIERS] + sorted((store / "project").glob("*.md")) + sorted((store / "inbox").glob("*.md")) \
        + sorted(p for p in (store / "agents").glob("*/scratch.md") if p.parent.name != "_template")
    out = []
    for p in paths:
        if p.exists():
            rel = str(p.relative_to(store))
            out.append({"path": rel, "tier": TIERS.get(rel, "agents"), "lines": len(p.read_text(errors="ignore").splitlines()), "at": p.stat().st_mtime})
    return out


def safe_path(store: Path, rel: str) -> Path:
    path = (store / rel).resolve()
    if path.suffix != ".md" or store.resolve() not in path.parents or ".git" in path.parts or "dreams" in path.relative_to(store.resolve()).parts:
        raise Refused("That file isn't part of this memory.")
    if not path.exists():
        raise Refused("That file doesn't exist.")
    return path


def read_memory(name: str, rel: str) -> dict:
    store = store_of(name)
    repos = repos_of(store)
    if rel == "steering":
        text = (steering_of(repos[0]) if repos else "") or ""
        return {"path": rel, "content": text, "hash": sha(text), "editable": True, "history": []}
    path = safe_path(store, rel)
    text = path.read_text(errors="ignore")
    # Newest first. An edit carries the line before and after, a removal the line that went: the kit's log says which.
    history = [{key: row[key] for key in ("when", "agent", "session", "why", "was", "now")} for row in reversed(kit.log_rows(store)) if row["file"] == rel] if rel != "CHANGELOG.md" else []
    return {"path": rel, "content": text, "hash": sha(text), "editable": rel == "ORG.md", "history": history[:30]}


def write_memory(name: str, body: dict):
    store = store_of(name)
    rel, content, expect = body.get("path"), str(body.get("content", "")), body.get("expect")
    if len(content) > 100_000:
        raise Refused("That's too long for memory.")
    with write_lock:
        if rel == "steering":
            save_steering(repos_of(store), content, expect)
            return {"ok": True}
        if rel != "ORG.md":
            raise Refused("Only your principles are edited here. Agents write the rest.")
        the_person("save ORG.md")
        pinned = lambda text: sorted(line.rstrip() for line in text.splitlines() if line.rstrip().endswith(PIN))
        if pinned(content) != pinned((store / "ORG.md").read_text(errors="ignore")):
            raise Refused("A pin is added or taken off only by a dream's edit that you tick. Nothing was saved.")
        save_principles(store, content.rstrip("\n") + "\n", expect)
    record_action(name, "principles", "ORG.md saved")
    return {"ok": True}


def save_steering(repos: list, content: str, expect: str):
    """Steering lives in every repo of a store, in .claude/commands/dream.md, and must be the same block in each. A save
    writes all of them or none: every file is read and checked first (it is there, its block is the one you opened, and
    every repo's block is the same), the new files are written beside the old ones, then each is swapped in; if a swap
    fails, the ones already swapped go back to what they held."""
    if not repos:
        raise Refused("This project has no folder, so it has no Steering.")
    paths = [Path(repo) / ".claude" / "commands" / "dream.md" for repo in repos]
    missing = [str(p.parent.parent.parent).replace(str(HOME), "~", 1) for p in paths if not p.is_file()]
    if missing:
        raise Refused(f"{', '.join(missing)} has no .claude/commands/dream.md, so Steering can't be saved in every folder. Nothing was saved.")
    before = [p.read_text() for p in paths]
    blocks = [text.split("\n## Steering", 1)[1].split("\n", 1)[1].strip() if "\n## Steering" in text else "" for text in before]
    if len(set(blocks)) > 1:
        raise Refused("Steering isn't the same in every folder of this project, so a save would overwrite one of them. Nothing was saved.")
    if sha(blocks[0]) != expect:
        raise Refused("It changed since you opened it. Reload and try again.")
    after = [text.split("\n## Steering", 1)[0] + "\n## Steering (edit per project)\n" + content.strip() + "\n" for text in before]
    temps = []
    try:
        for path, text in zip(paths, after):
            tmp = path.with_name(path.name + ".hub-tmp")
            tmp.write_text(text)
            temps.append(tmp)
    except OSError as error:
        for tmp in temps:
            tmp.unlink(missing_ok=True)
        raise Refused(f"Steering wasn't saved: {error}. Nothing was changed.")
    swapped = []
    try:
        for path, tmp in zip(paths, temps):
            os.replace(tmp, path)
            swapped.append(path)
    except OSError as error:
        for path in swapped:
            path.write_text(before[paths.index(path)])
        for tmp in temps:
            tmp.unlink(missing_ok=True)
        raise Refused(f"Steering wasn't saved: {error}. Every folder is as it was.")


def locked_write(path: Path, text: str):
    """Write the person's tier: the lock comes off for the write and goes back on whatever happens to it."""
    run("chflags", "nouchg", path)
    try:
        path.write_text(text)
    finally:
        run("chflags", "uchg", path)


def save_principles(store: Path, text: str, expect: str):
    """Save ORG.md and have the kit seal it: its memcheck.py --seal writes the log row and commits, under the store's
    lock, so a save here is versioned exactly like one made by hand. If the kit can't seal it (git is busy, another
    writer holds the memory, the kit doesn't answer) the file goes back to what it held and the person is told, so the
    store is never left changed and unversioned. Whatever happens, the file is locked again."""
    path, log = store / "ORG.md", store / "CHANGELOG.md"
    before = path.read_text()
    if sha(before) != expect:
        raise Refused("It changed since you opened it. Reload and try again.")
    if text == before:
        return
    rows = log.read_bytes() if log.exists() else b""
    locked_write(path, text)
    try:
        trouble = kit.seal(store)
    except Refused as quiet:
        trouble = str(quiet)
    if trouble:
        locked_write(path, before)
        after = log.read_bytes() if log.exists() else b""
        added = after[len(rows):].decode(errors="ignore")
        if after.startswith(rows) and added.count("\n") == 1 and " | ORG.md | human | " in added:
            log.write_bytes(rows)  # the row the kit wrote for a change that is not there any more, and only that row
        raise Refused(f"Your principles were not saved, and ORG.md is as it was. {trouble}")


def search_memory(name: str, query: str) -> list:
    needle = query.lower().strip()
    if len(needle) < 2:
        return []
    store = store_of(name)
    hits = []
    for item in memory_files(name):
        for number, line in enumerate((store / item["path"]).read_text(errors="ignore").splitlines(), 1):
            if needle in line.lower():
                hits.append({"path": item["path"], "line": number, "text": line.strip()[:220]})
                if len(hits) >= 80:
                    return hits
    return hits


# ── dreams ──
ITEM = re.compile(r"^- \[([ xX])\] (.*)$")
MOVES = re.compile(r"^(\S+\.md): (.+)$")  # under a split: one new file and the lines that move there
NOT_CARRIED = re.compile(r"^- Not carried over: (.+)$")
NUMBERED = re.compile(r'^(\d+)\.\s+(.+?)\s+—\s+([^—]+?)(?:\s+—\s+(.*))?$')


# The two sections a person has to know apart: text only they can change, and notes Claude Code kept outside memory.
SECTION_KINDS = (("ORG.md and Steering", "yours"), ("Claude's own notes", "own-notes"), ("Unchecked facts", "unchecked"), ("Needs you", "needs"))
NEED_LINE = re.compile(r"^- (?!\[[ xX]\] )(.+)$")  # a plain line under "Needs you": no tick box, never memory
RESOLVED = "resolved: "  # how the kit's dream says a need it said before has been put right


def section_kind(name: str) -> str:
    plain = name.replace("\u2019", "'")
    return next((kind for start, kind in SECTION_KINDS if plain.startswith(start)), "")


def detail(item: dict, row: str):
    """One of the indented lines under an item, as the kit's dream writes them (.claude/commands/dream.md).

    `replaces:` is taken whole, because the line it quotes can hold anything, " · " included: for an edit it is the
    current line, for a merge the opening words of each line it stands for, separated by " ; ". Under a split, a
    line that starts with a file names the lines that move there. Every other line is facts separated by " · ".
    """
    if row.startswith("replaces:"):
        value = row[len("replaces:"):].strip()
        if item["action"].startswith("merge"):
            item["stands_for"] = [part.strip() for part in value.split(" ; ") if part.strip()]
        else:
            item["replaces"] = value[1:-1] if len(value) > 1 and value[0] == value[-1] == '"' else value
    elif item["action"].startswith("split") and MOVES.match(row):
        item["moves"].append({"file": MOVES.match(row)[1], "lines": [part.strip() for part in MOVES.match(row)[2].split(" ; ") if part.strip()]})
    else:
        for part in row.split(" · "):
            if ": " in part:
                key, value = part.split(": ", 1)
                item["details"][key.strip()] = value.strip()


PIN = " (pin)"  # how a pinned entry's first line ends. Only the dreamer's applied edit adds or removes it


def pin_change(item: dict) -> str:
    """"pin" or "unpin" for an edit item whose new line and the line it replaces differ only by the pin suffix, else empty."""
    new, old = item["quote"], item["replaces"]
    if item["action"] != "edit" or not new or not old:
        return ""
    return "pin" if new == old + PIN else "unpin" if old == new + PIN else ""


def pins_of(store: Path) -> list:
    """The pinned entries of a store, file by file: each line of project/*.md that ends with the pin suffix, as a title
    without its date or the suffix. Read only: the hub never writes the suffix."""
    out = []
    for path in sorted((store / "project").glob("*.md")):
        for number, line in enumerate(path.read_text(errors="ignore").splitlines(), 1):
            if line.rstrip().endswith(PIN):
                title = re.sub(r"^- (\[[^\]]*\] )?", "", line.rstrip()[:-len(PIN)]).strip()
                out.append({"file": f"project/{path.name}", "line": number, "title": title if len(title) <= 160 else title[:159].rstrip() + "…"})
    return out


def parse_proposal(text: str) -> dict:
    title, sections, current, group = "", [], None, ""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.startswith("# ") and not title:
            title = line[2:].strip()
        elif line.startswith("## "):
            current = {"name": line[3:].strip(), "groups": [], "text": [], "skipped": [], "needs": []}
            sections.append(current)
            group = ""
        elif current is None:
            continue
        elif line.startswith("### "):
            group = line[4:].strip()
        elif current["name"].startswith("Needs you") and NEED_LINE.match(line):
            said = NEED_LINE.match(line)[1].strip()
            current["needs"].append({"text": said[len(RESOLVED):] if said.startswith(RESOLVED) else said, "resolved": said.startswith(RESOLVED)})
        elif ITEM.match(line) and not current["name"].startswith("Needs you"):  # the kit reads no tick line there as an item
            m = ITEM.match(line)
            item = {"line": index, "checked": m[1] != " ", "group": group, "text": m[2], "n": None, "file": "", "action": "", "quote": "", "details": {},
                    "replaces": "", "stands_for": [], "moves": [], "facts": [], "pin": ""}
            found = NUMBERED.match(m[2])
            if found:
                item.update(n=int(found[1]), file=found[2], action=found[3].strip(), quote=(found[4] or "").strip().strip('"'))
            j, under = index + 1, []
            while j < len(lines) and lines[j].startswith("      "):
                under.append(lines[j].strip())
                fact = kit.FACT.match(under[-1]) if item["action"] == kit.CARRY else None
                if fact:  # one checked fact of a carry item: "+" carries it, "-" leaves it out
                    item["facts"].append({"line": j, "sign": fact[1], "file": fact[2], "text": fact[3], "machine": fact[4]})
                else:
                    detail(item, under[-1])
                j += 1
            item["pin"] = pin_change(item)
            item["id"] = kit.item_id(m[2], under)  # the kit's name for this item: its apply record and what it carries on use it
            current["groups"].append(item)
        elif NOT_CARRIED.match(line) and len(NOT_CARRIED.match(line)[1].split(" — ")) >= 3:
            file, *subject, reason = NOT_CARRIED.match(line)[1].split(" — ")
            current["skipped"].append({"file": file.strip(), "subject": " — ".join(subject).strip(), "reason": reason.strip()})
        elif not line.startswith("      "):
            current["text"].append(line)
    out = {"title": title, "sections": [{"name": s["name"], "kind": section_kind(s["name"]), "items": s["groups"], "skipped": s["skipped"], "needs": s["needs"], "text": "\n".join(s["text"]).strip()} for s in sections]}
    return out


PROPOSAL_FILE = re.compile(r"^\d{4}-\d{2}-\d{2}(-\d+)?-proposal\.md$")
CLOSED_WHY = {"applied": "This proposal was applied", "dismissed": "This proposal was dismissed", "earlier": "This proposal is from before the kit kept a record of proposals"}


def proposal_summary(store: Path, record: dict) -> dict:
    """One proposal for a list: what the kit's record says of it, and what its file holds. A closed proposal's file can be gone."""
    path = store / "dreams" / record["name"]
    text = path.read_text(errors="ignore") if path.is_file() else ""
    items = [m for m in map(ITEM.match, text.splitlines()) if m]
    return {"file": record["name"], "title": text.splitlines()[0].lstrip("# ").strip() if text else record["name"], "gone": not path.is_file(),
            "total": len(items), "changes": sum(1 for m in items if NUMBERED.match(m[2])), "ticked": sum(1 for m in items if m[1] != " "),
            "state": record["state"], "written": record["written"], "closed": record["closed"]}


def known_proposal(store: Path, file: str) -> dict:
    """The record of one proposal the store knows. A file in dreams/ that the kit's record doesn't know is not a proposal."""
    if not PROPOSAL_FILE.match(file or ""):
        raise Refused("That isn't a dream.")
    found = kit.record_of(store)
    if found["problem"]:
        raise Refused(found["problem"])
    record = next((p for p in found["proposals"] if p["name"] == file), None)
    if record is None and file in earlier_proposals(store, found["proposals"]):
        return {"name": file, "state": "earlier", "written": None, "closed": None}
    if record is None:
        raise Refused("That dream doesn't exist.")
    return record


def earlier_proposals(store: Path, known: list) -> list:
    """Proposals from before the kit kept a record: files the store's history holds and the record doesn't know. They can
    be read, and nothing else. A file git doesn't track is not one of these: no dream finished it, and the kit's check says so."""
    names = {p["name"] for p in known}
    tracked = run("git", "-C", store, "ls-files", "--", "dreams").stdout.splitlines()
    return sorted((Path(row).name for row in tracked if PROPOSAL_FILE.match(Path(row).name) and Path(row).name not in names and (store / row).is_file()), reverse=True)


def proposal_path(store: Path, file: str) -> Path:
    known_proposal(store, file)
    path = store / "dreams" / file
    if not path.is_file():
        raise Refused("That dream's file is gone.")
    return path


def open_only(store: Path, file: str, doing: str) -> Path:
    """The kit's rule: a store has one open proposal at most, and only that one is ticked and applied."""
    record = known_proposal(store, file)
    if record["state"] != "open":
        raise Refused(f"{CLOSED_WHY[record['state']]}, so nothing in it can be {doing}. Only an open proposal can.")
    return proposal_path(store, file)


def dream_repo(name: str, repos: list) -> str:
    """The folder a project's dreams run in: where its dream job runs, else its first folder. Steering is that folder's own."""
    schedule = kit.schedule_of(name)
    return schedule["cwd"] if schedule and schedule.get("cwd") in repos else repos[0]


PART_RESULTS = ("applied", "already applied", "refused", "left out")


def parts_of(row: dict) -> list:
    """What the kit's apply did with each fact of a carry item, as its record holds them. Parts not in the kit's shape are not shown."""
    parts = row.get("parts")
    if not isinstance(parts, list):
        return []
    return [{key: part[key] for key in ("sign", "file", "line", "result", "reason")} for part in parts
            if isinstance(part, dict) and part.get("sign") in ("+", "-") and all(isinstance(part.get(key), str) for key in ("file", "line", "reason")) and part.get("result") in PART_RESULTS]


def fates_of(store: Path, name: str, record: dict, ids: list) -> dict:
    """What became of each ticked item once it was applied, from the kit's record, joined to the proposal's items by id.

    `fates` is by the item's id: an item rewritten after it was applied is another item and has none. `unread`
    holds the rows that belong to no item of the proposal as it stands, among them the ticked lines the kit could
    not read as items: nothing ticked may vanish. `yours` is there while ORG.md or Steering items wait for the person.
    """
    file = record["name"]
    found = kit.apply_record(store, file)
    if found is None:
        return {"fates": {}, "unread": [], "yours": None, "record_problem": ""}
    fate = lambda row: {"result": row["result"], "reason": row["reason"], "commits": row["commits"], "kind": row["kind"], "parts": parts_of(row)}
    # While the proposal is open its record says what is the person's. Once it is closed the kit carries those items,
    # and one that was done or taken back no longer waits.
    waiting = sum(1 for row in found["rows"] if row["result"] == "yours") if record["state"] == "open" \
        else sum(1 for entry in kit.record_of(store)["left"] if entry["from"] == file and entry["waiting"] == "you")
    repos = repos_of(store)
    return {"fates": {row["key"]: fate(row) for row in found["rows"] if row["key"] in ids},
            "unread": [{"text": row["target"], **fate(row)} for row in found["rows"] if row["key"] not in ids],
            "yours": {**kit.yours_commands(store, dream_repo(name, repos)), "waiting": waiting} if waiting and repos else None,
            "record_problem": found["problem"]}


NOTE_COUNTS = re.compile(r"^(.+?) — (facts: \d+(?: · [a-z ]+: \d+)*)(?: · nothing here because: (.+))?$")


def side_notes(store: Path, file: str):
    """What the dream read and did not propose, from the file it writes beside the proposal (<stem>-notes.md), or None
    for a proposal from before there were such files. Read only: the dream writes it and the kit commits it with the proposal.
    {"file", "sections": [{"name", "lines": [...], "notes": [{"note", "counts": [[word, n]], "because"}]}]}"""
    path = store / "dreams" / (file[:-len("-proposal.md")] + "-notes.md")
    if not path.is_file():
        return None
    sections, current = [], None
    for line in path.read_text(errors="ignore").splitlines():
        if line.startswith("## "):
            current = {"name": line[3:].strip(), "lines": [], "notes": []}
            sections.append(current)
        elif current is None or not line.strip():
            continue
        elif current["name"].startswith("Claude's own notes") and NOTE_COUNTS.match(line[2:] if line.startswith("- ") else ""):
            m = NOTE_COUNTS.match(line[2:])
            counts = [[word, int(n)] for word, n in (part.rsplit(": ", 1) for part in m[2].split(" · "))]
            current["notes"].append({"note": m[1].strip(), "counts": counts, "because": (m[3] or "").strip()})
        else:
            current["lines"].append(line[2:] if line.startswith("- ") else line.strip())
    return {"file": path.name, "sections": [s for s in sections if s["lines"] or s["notes"]]}


def read_dream(name: str, file: str) -> dict:
    store = store_of(name)
    record = known_proposal(store, file)
    if not (store / "dreams" / file).is_file():  # a closed proposal whose file was removed: say so; there is nothing to read
        return {"title": file, "sections": [], "file": file, "hash": "", "gone": True, "state": record["state"], "written": record["written"], "closed": record["closed"],
                "fates": {}, "unread": [], "yours": None, "record_problem": "", "notes": side_notes(store, file)}
    text = proposal_path(store, file).read_text(errors="ignore")
    parsed = parse_proposal(text)
    ids = [item["id"] for section in parsed["sections"] for item in section["items"]]
    return {**parsed, "file": file, "hash": sha(text), "gone": False, "state": record["state"], "written": record["written"], "closed": record["closed"],
            "notes": side_notes(store, file), **fates_of(store, name, record, ids)}


ITEM_PARTS = re.compile(r'^(.+?)\s+—\s+([^—]+?)(?:\s+—\s+(.*))?$')  # <file> — <action> — "<line>", as after an item's number


def sentence(way: str) -> str:
    """The kit's words on what gets a waiting item done, as a sentence of their own. Where the kit points at its own
    command for clearing a dead end, the hub has a button, and says that."""
    if way.startswith(kit.DEAD_END) and way.endswith(kit.DEAD_END_TAIL):
        way = way[:-len(kit.DEAD_END_TAIL)] + ". Take it back to clear it"
    return way[:1].upper() + way[1:] + ("" if not way or way.endswith(".") else ".")


def carried_item(entry: dict, ways: dict, declined: list) -> dict:
    """An item a closed proposal left, as the kit's record carries it, read like a proposal's item.

    `waiting` is the kit's word and the only thing that sorts it: "you" or "a session" means the person approved it
    and it is not done yet; null means it goes to the next dream, or is declined when the kit lists it as declined
    (`declined`, the kit's own list: how often and how long ago are the kit's numbers).
    `ticked` only says it was ticked. It is named by `from` and `id`: `number` can be missing and repeats across
    proposals. `way` is the kit's own sentence on what gets a waiting item done, or why nothing can any more.
    """
    item = {"action": "", "details": {}, "replaces": "", "stands_for": [], "moves": []}
    found = ITEM_PARTS.match(entry["item"])
    item.update(file=found[1] if found else "", action=found[2].strip() if found else "", quote=(found[3] or "").strip().strip('"') if found else entry["item"])
    for row in entry["detail"]:
        detail(item, row)
    way = ways.get((entry["item"], entry["from"]), "") if entry["waiting"] else ""
    return {"id": entry["id"], "from": entry["from"], "item": entry["item"], "number": entry["number"], "at": entry["at"],
            "file": item["file"], "action": item["action"], "quote": item["quote"] or item["file"], "why": item["details"].get("why", ""), "replaces": item["replaces"],
            "ticked": entry["ticked"], "left": entry["left"], "waiting": entry["waiting"], "declined": not entry["waiting"] and entry["item"] in declined,
            "way": sentence(way), "dead": way.startswith(kit.DEAD_END)}


def dream_metrics(store: Path) -> list:
    history = store / "dreams" / "metrics.jsonl"
    return [json.loads(line) for line in history.read_text().splitlines() if line.strip()] if history.exists() else []


NEED = {"text": str, "from": str, "last": str, "said": int}


def needs_of(store: Path) -> dict:
    """What dreams said needs the person and nobody has closed, oldest first, from the kit's dreams/needs.json. Not
    memory, and the hub never writes the file: the person closes one through the kit's `done`.
    {"open": [{"text", "from", "last", "at", "said"}], "problem": ""}. A store with no file has none."""
    path = store / "dreams" / "needs.json"
    if not path.exists():
        return {"open": [], "problem": ""}
    try:
        found = json.loads(path.read_text())["needs"]
        good = isinstance(found, list) and all(isinstance(e, dict) and all(type(e.get(k)) is t for k, t in NEED.items()) and isinstance(e.get("at"), (int, float)) for e in found)
    except (OSError, ValueError, KeyError, TypeError):
        good = False
    if not good:
        damaged(path, "it isn't what scripts/needs.py writes")
        return {"open": [], "problem": f"The kit's list of what needs you can't be read. Look at {str(path).replace(str(HOME), '~', 1)}."}
    return {"open": [{k: e[k] for k in ("text", "from", "last", "at", "said")} for e in sorted(found, key=lambda e: e["at"])], "problem": ""}


def dreams_of(name: str) -> dict:
    """The Dreams page: the proposals the kit knows, newest first, then the ones from before it kept a record; what waits;
    who is dreaming; what the kit's check finds; and what was last done here that only the person may do.

    Each answer is the kit's. When the kit doesn't answer one of them in time, that is said once as the page's problem
    and the rest is still shown."""
    store = store_of(name)
    found = kit.record_of(store)
    problem = found["problem"]

    def asked(question, otherwise):
        nonlocal problem
        try:
            return question()
        except Refused as quiet:
            problem = problem or str(quiet)
            return otherwise

    ways = asked(lambda: kit.said_bare(store)["ways"], {}) if any(entry["waiting"] for entry in found["left"]) else {}
    declined = asked(lambda: kit.declined(store), []) if any(not entry["waiting"] for entry in found["left"]) else []
    held, checks = asked(lambda: kit.dreamer(store), ""), asked(lambda: kit.problems(store), [])
    return {"dreams": [proposal_summary(store, p) for p in reversed(found["proposals"])], "earlier": earlier_proposals(store, found["proposals"]),
            "metrics": dream_metrics(store), "problem": problem, "left": [carried_item(entry, ways, declined) for entry in found["left"]],
            "held": held, "checks": checks, "actions": recent_actions(name, store), "needs": needs_of(store), "pins": pins_of(store)}


def tick(name: str, body: dict):
    store = store_of(name)
    file, index = text_in(body, "file", "which proposal"), number_in(body, "line", "The line", 0, 10_000_000)
    if not isinstance(body.get("expect"), str) or not isinstance(body.get("checked"), bool):
        raise Malformed("Say whether the box is ticked, and which version of the proposal you saw.")
    path = open_only(store, file, "ticked")
    with write_lock:
        text = path.read_text()
        if sha(text) != body.get("expect"):
            raise Refused("The proposal changed. Reload and try again.")
        lines = text.split("\n")
        if not index < len(lines) or not ITEM.match(lines[index]):
            raise Refused("That isn't a change in this proposal.")
        lines[index] = re.sub(r"^- \[[ xX]\]", "- [x]" if body.get("checked") else "- [ ]", lines[index], count=1)
        new = "\n".join(lines)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(new)
        os.replace(tmp, path)
    return {"hash": sha(new)}


def fact_sign(name: str, body: dict):
    """The person carries one fact of a carry item, or leaves it out: the sign at the start of its line becomes "+" or
    "-", and nothing else in the proposal changes. Open proposals only, and only if the file is the one the person saw."""
    store = store_of(name)
    file, index = text_in(body, "file", "which proposal"), number_in(body, "line", "The line", 0, 10_000_000)
    if not isinstance(body.get("expect"), str) or not isinstance(body.get("carry"), bool):
        raise Malformed("Say whether the fact is carried, and which version of the proposal you saw.")
    path = open_only(store, file, "changed")
    with write_lock:
        text = path.read_text()
        if sha(text) != body.get("expect"):
            raise Refused("The proposal changed. Reload and try again.")
        lines = text.split("\n")
        owner = next((i for i in range(min(index, len(lines)) - 1, -1, -1) if not lines[i].startswith("      ")), None)
        head = ITEM.match(lines[owner]) if owner is not None else None
        if not (index < len(lines) and lines[index].startswith("      ") and kit.FACT.match(lines[index].strip()) and head and f" — {kit.CARRY} — " in head[2]):
            raise Refused("That isn't a fact of a carry item in this proposal.")
        at = len(lines[index]) - len(lines[index].lstrip())
        lines[index] = lines[index][:at] + ("+" if body["carry"] else "-") + lines[index][at + 1:]
        new = "\n".join(lines)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(new)
        os.replace(tmp, path)
    return {"hash": sha(new)}


def close_need(name: str, body: dict):
    """The person closes one thing a dream said needs them: dealt with, or not wanted. The kit's `done` finds it by words
    only it holds, writes its list and commits; the hub shows what it said."""
    store, words = store_of(name), text_in(body, "words", "which one")
    the_person("close something that needs you")
    said = kit.done(store, words)
    record_action(name, "done", said)
    return {"said": said}


def dream_session(name: str) -> str:
    return f"hub-dream-{name}"


def dream_running(name: str, sessions: set) -> bool:
    return dream_session(name) in sessions or kit.job_running(name)


def start_dream(name: str):
    """Start a dream by hand. The kit turns one away while another is alive or a proposal is open, with a sentence that
    would pass unseen inside tmux, so the same two questions are asked here first and the answer is shown."""
    store = store_of(name)
    if dream_running(name, tmux_sessions()):
        raise Refused("It's already dreaming.")
    repos = repos_of(store)
    if not repos:
        raise Refused("This project has no folder.")
    held = kit.dreamer(store)
    if held:
        raise Refused(held, held=True)
    waiting = kit.open_proposal(store)
    if waiting:
        raise Refused(waiting)
    cwd = dream_repo(name, repos)
    result = tmux("new-session", "-d", "-s", dream_session(name), "-c", cwd, "-e", f"PATH={ENV['PATH']}", kit.dream_command(cwd))
    if result.returncode != 0:
        raise Refused("The dream didn't start: " + result.stderr.strip()[:200])
    return {"ok": True}


def release_dream(name: str, body: dict):
    """The person gives up the dream that holds the project. What the kit says it discarded is shown."""
    store = store_of(name)
    the_person("give up a dream")
    said = kit.release(store)
    record_action(name, "release", said)
    return {"said": said}


def set_schedule(name: str, body: dict):
    store = store_of(name)
    repos = repos_of(store)
    if not repos:
        raise Refused("This project has no folder.")
    if not isinstance(body.get("on"), bool):
        raise Malformed("Say whether automatic dreams are on or off.")
    kit.set_schedule((kit.schedule_of(name) or {}).get("cwd") or repos[0], body["on"], number_in(body, "hour", "The hour", 0, 23) if body["on"] else 0)
    return {"ok": True}


def apply_dream(name: str, body: dict):
    store = store_of(name)
    path = open_only(store, text_in(body, "file", "which proposal"), "applied")
    if not any(m and m[1] != " " for m in map(ITEM.match, path.read_text(errors="ignore").splitlines())):
        raise Refused("Nothing is ticked, so there is nothing to apply. To close it without applying, dismiss it.")
    repos = repos_of(store)
    return start_sessions({"project": name, "repo": dream_repo(name, repos), "tasks": [kit.SESSION_PASS], "isolate": False,
                           "titles": [f"Apply dream {path.name[:-len('-proposal.md')]}"]})


def yours_waiting(store: Path) -> int:
    """How many ORG.md and Steering changes the kit's own pass would offer the person: what closed proposals left waiting
    for them, and what the apply record of the open proposal calls theirs. The kit's --yours covers both."""
    found = kit.record_of(store)
    held = next((p for p in found["proposals"] if p["state"] == "open"), None)
    rows = (kit.apply_record(store, held["name"]) or {}).get("rows", []) if held else []
    return sum(1 for entry in found["left"] if entry["waiting"] == "you") + sum(1 for row in rows if row["result"] == "yours")


def yours_session(name: str, body: dict):
    """A terminal for the person's pass over the ticked ORG.md and Steering items: the kit's own script, which shows each
    change and asks for a typed yes. The hub starts it and shows what it says. It never lifts the lock on ORG.md: while
    the lock is on, the script says so and how to lift it, and the terminal stays open for the person to do that."""
    store = store_of(name)
    repos = repos_of(store)
    if not repos:
        raise Refused("This project has no folder.")
    if not yours_waiting(store):
        raise Refused("No ORG.md or Steering change is waiting for you.")
    sid, folder, title = secrets.token_hex(4), dream_repo(name, repos), "Apply your ORG.md and Steering changes"
    result = tmux("new-session", "-d", "-s", f"hub-{sid}", "-x", "120", "-y", "36", "-c", folder, *session_env(sid), f"{kit.YOURS_PASS}; exec /bin/zsh -l")
    if result.returncode != 0:
        raise Refused("The terminal didn't start: " + result.stderr.strip()[:200])
    (META / f"{sid}.json").write_text(json.dumps({"id": sid, "project": name, "repo": folder, "title": title, "kind": "shell", "isolate": False, "created": time.time(),
                                                   "task": kit.YOURS_PASS, "folder": folder, "mode": "", "model": "", "effort": "", "agent": ""}))
    return {"ids": [sid]}


def waiting_session(name: str, body: dict):
    """A session for what the person approved and only a session can do (a skill to write, a scratchpad to condense, an
    inbox item to accept): `/dream apply` does them and reports each, whether or not a proposal is open."""
    store = store_of(name)
    repos = repos_of(store)
    if not any(entry["waiting"] == "a session" for entry in kit.record_of(store)["left"]):
        raise Refused("Nothing approved is waiting for a session.")
    return start_sessions({"project": name, "repo": dream_repo(name, repos), "tasks": [kit.SESSION_PASS], "isolate": False, "titles": ["Do what waits for a session"]})


def withdraw_item(name: str, body: dict):
    """The person takes back an approval nobody has carried out. The kit finds the item by words only it holds: the whole item is that."""
    store, item = store_of(name), text_in(body, "item", "which item")  # an empty one would fit every item
    the_person("take back an approval")
    said = kit.withdraw(store, item)
    record_action(name, "withdraw", said)
    return {"said": said}


def dismiss_dream(name: str, body: dict):
    """The person closes the open proposal without applying it: its items go to the next dream, and none counts as a no."""
    store = store_of(name)
    file = text_in(body, "file", "which proposal")
    open_only(store, file, "dismissed")
    the_person("dismiss a proposal")
    said = kit.dismiss(store, file)
    record_action(name, "dismiss", said)
    return {"said": said}


def forget_item(name: str, body: dict):
    """The person lets a declined item be proposed again. The kit finds it by words only it holds: the whole item is that."""
    store, item = store_of(name), text_in(body, "item", "which item")  # an empty one would fit every item
    the_person("allow a declined item again")
    said = kit.forget(store, item)
    record_action(name, "forget", said)
    return {"said": said}


# ── sessions ──
def tmux_sessions() -> set:
    result = tmux("list-sessions", "-F", "#{session_name}")
    return set(result.stdout.split()) if result.returncode == 0 else set()


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:32] or "agent"


def title_from(task: str, project: str) -> str:
    words = task.split()
    return " ".join(words[:5]) + ("…" if len(words) > 5 else "") if words else project


def make_worktree(repo: str, name: str) -> str:
    """Plain `git worktree add`, so the kit's post-checkout hook links memory/. Inside the repo, so its trust carries over."""
    path = Path(repo) / ".claude" / "worktrees" / name
    common = run("git", "-C", repo, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()
    exclude = Path(common) / "info" / "exclude"
    current = exclude.read_text() if exclude.exists() else ""
    if "/.claude/worktrees/" not in current.split("\n"):
        exclude.write_text(current.rstrip("\n") + ("\n" if current else "") + "/.claude/worktrees/\n")
    result = run("git", "-C", repo, "worktree", "add", "-q", "-b", f"hub/{name}", path, "HEAD")
    if result.returncode != 0:
        raise Refused("The worktree wasn't created: " + result.stderr.strip()[:200])
    return str(path)


def session_env(sid: str) -> list:
    """What a session's shell is told: which session it is, and which hub's state folder its hook writes to."""
    return ["-e", f"HUB_SESSION={sid}", "-e", f"HUB_STATE={STATE}", "-e", f"PATH={ENV['PATH']}", "-e", "FORCE_HYPERLINK=1"]


def start_sessions(body: dict) -> dict:
    project = str(body.get("project", ""))
    store = store_of(project)
    repos = repos_of(store)
    repo = body.get("repo") or (repos[0] if repos else "")
    if repo not in repos:
        raise Refused("That folder isn't part of this project.")
    kind = "shell" if body.get("kind") == "shell" else "claude"
    tasks = [t.strip() for t in body.get("tasks", []) if t.strip()][:8] or [""]
    titles = body.get("titles") or []
    isolate = bool(body.get("isolate")) and kind == "claude"
    if isolate and off_limits(repo):
        raise Refused(f"A session of its own needs a git worktree, and the hub never runs git in {Path(repo).name}. Start it without one.")
    mode, model, effort, agent = (str(body.get(key) or "") for key in ("mode", "model", "effort", "agent"))
    if mode and mode not in MODES:
        raise Refused("That isn't a mode.")
    if model and model not in MODELS:
        raise Refused("That isn't a model.")
    if effort and effort not in EFFORTS:
        raise Refused("That isn't an effort level.")
    if agent and agent not in {a["name"] for a in agents.definitions(repos)}:
        raise Refused("That agent doesn't exist in this project.")
    made = []
    for index, task in enumerate(tasks):
        sid = secrets.token_hex(4)
        title = str(titles[index]) if index < len(titles) else title_from(task, project) if task else f"{project} {sid[:4]}"
        title = title.lstrip("- ")[:80] or project  # a title is passed as a flag value, so it must not look like a flag
        if kind == "shell":
            command = "exec /bin/zsh -l"
        else:
            cmd = [CLAUDE]
            if task:
                cmd.append(" " + task if task.startswith("-") else task)
            cmd += ["--settings", str(HOOKS), "--remote-control", title]
            cmd += ["--permission-mode", mode] if mode else []
            cmd += ["--model", model] if model else []
            cmd += ["--effort", effort] if effort else []
            cmd += ["--agent", agent] if agent else []
            command = f"{shlex.join(cmd)}; exec /bin/zsh -l"
        folder = make_worktree(repo, f"{slug(title)}-{sid[:4]}") if isolate else repo
        result = tmux("new-session", "-d", "-s", f"hub-{sid}", "-x", "120", "-y", "36", "-c", folder, *session_env(sid), command)
        if result.returncode != 0:
            raise Refused("The session didn't start: " + result.stderr.strip()[:200])
        (META / f"{sid}.json").write_text(json.dumps({"id": sid, "project": project, "repo": repo, "title": title, "kind": kind,
                                                       "isolate": isolate, "created": time.time(), "task": task[:400], "folder": folder,
                                                       "mode": mode, "model": model, "effort": effort, "agent": agent}))
        made.append(sid)
    return {"ids": made}


def session_meta(sid: str) -> dict:
    if not SESSION_ID.match(sid or ""):
        raise Refused("That isn't a session.")
    path = META / f"{sid}.json"
    if not path.exists():
        raise Refused("That session doesn't exist.")
    return json.loads(path.read_text())


def rename_session(sid: str, body: dict):
    session_meta(sid)
    title = " ".join(str((body or {}).get("title", "")).split())[:80]
    if not title:
        raise Refused("Give the session a name.")
    path = META / f"{sid}.json"
    with write_lock:
        meta = json.loads(path.read_text())
        meta["title"] = title
        spare = path.with_suffix(".tmp")
        spare.write_text(json.dumps(meta))
        os.utime(spare, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns))  # the sidebar orders by this
        os.replace(spare, path)
    return {"ok": True, "title": title}


def stop_session(sid: str):
    session_meta(sid)
    tmux("kill-session", "-t", f"hub-{sid}")
    return {"ok": True}


def dismiss_session(sid: str):
    session_meta(sid)
    tmux("kill-session", "-t", f"hub-{sid}")
    (META / f"{sid}.json").unlink(missing_ok=True)
    (EVENTS / f"{sid}.json").unlink(missing_ok=True)
    shutil.rmtree(UPLOADS / sid, ignore_errors=True)
    shutil.rmtree(RECORDS / sid, ignore_errors=True)
    agents.forget(sid)
    status.forget(sid)
    return {"ok": True}


# ── deleting: the session leaves the hub, and its conversation goes to the Trash, where Finder can still get it back ──
TRASH = HOME / ".Trash"
CLAUDE_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def transcripts_of(sid: str, meta: dict) -> list:
    """The session's own transcripts in Claude Code's folder, each a file `<projects>/<one folder>/<uuid>.jsonl`.
    Read from where the hub itself recorded them (the hook's event file, the record of a close), never from a request,
    and only kept when the path really resolves to such a file: no links, nothing outside ~/.claude/projects."""
    root = (HOME / ".claude" / "projects").resolve()
    named = []
    try:
        named.append(json.loads((EVENTS / f"{sid}.json").read_text()).get("transcript", ""))
    except (OSError, ValueError, AttributeError):
        pass
    named.append(meta.get("transcript", ""))
    found = []
    for value in named:
        if not isinstance(value, str) or not value:
            continue
        path = Path(value)
        if path.is_symlink() or not path.is_file():
            continue
        real = path.resolve()
        if real.parent.parent != root or not real.name.endswith(".jsonl") or not CLAUDE_ID.match(real.stem) or real in found:
            continue
        found.append(real)
    return found


def side_folder(transcript: Path):
    """The folder Claude Code keeps beside a transcript (its subagents and tool results), when there is one."""
    folder = transcript.with_suffix("")
    return folder if folder.is_dir() and not folder.is_symlink() else None


def size_words(size: int) -> str:
    for unit, step in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if size >= step:
            return f"{size / step:.1f} {unit}"
    return f"{size} bytes"


def delete_check(sid: str) -> dict:
    """Everything the delete dialog says, asked before the person confirms: the session, whether it runs, what moves to
    the Trash, and whether the memory kit has read it, in the kit's own record."""
    meta = session_meta(sid)
    target = f"hub-{sid}"
    alive = target in tmux_sessions()
    files = transcripts_of(sid, meta) if meta.get("kind") == "claude" else []
    kept = RECORDS / sid / "transcript.jsonl"
    size = sum(p.stat().st_size for p in files) or (kept.stat().st_size if kept.exists() else 0)
    times = [p.stat().st_mtime for p in files] + ([kept.stat().st_mtime] if kept.exists() else [])
    closed = (meta.get("closed") or {}).get("at")
    last = max(times + [t for t in (closed, meta.get("resumed"), meta.get("created")) if isinstance(t, (int, float))] or [0])
    uploads = [p for p in (UPLOADS / sid).iterdir() if p.is_file()] if (UPLOADS / sid).is_dir() else []
    out = {"title": meta["title"], "project": meta["project"], "kind": meta.get("kind"), "alive": alive,
           "working": alive and "esc to interrupt" in pane_of(target).lower(), "closing": bool(meta.get("closing")),
           "created": meta.get("created"), "last": last or None, "size": size_words(size) if size else "",
           "transcripts": len(files), "record": kept.exists(), "uploads": len(uploads), "kit": None}
    if meta.get("kind") == "claude":
        claude_id = files[0].stem if files else meta.get("claude_session", "")
        dreamed = kit.dreamed_until(meta["project"])
        started = meta.get("created") or last
        # The kit reads a moment from its second on (slim.within), so anything in the second of the last dream is still unread.
        read = "none" if dreamed is None else "all" if int(last) < int(dreamed) else "part" if int(started) < int(dreamed) else "none"
        out["kit"] = {"read": read, "dreamed": dreamed, "archive": kit.archive_copy(meta["project"], claude_id) if claude_id else "",
                      "keep_days": kit.ARCHIVE_KEEP_DAYS}
    return out


def _trash_folder(meta: dict) -> Path:
    """A new folder in the Trash for this one session, with a name nothing else there has."""
    stem = f"Dream Hub session {slug(meta.get('title', ''))} {meta['id']} {time.strftime('%Y-%m-%d %H.%M.%S')}"
    for n in range(1, 100):
        folder = TRASH / (stem if n == 1 else f"{stem} {n}")
        try:
            folder.mkdir(parents=False)
            return folder
        except FileExistsError:
            continue
    raise Refused("The Trash already has folders of that name, so nothing was deleted.")


def _ended(pids: set, seconds: float = 6) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        alive = set()
        for pid in pids:
            try:
                os.kill(pid, 0)
                alive.add(pid)
            except ProcessLookupError:
                pass
            except PermissionError:
                alive.add(pid)
        if not alive:
            return True
        pids = alive
        time.sleep(0.2)
    return False


def delete_session(sid: str):
    """Remove a session from the hub for good: end it if it still runs (its own tmux session, by exact name, and
    nothing else), move its conversation and what the hub kept of it to one new folder in the Trash, and forget the
    rest. The kit's archive is never touched."""
    meta = session_meta(sid)
    if meta.get("closing"):
        raise Refused("This session is closing now. Delete it once it has closed.")
    if not TRASH.is_dir():
        raise Refused("There is no Trash folder in your home folder, so nothing was deleted.")
    target = f"hub-{sid}"
    stopped = False
    if target in tmux_sessions():
        panes = tmux("list-panes", "-t", f"={target}", "-F", "#{pane_pid}").stdout.split()
        children = status.processes()
        pids = set()
        for pane in panes:
            if pane.isdigit():
                pids |= status.tree_of(int(pane), children)
        result = tmux("kill-session", "-t", f"={target}")
        if result.returncode != 0 and target in tmux_sessions():
            raise Refused("The session couldn't be stopped, so nothing was deleted.")
        stopped = True
        if pids and not _ended(pids):
            raise Refused("The session was stopped, but Claude hasn't finished exiting, so nothing was deleted yet. Try again in a moment.")
    files = transcripts_of(sid, meta) if meta.get("kind") == "claude" else []
    try:
        folder = _trash_folder(meta)
    except OSError as error:
        raise Refused(f"The Trash can't be written to ({error.strerror or error}), so nothing was deleted.")
    moved, where = [], []
    try:
        for transcript in files:
            shutil.move(str(transcript), str(folder / transcript.name))
            moved.append(transcript.name); where.append(f"{transcript.name}  was  {transcript}")
            side = side_folder(transcript)
            if side is not None:
                shutil.move(str(side), str(folder / side.name))
                moved.append(side.name + "/"); where.append(f"{side.name}/  was  {side}")
        for name, source in (("hub record", RECORDS / sid), ("uploads", UPLOADS / sid)):
            if source.is_dir() and not source.is_symlink():
                shutil.move(str(source), str(folder / name))
                moved.append(name + "/"); where.append(f"{name}/  was  {source}")
        shutil.move(str(META / f"{sid}.json"), str(folder / "hub session.json"))
        where.append(f"hub session.json  was  {META / f'{sid}.json'}")
    except OSError as error:
        raise Refused(f"Deleting stopped part way ({error.strerror or error}). Moved to the Trash so far: {', '.join(moved) or 'nothing'}, in {folder.name}.")
    (folder / "Where these came from.txt").write_text(
        f"Dream Hub session “{meta.get('title', '')}” ({sid}) of {meta.get('project', '')}, deleted {time.strftime('%Y-%m-%d %H:%M:%S')}.\n"
        "To undo, move each item back to where it was:\n\n" + "\n".join(where) + "\n")
    (EVENTS / f"{sid}.json").unlink(missing_ok=True)
    agents.forget(sid)
    status.forget(sid)
    # A poll that had already read the session may have copied its transcript again, or a hook written once more.
    shutil.rmtree(RECORDS / sid, ignore_errors=True)
    (EVENTS / f"{sid}.json").unlink(missing_ok=True)
    return {"ok": True, "stopped": stopped, "trash": str(folder).replace(str(HOME), "~", 1), "moved": moved}


# ── closing and resuming: a closed session ends its process and keeps a record that can be read or resumed ──
HANDOFF_MARK = "[Dream Hub] This session is closing."
HANDOFF_MARKS = (HANDOFF_MARK, "[Agent" + " Hub] This session is closing.")  # this hub's, and the one older records carry
said_closing = lambda text: any(mark in text for mark in HANDOFF_MARKS)
def handoff_prompt(folder: str) -> str:
    """Sent as one line: the tidy-up stays inside this session's own folders and leaves shared things alone."""
    return (HANDOFF_MARK + " First tidy up after yourself. "
            "Stop local servers, watchers and background processes you started in this session. "
            "Never stop launchd services, Docker containers, or anything you didn't start. "
            "If you wait for a process to exit, wait on its PID or use pgrep -x, never pgrep -f. "
            f"Delete only test files, screenshots and build output you created, and only inside your own session temp folder "
            f"(/private/tmp/claude-{os.getuid()}/…) or this project folder ({folder}). "
            "Never delete anything elsewhere: not ~/Library/Caches, not the npm, pip, Homebrew or other shared caches. "
            "Never delete work, commits, the user's files or anything you aren't sure about. "
            "If this mode doesn't let you run commands, skip the tidy-up. "
            "Then reply with a handoff in exactly four short lines and nothing else: "
            "Done: what this session finished / Open: what is unfinished or uncertain / "
            "Next: the first thing to do when it resumes / Cleaned: what you stopped or removed, or nothing. "
            "Put each on its own line.")


SCRIPT_RUNNERS = re.compile(r"^(node|bun|deno|ruby|java|php|python\d*(\.\d+)?|Python)$")


def server_name(args: str, command: str) -> str:
    """What to call a server: the script a runtime is running (vite, server.py, http.server), else the program."""
    words = args.split()
    exe = Path(words[0]).name if words else command
    if not SCRIPT_RUNNERS.match(exe):
        return exe or command
    for index, word in enumerate(words[1:], 1):
        if words[index - 1] == "-m":
            return word
        if not word.startswith("-"):
            name = Path(word).name
            return re.sub(r"\.(c|m)?js$", "", name) or exe
    return exe


def still_running(folder: str) -> list:
    """Servers still listening that were started from this folder: shown, never stopped. Launchd jobs and processes
    inside a running hub session are left out, since those are meant to be running."""
    if not folder or not Path(folder).is_dir():
        return []
    root = str(Path(folder).resolve())
    listing = run("lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-Fpcn", timeout=10)
    ports, names, pid = {}, {}, None
    for line in listing.stdout.splitlines():
        if line.startswith("p"):
            pid = int(line[1:])
        elif line.startswith("c") and pid:
            names[pid] = line[1:]
        elif line.startswith("n") and pid:
            port = line.rsplit(":", 1)[-1]
            if port.isdigit():
                ports.setdefault(pid, set()).add(int(port))
    if not ports:
        return []
    cwd, pid = {}, None
    for line in run("lsof", "-a", "-d", "cwd", "-Fn", "-p", ",".join(map(str, ports)), timeout=10).stdout.splitlines():
        if line.startswith("p"):
            pid = int(line[1:])
        elif line.startswith("n") and pid:
            cwd[pid] = line[1:]
    inside = [p for p, path in cwd.items() if path == root or path.startswith(root + "/")]
    if not inside:
        return []
    managed = {int(row.split()[0]) for row in run("launchctl", "list", timeout=10).stdout.splitlines()[1:] if row.split() and row.split()[0].isdigit()}
    children, keep = process_tree(), set()
    stack = [int(p) for p in tmux("list-panes", "-a", "-F", "#{pane_pid}").stdout.split() if p.isdigit()]
    while stack:  # everything under a hub session that is still running belongs to that session
        p = stack.pop()
        if p not in keep:
            keep.add(p)
            stack += children.get(p, [])
    found = []
    shown = [p for p in inside if p not in managed and p not in keep and p != os.getpid()]
    details = run("ps", "-o", "pid=,etime=,args=", "-p", ",".join(map(str, shown)), timeout=10).stdout.splitlines() if shown else []
    for row in details:
        parts = row.split(None, 2)
        if len(parts) < 3 or not parts[0].isdigit():
            continue
        p, elapsed, args = int(parts[0]), parts[1], parts[2]
        days, _, clock = elapsed.rpartition("-")
        units = [int(x) for x in clock.split(":")]
        seconds = (int(days) if days else 0) * 86400 + sum(u * 60 ** i for i, u in enumerate(reversed(units)))
        found.append({"pid": p, "name": server_name(args, names.get(p, "")), "ports": sorted(ports[p]),
                      "since": time.time() - seconds, "folder": cwd[p].replace(str(HOME), "~", 1)})
    return sorted(found, key=lambda s: s["ports"][0])
HANDOFF_WAIT = 600  # tidying up can take a few minutes
_closing = set()  # sessions a thread is closing right now
_close_lock = threading.Lock()


def update_meta(sid: str, change) -> dict:
    """Read, change and write a session's record in one step, keeping its place in the sidebar."""
    path = META / f"{sid}.json"
    with write_lock:
        meta = json.loads(path.read_text())
        change(meta)
        spare = path.with_suffix(".tmp")
        spare.write_text(json.dumps(meta))
        os.utime(spare, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns))
        os.replace(spare, path)
    return meta


def snapshot(sid: str):
    """Copy the transcript and its subagents into the hub, whole or not at all. Returns Claude's session id."""
    live = live_transcript(sid)
    if live is None:
        return None
    target, spare = RECORDS / sid, RECORDS / f".{sid}.{os.getpid()}"
    shutil.rmtree(spare, ignore_errors=True)
    spare.mkdir(parents=True)
    shutil.copy2(live, spare / "transcript.jsonl")
    subagents = live.with_suffix("") / "subagents"
    if subagents.is_dir():
        shutil.copytree(subagents, spare / "transcript" / "subagents")
    old = RECORDS / f".{sid}.old"
    shutil.rmtree(old, ignore_errors=True)
    if target.exists():
        os.replace(target, old)
    os.replace(spare, target)
    shutil.rmtree(old, ignore_errors=True)
    update_meta(sid, lambda m: m.update(claude_session=live.stem, transcript=str(live)))
    return live.stem


def folder_changes(folder: str) -> int:
    if not folder or not Path(folder).is_dir():
        return 0
    result = run("git", "-C", folder, "status", "--porcelain", timeout=10)
    return len(result.stdout.splitlines()) if result.returncode == 0 else 0


def close_check(sid: str) -> dict:
    """What closing would leave behind, asked before the person confirms."""
    meta = session_meta(sid)
    folder = meta.get("folder") or meta.get("repo") or ""
    target = f"hub-{sid}"
    alive = target in tmux_sessions()
    pane = pane_of(target) if alive else ""
    return {"changes": folder_changes(folder), "folder": folder.replace(str(HOME), "~", 1), "alive": alive,
            "working": "esc to interrupt" in pane.lower(), "typed": bool(typed_now(target)) if alive else False}


def finish_close(sid: str, note: str = ""):
    try:
        meta = session_meta(sid)
    except Refused:
        return
    if meta.get("kind") == "claude":
        try:
            snapshot(sid)
        except OSError as error:  # the transcript stays where Claude keeps it; closing still goes ahead
            print(f"error: snapshot {sid}: {error}", flush=True)
    tmux("kill-session", "-t", f"hub-{sid}")
    update_meta(sid, lambda m: (m.pop("closing", None), m.update(closed={"at": time.time(), "note": note.strip()[:2000]})))


def handoff_reply(sid: str) -> str:
    """Claude's last words after the handoff prompt. The Stop hook can fire before they reach the transcript, so this is read, not assumed."""
    transcript = live_transcript(sid)
    if transcript is None:
        return ""
    reply = None
    for raw in transcript.read_text(errors="ignore").splitlines():
        try:
            entry = json.loads(raw)
        except ValueError:
            continue
        if entry.get("isMeta") or entry.get("isSidechain") or entry.get("type") not in ("user", "assistant"):
            continue
        content = (entry.get("message") or {}).get("content")
        text = _text_of(content).strip()
        if entry["type"] == "user" and said_closing(text):
            reply = ""
        elif entry["type"] == "assistant" and reply is not None and text:
            reply = text
    return reply or ""


def _wait_for_handoff(sid: str, asked_at: float):
    note = ""
    try:
        deadline = asked_at + HANDOFF_WAIT
        while time.time() < deadline:
            time.sleep(2)
            if f"hub-{sid}" not in tmux_sessions():
                break
            try:
                event = json.loads((EVENTS / f"{sid}.json").read_text())
            except (OSError, ValueError):
                continue
            if event.get("state") == "finished" and float(event.get("at", 0)) > asked_at + 0.5:
                note = handoff_reply(sid)
                if note:
                    break
    finally:
        finish_close(sid, note)
        with _close_lock:
            _closing.discard(sid)


def paste_prompt(target: str, text: str, buffer: str):
    """Give Claude a prompt the way a person would paste one, then press Enter. Typed keys this long arrive as a paste
    and Claude keeps only the tail, so it goes in as one bracketed paste through a tmux buffer. Enter waits until the
    paste is seen in Claude's input, and is pressed again only while the text is still sitting there well after:
    Claude can drop an Enter that comes right behind a paste."""
    tmux("set-buffer", "-b", buffer, text)
    tmux("paste-buffer", "-p", "-d", "-b", buffer, "-t", target)
    end = time.time() + 3
    time.sleep(0.3)
    while time.time() < end and not typed_now(target):
        time.sleep(0.15)
    for _ in range(3):
        time.sleep(0.5)
        tmux("send-keys", "-t", target, "Enter")
        gone = time.time() + 2.5
        while time.time() < gone and typed_now(target):
            time.sleep(0.2)
        if not typed_now(target):
            return


def close_session(sid: str, body: dict):
    meta = session_meta(sid)
    if meta.get("closed"):
        return {"ok": True}
    if meta.get("closing"):
        raise Refused("This session is already closing.")
    target = f"hub-{sid}"
    alive = target in tmux_sessions()
    if meta.get("kind") != "claude":
        tmux("kill-session", "-t", target)
        return {"ok": True}
    if not (body or {}).get("note") or not alive:
        finish_close(sid)
        return {"ok": True}
    pane = pane_of(target)
    if "esc to interrupt" in pane.lower():
        raise Refused("Claude is working. Close it when it stops, or close it now without a handoff.")
    if typed_now(target):
        raise Refused("Send or clear what's typed in this session first.")
    with _close_lock:
        if sid in _closing:
            raise Refused("This session is already closing.")
        _closing.add(sid)
    asked_at = time.time()
    update_meta(sid, lambda m: m.update(closing={"at": asked_at}))
    paste_prompt(target, handoff_prompt(meta.get("folder") or meta.get("repo") or ""), f"hub-close-{sid}")
    threading.Thread(target=_wait_for_handoff, args=(sid, asked_at), daemon=True).start()
    return {"ok": True, "closing": True}


def recover_closing(meta: dict):
    """A close the hub was waiting on when it restarted: finish it, without the note it never got."""
    sid = meta["id"]
    with _close_lock:
        if sid in _closing:
            return
        _closing.add(sid)

    def finish():
        try:
            finish_close(sid)
        finally:
            with _close_lock:
                _closing.discard(sid)
    threading.Thread(target=finish, daemon=True).start()


def resume_session(sid: str):
    meta = session_meta(sid)
    target = f"hub-{sid}"
    if meta.get("kind") != "claude":
        raise Refused("Only Claude sessions can be resumed.")
    if target in tmux_sessions():
        raise Refused("This session is still running.")
    folder = meta.get("folder") or meta.get("repo")
    if not folder or not Path(folder).is_dir():
        raise Refused("Its folder isn't there any more, so it can't be resumed.")
    live = live_transcript(sid)
    claude_id = live.stem if live else meta.get("claude_session")
    if not claude_id or not re.match(r"^[0-9a-f-]{36}$", claude_id):
        raise Refused("There's no conversation to resume.")
    if live is None:  # Claude has cleaned its copy away: put the hub's back where Claude looks for it
        kept, home = RECORDS / sid / "transcript.jsonl", Path(meta.get("transcript", ""))
        if not kept.exists() or (HOME / ".claude" / "projects").resolve() not in home.resolve().parents or home.name != f"{claude_id}.jsonl":
            raise Refused("There's no conversation to resume.")
        home.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(kept, home)
        if (RECORDS / sid / "transcript" / "subagents").is_dir():
            shutil.copytree(RECORDS / sid / "transcript" / "subagents", home.with_suffix("") / "subagents", dirs_exist_ok=True)
    cmd = [CLAUDE, "--resume", claude_id, "--settings", str(HOOKS), "--remote-control", meta["title"]]
    cmd += ["--permission-mode", meta["mode"]] if meta.get("mode") in MODES else []
    cmd += ["--model", meta["model"]] if meta.get("model") in MODELS else []
    cmd += ["--effort", meta["effort"]] if meta.get("effort") in EFFORTS else []
    cmd += ["--agent", meta["agent"]] if meta.get("agent") else []
    result = tmux("new-session", "-d", "-s", target, "-x", "120", "-y", "36", "-c", folder, *session_env(sid), f"{shlex.join(cmd)}; exec /bin/zsh -l")
    if result.returncode != 0:
        raise Refused("The session didn't start: " + result.stderr.strip()[:200])
    update_meta(sid, lambda m: (m.pop("closed", None), m.pop("closing", None), m.update(resumed=time.time())))
    return {"ok": True}


PASTED = re.compile(r"</?pasted_content[^>]*>")


def _text_of(content) -> str:
    """A message's text, with pasted blocks unwrapped: Claude Code stores a paste inside <pasted_content> tags."""
    if isinstance(content, str):
        return PASTED.sub("", content).strip()
    return PASTED.sub("", "\n".join(str(b.get("text", "")) for b in content or [] if isinstance(b, dict) and b.get("type") == "text")).strip()


def session_record(sid: str) -> dict:
    """The conversation as something to read: what you asked, what Claude said, and what it did in between."""
    meta = session_meta(sid)
    transcript = transcript_of(sid)
    items, tools, hiding = [], None, False
    if transcript is not None:
        for number, raw in enumerate(transcript.read_text(errors="ignore").splitlines()):
            try:
                entry = json.loads(raw)
            except ValueError:
                continue
            if entry.get("isMeta") or entry.get("isSidechain") or entry.get("type") not in ("user", "assistant"):
                continue
            at, message = str(entry.get("timestamp", "")), entry.get("message") or {}
            content = message.get("content")
            said = _text_of(content).strip()
            if entry["type"] == "user" and said_closing(said):
                hiding, tools = True, None  # the tidy-up and the handoff are shown as the note
                continue
            if hiding:
                if entry["type"] == "user" and said and not said.startswith("<"):
                    hiding = False  # you wrote again after resuming
                else:
                    continue
            blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content if isinstance(content, list) else []
            for index, block in enumerate(blocks):
                if not isinstance(block, dict):
                    continue
                kind = block.get("type")
                if kind == "text":
                    text = PASTED.sub("", str(block.get("text", ""))).strip()
                    if not text or text.startswith("<") or said_closing(text):
                        continue
                    tools = None
                    items.append({"kind": "you" if entry["type"] == "user" else "claude", "text": text[:20000], "at": at})
                elif kind == "image" and entry["type"] == "user" and (block.get("source") or {}).get("type") == "base64":
                    tools = None
                    items.append({"kind": "image", "blob": f"{number}.{index}", "at": at})
                elif kind == "tool_use":
                    tool, given = str(block.get("name", "")), block.get("input") or {}
                    if tools is None:
                        tools = {"kind": "tools", "steps": [], "count": 0, "errors": 0, "at": at}
                        items.append(tools)
                    tools["count"] += 1
                    if len(tools["steps"]) < 60:
                        tools["steps"].append({"id": block.get("id"), "k": agents.KINDS.get(tool, "m"), "what": agents.phrase(tool, given)})
                elif kind == "tool_result" and block.get("is_error") and tools is not None:
                    tools["errors"] += 1
                    for step in tools["steps"]:
                        if step["id"] == block.get("tool_use_id"):
                            step["k"] = "!"
    for item in items:
        for step in item.get("steps", []):
            step.pop("id", None)
    closed = meta.get("closed") or {}
    return {"title": meta["title"], "note": closed.get("note", ""), "closedAt": closed.get("at"), "created": meta.get("created"),
            "folder": (meta.get("folder") or meta.get("repo") or "").replace(str(HOME), "~", 1), "kept": (RECORDS / sid / "transcript.jsonl").exists(),
            "running": still_running(meta.get("folder") or meta.get("repo") or "") if f"hub-{sid}" not in tmux_sessions() else [],
            "items": items[-1500:], "earlier": max(0, len(items) - 1500)}


def connect_phone(sid: str):
    meta = session_meta(sid)
    target = f"hub-{sid}"
    if target not in tmux_sessions():
        raise Refused("That session has ended.")
    tmux("send-keys", "-t", target, "-l", f"/remote-control {meta['title']}")
    tmux("send-keys", "-t", target, "Enter")
    return {"ok": True}


def live_claude(sid: str):
    meta = session_meta(sid)
    target = f"hub-{sid}"
    if meta.get("kind") != "claude" or target not in tmux_sessions():
        raise Refused("That session has ended.")
    return meta, target


def pane_of(target: str) -> str:
    return tmux("capture-pane", "-p", "-t", target).stdout


def mode_in(pane: str) -> str:
    return next((mode for text, mode in MODE_FOOTER if text in pane), "")


def typed_draft(pane: str) -> str:
    """Text sitting unsent in Claude's input, which a slash command would be added to."""
    for line in reversed(pane.splitlines()):
        row = line.strip()
        if row.startswith("❯"):
            rest = row[1:].strip()
            return "" if rest.startswith('Try "') else rest
    return ""


SGR = re.compile(r"\x1b\[([0-9;]*)m")
OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")


def _ghost(style: dict) -> bool:
    """Text drawn dim or grey: Claude's suggestion of what to type next, which isn't typed until Tab takes it."""
    return style["dim"] or style["fg"] in ("90", "2") or (style["fg"] or "").startswith("38;5;") and 236 <= int(style["fg"][5:] or 0) <= 250


def typed_now(target: str) -> str:
    """What sits unsent in Claude's input right now, read with its colours so a suggestion in grey isn't taken for it."""
    raw = tmux("capture-pane", "-p", "-e", "-t", target).stdout
    for line in reversed(raw.splitlines()):
        line = OSC.sub("", line)
        if not SGR.sub("", line).strip().startswith("❯"):
            continue
        style, seen, text, ghost = {"fg": None, "dim": False}, False, "", None
        parts = SGR.split(line)
        for index, part in enumerate(parts):
            if index % 2:  # an SGR code
                codes = part.split(";") if part else ["0"]
                at = 0
                while at < len(codes):
                    code = codes[at]
                    if code in ("", "0"):
                        style = {"fg": None, "dim": False}
                    elif code == "2":
                        style["dim"] = True
                    elif code == "22":
                        style["dim"] = False
                    elif code == "39":
                        style["fg"] = None
                    elif code == "38" and at + 2 < len(codes) and codes[at + 1] == "5":
                        style["fg"] = f"38;5;{codes[at + 2]}"; at += 2
                    elif code == "38" and at + 4 < len(codes) and codes[at + 1] == "2":
                        at += 4
                        style["fg"] = "rgb"
                    elif code.isdigit() and (30 <= int(code) <= 37 or 90 <= int(code) <= 97):
                        style["fg"] = code
                    at += 1
                continue
            for char in part:
                if not seen:
                    seen = char == "❯"
                    continue
                if ghost is None and not char.isspace():
                    ghost = _ghost(style)
                text += char
        rest = text.replace("\xa0", " ").strip()
        return "" if ghost or rest.startswith('Try "') else rest
    return ""


def set_mode(sid: str, body: dict):
    meta, target = live_claude(sid)
    wanted = body.get("mode")
    if wanted not in MODES:
        raise Refused("That isn't a mode.")
    for _ in range(6):  # Shift+Tab steps through the modes; the footer says where it landed
        if mode_in(pane_of(target)) == wanted:
            meta["mode"] = wanted
            (META / f"{sid}.json").write_text(json.dumps(meta))
            return {"mode": wanted}
        tmux("send-keys", "-t", target, "BTab")
        time.sleep(0.45)
    raise Refused("That mode isn't available in this session.")


def set_model(sid: str, body: dict):
    """Through the /model menu with its "this session only" key: `/model <name>` would change the default for every new session."""
    meta, target = live_claude(sid)
    wanted = body.get("model")
    if wanted not in MODELS:
        raise Refused("That isn't a model.")
    pane = pane_of(target)
    if typed_now(target):
        raise Refused("Send or clear what's typed in this session first.")
    if "esc to interrupt" in pane.lower():
        raise Refused("Claude is working. Change the model when it stops.")
    tmux("send-keys", "-t", target, "-l", "/model")
    time.sleep(0.7)
    tmux("send-keys", "-t", target, "Enter")
    row = re.compile(r"^\s*([❯↓↑]\s*)?\d+\.\s+" + MODELS[wanted] + r"\b")  # in a short terminal the list scrolls and marks its edges
    for _ in range(24):
        time.sleep(0.25)
        lines = pane_of(target).splitlines()
        target_row = next((i for i, line in enumerate(lines) if row.match(line)), None)
        cursor = next((i for i, line in enumerate(lines) if re.match(r"^\s*❯\s*\d+\.", line)), None)
        if target_row is not None and cursor is not None:
            break
    else:
        tmux("send-keys", "-t", target, "Escape")
        raise Refused("The model menu didn't open.")
    for _ in range(abs(target_row - cursor)):
        tmux("send-keys", "-t", target, "Down" if target_row > cursor else "Up")
        time.sleep(0.08)
    tmux("send-keys", "-t", target, "s")
    for _ in range(10):  # mid-conversation, Claude confirms the switch because the cached history has to be re-read
        time.sleep(0.25)
        pane = pane_of(target)
        if "Switch model?" in pane:
            tmux("send-keys", "-t", target, "Enter")
            break
        if "Select model" not in pane:
            break
    meta["model"] = wanted
    (META / f"{sid}.json").write_text(json.dumps(meta))
    return {"model": wanted}


def session_agents(sid: str) -> list:
    session_meta(sid)
    return agents.subagents(sid, transcript_of(sid), f"hub-{sid}" in tmux_sessions())


def project_agents(name: str) -> list:
    return agents.definitions(repos_of(store_of(name)))


def process_tree() -> dict:
    children = {}
    for row in run("ps", "-axo", "pid=,ppid=").stdout.splitlines():
        parts = row.split()
        if len(parts) == 2:
            children.setdefault(int(parts[1]), []).append(int(parts[0]))
    return children


def claude_info(pane: int, children: dict):
    stack = [pane]
    while stack:
        pid = stack.pop()
        path = CLAUDE_SESSIONS / f"{pid}.json"
        if path.exists():
            try:
                return json.loads(path.read_text())
            except ValueError:
                return {"pid": pid}
        stack += children.get(pid, [])
    return None


QUESTION_FENCE = re.compile(r"```.*?```", re.S)
OPTION_LINE = re.compile(r"^(?:[-*\u2022]|\d+[.)]|[A-Za-z][.)])\s")


def question_in(text: str) -> str:
    """The question a finished reply leaves open, or nothing. Options listed after the question don't hide it."""
    lines = [line.strip() for line in QUESTION_FENCE.sub("", text or "").splitlines() if line.strip()]
    while len(lines) > 1 and OPTION_LINE.match(lines[-1]):
        lines.pop()
    if not lines:
        return ""
    sentences = re.split(r"(?<=[.!?])\s+", re.sub(r"[*_`#>]+", "", lines[-1]).strip())
    return next((s for s in reversed(sentences[-2:]) if s.endswith("?")), "")[:200]


def sessions_state() -> list:
    alive = tmux("list-panes", "-a", "-F", "#{session_name} #{pane_pid}")
    panes = dict(row.split() for row in alive.stdout.splitlines() if len(row.split()) == 2)
    children = status.processes()
    commands = status.commands_of(int(pid) for name, pid in panes.items() if name.startswith("hub-") and pid.isdigit())
    out = []
    for path in sorted(META.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            meta = json.loads(path.read_text())
        except ValueError:
            continue
        name = f"hub-{meta['id']}"
        row = {k: meta.get(k) for k in ("id", "project", "repo", "title", "kind", "created", "isolate", "mode", "model", "effort", "agent", "task")}
        row.update(state="ended", phone=None, connected=False, alive=name in panes)
        pane_pid, claude_pid, transcript = int(panes.get(name, 0) or 0), 0, None
        # One look at the screen per session: the mode in its footer, a first-run question, whether Claude says it works.
        screen = pane_of(name) if row["alive"] and meta["kind"] == "claude" else ""
        if meta["kind"] == "claude":
            transcript = transcript_of(meta["id"])
            digest = agents.digest_of(meta["id"], transcript)
            row.update(now=digest["now"], said=digest["said"], trace=digest["trace"][-agents.TRACE_LIMIT:], tally=dict(Counter(digest["trace"])), agents=agents.counts(meta["id"], transcript, row["alive"]), model=digest["model"] or meta.get("model", ""))
            row["mode"] = (mode_in(screen) if row["alive"] else "") or meta.get("mode", "")
        if meta["kind"] == "shell":
            row["state"] = "shell" if row["alive"] else "ended"
        elif row["alive"]:
            info = claude_info(pane_pid, children)
            event = status.read_event(meta["id"])
            # Claude runs under the pane's `zsh -c "claude …; exec zsh -l"`. Once the shell has taken the pane over, Claude is gone.
            starting = info is None and status.waiting_on_claude(commands.get(pane_pid, ""))
            working_on_screen = "esc to interrupt" in screen.lower()
            if info is None and not starting:
                failed = status.error_of(event, False, True)
                if failed:
                    row.update(state="error", error=failed)
            elif info is None:
                # Claude is up but hasn't written its status file: starting, or waiting at a first-run question.
                row["state"] = "needs" if any(mark in screen for mark in FIRST_RUN) else "working" if working_on_screen else "finished"
            else:
                claude_pid = int(info.get("pid") or 0)
                bridge = info.get("bridgeSessionId")
                row.update(connected=bool(bridge), phone=f"https://claude.ai/code/{bridge}" if bridge else None)
                state = event.get("state")
                if state in (None, "ended"):
                    state = "working" if info.get("status") not in ("idle", None) else "finished"
                if state == "error":
                    failed = status.error_of(event, True, True)
                    state = "error" if failed else "finished"
                    if failed:
                        row["error"] = failed
                # Esc stops a turn without the Stop hook, so a "working" or "needs" the hooks left behind is checked
                # against Claude's own status: idle since after the last event, and nothing on screen still running or asking.
                settled = info.get("status") == "idle" and (info.get("statusUpdatedAt") or 0) / 1000 > float(event.get("at") or 0) + 1
                if state in ("working", "needs") and settled and not working_on_screen and not status.dialog_in(screen) and not any(mark in screen for mark in FIRST_RUN):
                    state = "finished"
                row["state"] = state
                if state == "needs":
                    row["asking"] = str(event.get("message", ""))[:200]
                elif state == "finished":
                    asked = question_in(digest["last"])
                    if asked:
                        row.update(state="needs", asking=asked)
                if state != "working" and "trust this folder" in screen:
                    row["state"] = "needs"
        if meta["kind"] == "claude":
            closed = meta.get("closed")
            if meta.get("closing"):
                if row["state"] != "needs":  # a permission prompt during the tidy-up still needs you
                    row["state"] = "closing"
                with _close_lock:
                    orphaned = meta["id"] not in _closing
                if orphaned:
                    recover_closing(meta)
            elif closed and not row["alive"]:
                row.update(state="closed", closedAt=closed.get("at"), note=closed.get("note", ""))
            if not row["alive"]:
                live = live_transcript(meta["id"])
                kept = RECORDS / meta["id"] / "transcript.jsonl"
                if live is not None and (not kept.exists() or live.stat().st_mtime > kept.stat().st_mtime + 1):
                    try:
                        snapshot(meta["id"])  # a session that ended on its own is kept too
                    except OSError as error:
                        print(f"error: snapshot {meta['id']}: {error}", flush=True)
                folder = meta.get("folder") or meta.get("repo")
                row["resumable"] = bool(folder and Path(folder).is_dir() and (live is not None or kept.exists()))
        if row["state"] != "error":
            row.pop("error", None)
        status.enrich(row, meta, transcript, pane_pid, claude_pid, children)
        out.append(row)
    return out


# ── project identity: display name and icon. The store and its folders keep their names, so nothing else breaks. ──
ICON_TYPES = {b"\x89PNG\r\n\x1a\n": "png", b"\xff\xd8\xff": "jpg", b"RIFF": "webp"}


def profile_of(name: str) -> dict:
    path = PROFILES / f"{name}.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        data = {}
    return {"display": data.get("display") or name, "icon": data.get("icon")}


def set_profile(name: str, body: dict):
    store_of(name)
    display = " ".join(str(body.get("display", "")).split())[:40] or name
    icon = body.get("icon")
    if icon is not None:
        kind, value, color = icon.get("kind"), str(icon.get("value", "")), str(icon.get("color", "#7c7aff"))
        if kind == "glyph" and not re.match(r"^[a-z-]{1,24}$", value):
            raise Refused("That isn't an icon.")
        if kind == "emoji" and not 0 < len(value) <= 8:
            raise Refused("Use a single emoji.")
        if kind not in ("glyph", "emoji", "image", "letter") or not re.match(r"^#[0-9a-fA-F]{6}$", color):
            raise Refused("That isn't an icon.")
        icon = {"kind": kind, "value": value, "color": color}
    (PROFILES / f"{name}.json").write_text(json.dumps({"display": display, "icon": icon}))
    return {"ok": True}


def icon_file(name: str):
    store_of(name)
    for ext in ("png", "jpg", "webp"):
        path = PROFILES / f"{name}.{ext}"
        if path.exists():
            return path
    raise Refused("This project has no image.")


def save_icon(name: str, data: bytes):
    store_of(name)
    if len(data) > 2_000_000:
        raise Refused("Use an image under 2 MB.")
    ext = next((e for magic, e in ICON_TYPES.items() if data.startswith(magic)), None)
    if ext is None or (ext == "webp" and data[8:12] != b"WEBP"):
        raise Refused("Use a PNG, JPEG or WebP image.")
    for old in ("png", "jpg", "webp"):
        (PROFILES / f"{name}.{old}").unlink(missing_ok=True)
    (PROFILES / f"{name}.{ext}").write_bytes(data)
    return {"ok": True}


# ── homepage ──
def repo_overview(repo: str) -> dict:
    branch = run("git", "-C", repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    dirty = len(run("git", "-C", repo, "status", "--porcelain").stdout.splitlines())
    commits = []
    for row in run("git", "-C", repo, "log", "-8", "--format=%h%x1f%s%x1f%ct").stdout.splitlines():
        parts = row.split("\x1f")
        if len(parts) == 3:
            commits.append({"sha": parts[0], "text": parts[1], "at": int(parts[2])})
    return {"path": repo, "name": Path(repo).name, "branch": "detached" if branch == "HEAD" else branch, "dirty": dirty, "commits": commits}


def pulse_of(repos: list, store: Path) -> dict:
    """Commits and memory writes per day for the last two weeks."""
    days = {time.strftime("%Y-%m-%d", time.localtime(time.time() - n * 86400)): 0 for n in range(13, -1, -1)}
    for repo in repos:
        for stamp in run("git", "-C", repo, "log", "--since=14.days", "--format=%ct").stdout.split():
            day = time.strftime("%Y-%m-%d", time.localtime(int(stamp)))
            if day in days:
                days[day] += 1
    log = store / "CHANGELOG.md"
    for line in (log.read_text(errors="ignore").splitlines() if log.exists() else []):
        match = re.match(r"^\| (\d{4}-\d{2}-\d{2})T", line)
        if match and match[1] in days:
            days[match[1]] += 1
    return days


def home_of(name: str) -> dict:
    store = store_of(name)
    repos = [repo_overview(r) for r in repos_of(store)]
    activity = [{"kind": "commit", "text": c["text"], "who": r["name"], "at": c["at"]} for r in repos for c in r["commits"]]
    for row in kit.log_rows(store):
        if re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$", row["when"]):
            activity.append({"kind": "memory", "text": row["why"], "was": row["was"], "now": row["now"], "who": row["agent"], "at": time.mktime(time.strptime(row["when"], "%Y-%m-%dT%H:%M:%S"))})
    activity.sort(key=lambda a: a["at"], reverse=True)
    # Only the numbers: the Dreams page asks the kit who is dreaming and what its check finds, and a homepage that is
    # redrawn every few seconds for every project must not.
    return {"repos": repos, "activity": activity[:14], "metrics": dream_metrics(store), "dreams": len(kit.record_of(store)["proposals"]),
            "files": len(memory_files(name)), "store": str(store), "agents": project_agents(name),
            "pulse": [{"day": d, "n": n} for d, n in pulse_of(repos_of(store), store).items()]}


def platform() -> dict:
    activity, metrics, days = [], {}, {}
    for store in stores():
        data = home_of(store.name)
        for point in data["pulse"]:
            days[point["day"]] = days.get(point["day"], 0) + point["n"]
        activity += [{**a, "project": store.name} for a in data["activity"]]
        if data["metrics"]:
            metrics[store.name] = data["metrics"][-1]
    activity.sort(key=lambda a: a["at"], reverse=True)
    return {"activity": activity[:12], "metrics": metrics, "pulse": [{"day": d, "n": days[d]} for d in sorted(days)]}


# ── what was shared in a session: links, images, audio and files ──
IMAGE = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif", "webp": "image/webp", "avif": "image/avif", "svg": "image/svg+xml"}
AUDIO = {"mp3": "audio/mpeg", "m4a": "audio/mp4", "wav": "audio/wav", "ogg": "audio/ogg", "flac": "audio/flac", "aac": "audio/aac"}
VIDEO = {"mp4": "video/mp4", "mov": "video/quicktime", "webm": "video/webm"}
DOCUMENT = {"pdf": "application/pdf"}
TEXT = {"md", "txt", "json", "csv", "log", "py", "js", "ts", "tsx", "swift", "sh", "html", "css", "yml", "yaml", "toml"}
MEDIA = {**IMAGE, **AUDIO, **VIDEO, **DOCUMENT}
URL_TEXT = re.compile(r"https?://[^\s<>\"'`)\]}]+")
PATH_TEXT = re.compile(r"(?:~/|/|\.{1,2}/|[\w.-]+/)[\w./@%+-]*\.(?:" + "|".join(sorted({*MEDIA, *TEXT}, key=len, reverse=True)) + r")\b", re.I)
EXT_IMAGE = re.compile(r"\.(png|jpe?g|gif|webp|avif|svg)$", re.I)
BLOB_REF = re.compile(r"^(?:([a-z0-9]{6,40}):)?(\d+)\.(\d+)(?:\.(\d+))?$")
# Folders whose files are never served or diffed, wherever they are; the config's "private_folders" adds names.
NEVER_SERVED = {".git", ".ssh", ".gnupg", ".aws", ".config", "Keychains", *listed("private_folders")}
_shared_cache = {}


def kind_of(path: str) -> str:
    ext = path.rsplit(".", 1)[-1].lower()
    if ext in IMAGE:
        return "image"
    if ext in AUDIO:
        return "audio"
    if ext in VIDEO:
        return "video"
    if ext in DOCUMENT:
        return "pdf"
    return "text" if ext in TEXT else ""


def allowed_roots(meta: dict) -> list:
    store = STORES / meta["project"]
    roots = [Path(r) for r in repos_of(store)] + [HOME / "Downloads", HOME / "Desktop", UPLOADS / meta["id"], Path(f"/private/tmp/claude-{os.getuid()}")]
    if meta.get("folder"):
        roots.append(Path(meta["folder"]))
    return [r.resolve() for r in roots if r.exists()]


def resolve_shared(sid: str, raw: str) -> Path:
    """Only files a session could reasonably have shared: its project, its uploads, scratch space, Downloads and Desktop."""
    meta = session_meta(sid)
    text = str(raw or "")
    if text.startswith("file://"):
        text = text[7:]
    base = Path(meta.get("folder") or meta["repo"])
    path = (Path(text).expanduser() if text.startswith(("/", "~")) else base / text).resolve()
    if not kind_of(path.name) or NEVER_SERVED & set(path.parts) or path.name.startswith(".env") or path.suffix.lower() in (".pem", ".key"):
        raise Refused("That file can't be opened here.")
    if not any(root == path or root in path.parents for root in allowed_roots(meta)):
        raise Refused("That file is outside this session's folders.")
    if not path.is_file():
        raise Refused("That file isn't there any more.")
    return path


def mime_of(path: Path) -> str:
    return MEDIA.get(path.suffix.lower().lstrip("."), "application/octet-stream")


def preview(sid: str, raw: str) -> dict:
    path = resolve_shared(sid, raw)
    if kind_of(path.name) != "text":
        raise Refused("That file has no text to show.")
    with open(path, errors="replace") as f:
        return {"name": path.name, "path": str(path), "text": f.read(200_000)}


def live_transcript(sid: str):
    event = EVENTS / f"{sid}.json"
    try:
        value = json.loads(event.read_text()).get("transcript", "")
    except (OSError, ValueError):
        return None
    path = Path(value).resolve() if value else None
    return path if path and (HOME / ".claude" / "projects").resolve() in path.parents and path.exists() else None


def transcript_of(sid: str):
    """Claude's own transcript while it exists, then the hub's copy. The copy keeps the same layout, subagents beside it."""
    live = live_transcript(sid)
    if live is not None:
        return live
    kept = RECORDS / sid / "transcript.jsonl"
    return kept if kept.exists() else None


def shared_items(sid: str) -> list:
    session_meta(sid)
    transcript = transcript_of(sid)
    if transcript is None:
        return []
    subagent_files = sorted((transcript.with_suffix("") / "subagents").glob("agent-*.jsonl"))
    stamp = (transcript.stat().st_mtime, tuple(p.stat().st_mtime for p in subagent_files))
    cached = _shared_cache.get(sid)
    if cached and cached[0] == stamp:
        return cached[1]
    found, seen = [], set()

    def add(key, item, at):
        if key not in seen:
            seen.add(key)
            found.append({**item, "at": at})

    def add_path(text, at, by):
        try:
            path = resolve_shared(sid, text)
        except Refused:
            return False
        add(str(path), {"kind": kind_of(path.name), "path": str(path), "name": path.name, "by": by}, at)
        return True

    def scan(path: Path, by: str):
        prefix = f"{by}:" if by else ""
        shown_by_path = set()  # a Read of an image also returns its pixels; the file is the better thing to show
        for number, raw in enumerate(path.read_text(errors="ignore").splitlines()):
            try:
                entry = json.loads(raw)
            except ValueError:
                continue
            # Only what was said in the chat: session context and tool announcements are logged as other entry types.
            if entry.get("isMeta") or (entry.get("isSidechain") and not by) or entry.get("type") not in ("user", "assistant"):
                continue
            at = str(entry.get("timestamp", ""))
            content = (entry.get("message") or {}).get("content")
            role = (entry.get("message") or {}).get("role")
            blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content if isinstance(content, list) else []
            for index, block in enumerate(blocks):
                if not isinstance(block, dict):
                    continue
                kind = block.get("type")
                if kind == "text" and not str(block.get("text", "")).lstrip().startswith("<") and not (by and role == "user"):
                    text = str(block.get("text", ""))
                    for url in URL_TEXT.findall(text):
                        url = url.rstrip(".,;:!?*_")
                        if not url.startswith("https://claude.ai/code/session_"):
                            add(url, {"kind": "link", "url": url, "name": url.split("//", 1)[1], "by": by}, at)
                    for match in PATH_TEXT.findall(URL_TEXT.sub(" ", text)):
                        add_path(match, at, by)
                elif kind == "image" and role == "user" and (block.get("source") or {}).get("type") == "base64":
                    add(f"{prefix}{number}:{index}", {"kind": "image", "blob": f"{prefix}{number}.{index}", "name": "Pasted image", "by": by}, at)
                elif kind == "tool_use" and block.get("name") in ("Write", "Read"):
                    file_path = str((block.get("input") or {}).get("file_path", ""))
                    if block["name"] == "Write" or EXT_IMAGE.search(file_path):
                        if add_path(file_path, at, by) and block["name"] == "Read":
                            shown_by_path.add(block.get("id"))
                elif kind == "tool_result" and block.get("tool_use_id") not in shown_by_path and isinstance(block.get("content"), list):
                    for inner, part in enumerate(block["content"]):
                        if isinstance(part, dict) and part.get("type") == "image" and (part.get("source") or {}).get("type") == "base64":
                            add(f"{prefix}{number}:{index}:{inner}", {"kind": "image", "blob": f"{prefix}{number}.{index}.{inner}", "name": "Screenshot", "by": by}, at)

    scan(transcript, "")
    for path in subagent_files:
        scan(path, path.stem[len("agent-"):])
    found.sort(key=lambda item: item["at"], reverse=True)
    _shared_cache[sid] = (stamp, found[:150])
    return found[:150]


def blob_of(sid: str, ref: str):
    session_meta(sid)
    transcript = transcript_of(sid)
    match = BLOB_REF.match(ref or "")
    if transcript is None or match is None:
        raise Refused("That image isn't available.")
    by, number, index, inner = match.groups()
    path = transcript.with_suffix("") / "subagents" / f"agent-{by}.jsonl" if by else transcript
    try:
        block = json.loads(path.read_text(errors="ignore").splitlines()[int(number)])["message"]["content"][int(index)]
        source = (block["content"][int(inner)] if inner is not None else block)["source"]
        mime = source["media_type"]
        if mime not in IMAGE.values() or mime == "image/svg+xml":
            raise KeyError
        return base64.b64decode(source["data"]), mime
    except (OSError, IndexError, KeyError, TypeError, ValueError):
        raise Refused("That image isn't available.")


def save_upload(sid: str, name: str, data: bytes) -> dict:
    session_meta(sid)
    clean = re.sub(r"[^\w.-]+", "-", Path(str(name)).name).strip("-.") or "file"
    if not kind_of(clean):
        raise Refused("Share images, audio, video, PDFs or text files.")
    folder = UPLOADS / sid
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{secrets.token_hex(2)}-{clean}"
    path.write_bytes(data)
    return {"path": str(path), "name": clean}


def state() -> dict:
    live = tmux_sessions()
    projects = []
    for store in stores():
        name = store.name
        repos = repos_of(store)
        known = kit.record_of(store)
        latest = proposal_summary(store, known["proposals"][-1]) if known["proposals"] else None
        projects.append({"name": name, **profile_of(name), "repos": repos, "schedule": kit.schedule_of(name), "check": kit.dream_check_of(name), "dreaming": dream_running(name, live),
                         "batch": len(list((store / "dreams" / "_batch").glob("[!_]*.md"))) if (store / "dreams" / "_batch").exists() else 0,
                         "latest": latest, "setup": setup_of(name, store, repos), "lessons": sum(entries(p) for p in (store / "project").glob("*.md")),
                         "waits": {who: sum(1 for entry in known["left"] if entry["waiting"] == who) for who in ("you", "a session")}, "needs": len(needs_of(store)["open"])})
    return {"projects": projects, "sessions": sessions_state(), "at": time.time(), "version": version()}

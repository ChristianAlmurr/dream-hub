"""Everything Dream Hub asks of the agent memory kit, and nothing else: the kit's scripts it runs and the kit's files
it reads. The rest of the hub goes through here and never touches the kit itself.

The kit says who is dreaming, which proposal is open, what was left unticked and whether Claude Code keeps notes of
its own, each through one of its scripts. The hub runs that script and shows the answer, so the two can't disagree.
Two things the kit has no read-only command for are read from its files here: the dream job (a launchd plist) and
the state file of the daily check, whose refusal rule mirrors the kit's and is compared with it in the tests.
When the kit gets one read-only status command, the readers in this file are what it replaces.
"""
import hashlib, json, math, plistlib, re, shlex, subprocess, time
from pathlib import Path
from xml.parsers.expat import ExpatError

from hub_base import ENV, HOME, JOBS, KIT, LAUNCHD_JOBS, Refused, damaged, pasteable, run, stamp_of, whole

KIT_STATE = HOME / ".local" / "state" / "agent-memory"  # the kit's own records, one <store>.json per store
SCHEDULES = JOBS.resolve() == LAUNCHD_JOBS  # the kit schedules through launchd and nowhere else, so only a hub that reads launchd's folder may ask it to
# What marks a command as part of a dream, or moves the kit's clock. The hub is never a dream, whatever started it.
NOT_THE_HUBS = ("CLAUDE_CODE_SESSION_ID", "AGENT_MEMORY_DREAM_RUN", "AGENT_MEMORY_DREAM_REASON", "AGENT_MEMORY_PROPOSE_ONLY", "AGENT_MEMORY_NOW")
HELD = 4  # dream_owner.py's exit code when a dream is alive on the store
OPEN_LINE = re.compile(r"^.*/dreams/(?P<name>\d{4}-\d{2}-\d{2}(-\d+)?-proposal\.md) \((?P<age>of .+)\)$")
PROPOSAL_NAME = re.compile(r"\d{4}-\d{2}-\d{2}(-\d+)?-proposal\.md")
_kept = {}


TIMEOUT = 60  # seconds a kit script gets. Its writers wait for the store's lock, so one that takes longer is waiting for another writer


class Silent(Refused):
    """A kit script didn't answer in time. For an action this is the answer the person gets; a read turns it into
    the problem of that one project, so the others are still shown."""


def kit_env(store: Path, extra=None) -> dict:
    """What a kit script is run with. Never the marks of a dream, whatever started the hub: the hub is not a dream. And
    never git's optional locks, so a read changes nothing in the store."""
    environment = {key: value for key, value in ENV.items() if key not in NOT_THE_HUBS}
    environment.update({"GIT_OPTIONAL_LOCKS": "0", **(extra or {}), "AGENT_MEMORY_DIR": str(store)})
    return environment


def run_kit(store: Path, command: list, env=None, feed=None):
    """Run a command of the kit's for a store, and stop waiting after TIMEOUT."""
    try:
        return subprocess.run(command, env=kit_env(store, env), input=feed, capture_output=True, text=True, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        raise Silent(f"The kit didn't answer within {'a minute' if TIMEOUT == 60 else f'{TIMEOUT} seconds'}. Another writer may hold this memory. Nothing was changed.")


def ask(store: Path, script: str, *args, env=None):
    """Run one of the kit's scripts for a store."""
    return run_kit(store, ["python3", str(KIT / "scripts" / script), *args], env)


def said(result) -> str:
    """What a kit script said when it stopped: its last word to the person, on one line."""
    return " ".join((result.stderr.strip() or result.stdout.strip() or f"it exited with {result.returncode} and said nothing").split())


def remembered(key: str, rests_on, work):
    """An answer kept until a file it rests on changes, and for a minute at most, so a poll doesn't start Python for every project."""
    signature = (int(time.time() // 60), rests_on())
    if key in _kept and _kept[key][0] == signature:
        return _kept[key][1]
    answer = work()
    _kept[key] = ((int(time.time() // 60), rests_on()), answer)
    return answer


# ── a new project, and its dream job ──
def install(name: str, repos: list):
    """Make the store and put the kit into its folders. A hub that doesn't read launchd's folder leaves the dream job out."""
    extra = {} if SCHEDULES else {"AGENT_KIT_NO_SCHEDULE": "1"}
    return subprocess.run(["sh", str(KIT / "install-store.sh"), name, *map(str, repos)], env={**ENV, **extra}, capture_output=True, text=True, timeout=120)


def untouched_principles() -> str:
    return (KIT / "memory-template" / "ORG.md").read_text()


def retire_command(name: str) -> str:
    return f"sh {pasteable(KIT / 'retire-auto-memory.sh')} {shlex.quote(name)}"


def dream_command(repo: str) -> str:
    """One route for every dream, so a failure always alerts."""
    return shlex.join(["/bin/sh", str(KIT / "run-dream.sh"), repo])


def schedule_of(name: str):
    """The store's launchd job. The kit's job checks every day at one hour and dreams only when a dream is due, so it
    has no weekday. A job from before that runs on one weekday, and reads that way until the store is rescheduled."""
    plist = JOBS / f"ai.agent-memory.dream.{name}.plist"
    if not plist.exists():
        return None
    try:
        with open(plist, "rb") as f:
            data = plistlib.load(f)
    except (OSError, ValueError, ExpatError) as error:
        return damaged(plist, error)
    when = data.get("StartCalendarInterval") if isinstance(data, dict) else None
    if not isinstance(when, dict) or not whole(when.get("Hour")) or not 0 <= when["Hour"] <= 23:
        return damaged(plist, "StartCalendarInterval isn't one time of day with an Hour from 0 to 23")
    if "Weekday" in when and not whole(when["Weekday"]):
        return damaged(plist, "its Weekday isn't a number")
    if not isinstance(data.get("WorkingDirectory"), str):
        return damaged(plist, "it has no WorkingDirectory")
    return {"hour": when["Hour"], "weekday": when.get("Weekday"), "cwd": data["WorkingDirectory"]}


def set_schedule(repo: str, on: bool, hour: int):
    if not SCHEDULES:
        raise Refused(f"This hub reads dream jobs from {JOBS}, not from launchd, so it can't switch them on or off. Use the installed hub for that.")
    result = run("sh", KIT / "schedule-dream.sh", repo, *(["--hour", str(hour)] if on else ["--remove"]))
    if result.returncode != 0:
        raise Refused(said(result)[:300])


def job_running(name: str) -> bool:
    """Whether launchd is running the store's dream job right now. Only a hub that reads launchd's folder asks launchd."""
    if not SCHEDULES:
        return False
    return bool(re.search(r'"PID"\s*=\s*(\d+)', run("launchctl", "list", f"ai.agent-memory.dream.{name}").stdout))


# ── the daily check's state file ──
TRY_ENDINGS = ("proposal", "nothing", "failed", None)


def finite(value) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def dream_check_of(name: str):
    """What the kit last decided about dreaming for this store, from its state file: why the last dream started, what
    the last daily check found, and when it last reminded the person of an open proposal. Nothing until the kit has
    written the file. Any key may be missing or null: after a dream finishes the last check is void, so `checked`
    and `verdict` are null until the next one. `waiting_said` is when the person was last reminded of approved
    changes that nobody has done yet.

    The file is the kit's (scripts/dream_due.py). One it would refuse is refused here by the same rule, load() and
    trusted() there, and the person gets a line saying so in place of the verdict. The kit has no command that only
    reads the file (dream_try.py ready also takes the writers' lock and tries a write), so the rule is mirrored and
    tests/test_hub.py compares the two, case by case.
    """
    path = KIT_STATE / f"{name}.json"
    if not path.exists():
        return None
    why = ""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        data, why = None, str(error)
    if not why and not isinstance(data, dict):
        why = "it isn't a JSON object"
    if not why:
        tries, reminded = data.get("tries"), data.get("reminded")
        if not all(data.get(key) is None or finite(data[key]) for key in ("last_dream", "checked")):
            why = "last_dream and checked must be numbers"
        elif not all(data.get(key) is None or isinstance(data[key], str) for key in ("last_reason", "verdict")):
            why = "last_reason and verdict must be text"
        elif not (tries is None or (isinstance(tries, list) and all(
                isinstance(t, dict) and finite(t.get("at")) and "ended" in t and t["ended"] in TRY_ENDINGS and (t.get("alerted") is None or finite(t["alerted"])) for t in tries))):
            why = "tries isn't a list of dreams tried, each with a time and how it ended"
        elif not (reminded is None or (isinstance(reminded, dict) and isinstance(reminded.get("proposal"), str) and finite(reminded.get("at")))):
            why = "reminded doesn't name a proposal and a time"
        elif not (data.get("waiting_said") is None or finite(data["waiting_said"])):
            why = "waiting_said isn't a number"
        elif any(when is not None and when > time.time() for when in [data.get("last_dream"), (reminded or {}).get("at"), data.get("waiting_said")] + [t["at"] for t in tries or []]):
            why = "it records a time later than now"
    if why:
        damaged(path, why)
        return {"problem": f"The kit can't read its record of this project's dream checks, so they aren't shown. Correct {str(path).replace(str(HOME), '~', 1)}, or move it away."}
    return {"verdict": data.get("verdict") or "", "checked": data.get("checked"), "reason": data.get("last_reason") or "", "dreamed": data.get("last_dream"),
            "reminded": data.get("reminded"), "waiting_said": data.get("waiting_said")}


ARCHIVE_KEEP_DAYS = 180  # the kit's archive.KEEP_DAYS: a stored session that ended longer ago than this is removed


def archive_copy(name: str, claude_id: str) -> str:
    """Where the kit's searchable archive keeps a session (scripts/archive.py), as ~/…, or "" when it has none.
    Read only: the archive is the kit's, and deleting a session in the hub never touches it."""
    if not re.match(r"^[0-9a-f-]{36}$", claude_id or "") or not re.match(r"^[a-z0-9][a-z0-9-]{0,40}$", name or ""):
        return ""
    path = KIT_STATE / "sessions" / name / f"{claude_id}.md"
    return str(path).replace(str(HOME), "~", 1) if path.is_file() else ""


def dreamed_until(name: str):
    """Up to when the store's last dream read its sessions (the kit's last_dream), or None when the kit has no
    readable record of one. A session with anything after this moment hasn't been read by a dream."""
    check = dream_check_of(name)
    return None if not check or check.get("problem") else check.get("dreamed")


# ── proposals: which there are, which is open, and what was left unticked ──
def record_of(store: Path) -> dict:
    """The store's proposals as the kit knows them, oldest first, and the items closed proposals left unapplied:
    {"proposals": [{"name", "written", "state", "closed"}], "left": [...], "problem": ""}.

    The kit's scripts/dream_proposal.py is the one place that says what state a proposal is in. Nothing here sorts
    file names or guesses from the store's history: a store with no record yet is answered by the kit too. When
    the kit can't read the record, `problem` is its sentence and the lists are empty.
    """
    script = KIT / "scripts" / "dream_proposal.py"
    rests_on = lambda: tuple(stamp_of(path) for path in (script, store / "dreams" / "proposals.json", store / "dreams", store / ".git" / "logs" / "HEAD"))

    def work():
        if not script.exists():
            return {"proposals": [], "left": [], "problem": f"The kit at {KIT} has no scripts/dream_proposal.py, so there is no telling which proposals this project has."}
        try:
            result = ask(store, "dream_proposal.py", "--json")
        except Silent as quiet:
            return {"proposals": [], "left": [], "problem": str(quiet)}
        try:
            found = json.loads(result.stdout) if result.returncode == 0 else None
        except ValueError:
            found = None
        if not (isinstance(found, dict) and isinstance(found.get("proposals"), list) and isinstance(found.get("left"), list)):
            return {"proposals": [], "left": [], "problem": said(result)}
        return {"proposals": found["proposals"], "left": found["left"], "problem": ""}

    return remembered(f"record:{store}", rests_on, work)


WAITING_ITEM = re.compile(r"^  (?P<item>\S.*)$")
WAITING_WAY = re.compile(r"^      approved in (?P<proposal>\S+-proposal\.md), \d+ days? ago; (?P<way>.+)$")
DEAD_END = "nothing can do it any more: "
DEAD_END_TAIL = ". Clear it with withdraw, below"  # the kit's pointer to its own command, where the hub has a button


def said_bare(store: Path) -> dict:
    """What `dream_proposal.py` says with no argument: {"open": <why no dream can start, or empty>, "ways": {(item, proposal): <what gets it done>}}.

    Its first line names the open proposal and its age, or says there is none: that line alone answers whether a
    dream can start. After it the kit lists each item the person approved that nobody has done yet, with the way
    to do it or, when its proposal or record is gone, "nothing can do it any more: …". The hub shows those
    sentences as they are, so an entry that has become a dead end reads the same here as in the kit.
    """
    try:
        result = ask(store, "dream_proposal.py")
    except Silent as quiet:
        return {"open": str(quiet), "ways": {}}
    if result.returncode != 0:
        return {"open": said(result), "ways": {}}
    lines = result.stdout.splitlines()
    found = OPEN_LINE.match(lines[0].strip()) if lines else None
    ways, item = {}, None
    for line in lines[1:]:
        if line.startswith("Needs you"):  # what needs the person follows the approved items, in lines of the same shape; the hub reads it from needs.json
            break
        way = WAITING_WAY.match(line)
        if way and item is not None:
            ways[(item, way["proposal"])] = way["way"]
            item = None
        elif WAITING_ITEM.match(line):
            item = WAITING_ITEM.match(line)["item"]
    return {"open": f"The proposal {found['name']} ({found['age']}) has not been applied or dismissed. Apply it or dismiss it, and then dream again." if found else "", "ways": ways}


def open_proposal(store: Path) -> str:
    """Why no dream can start on this store because of a proposal, in the kit's own terms, or empty."""
    return said_bare(store)["open"]


def withdraw(store: Path, words: str) -> str:
    """The person takes back one approved item that nobody has done yet. Returns what the kit said."""
    result = ask(store, "dream_proposal.py", "withdraw", words)
    if result.returncode != 0:
        raise Refused(said(result))
    return result.stdout.strip()


CARRY = "carry"  # the action of an item that moves the checked facts of one of Claude's own notes into memory
FACT = re.compile(r'^([+-]) (.+?\.md) "(.*)"(?: · machine: (\d+))?$')  # under a carry item, after the indent: carried or left out, the file, the line


def item_id(text: str, details: list) -> str:
    """Which item of a proposal this is, as the kit names it (dream_proposal.item_id, and `key` in an apply record): its
    tick line after the box and the indented lines under it, exactly as written. A number is only a label, and
    an item that is rewritten is another item. The kit has no command that prints these, so the rule is mirrored
    and tests/test_hub.py compares the two. A carry item's facts count as "+" whatever their sign: turning one into a
    "-" leaves that fact out and keeps the item the dream wrote."""
    if f" — {CARRY} — " in text:
        details = [("+" + row[1:]) if FACT.match(row) else row for row in details]
    return hashlib.sha256("\n".join([text] + details).encode()).hexdigest()[:12]


def declined(store: Path) -> list:
    """The items the person has declined and that are still held against a proposal, as the kit lists them
    (`dream_proposal.py declined`): passed over often enough, and not so long ago that the kit has let them go.
    Both numbers are the kit's, and the hub knows neither."""
    result = ask(store, "dream_proposal.py", "declined")
    if result.returncode != 0:
        raise Refused(said(result))
    return [line[2:] for line in result.stdout.splitlines() if line.startswith("- ")]


def dismiss(store: Path, name: str) -> str:
    """The person closes the open proposal without applying it. Returns what the kit said."""
    result = ask(store, "dream_proposal.py", "dismiss", name)
    if result.returncode != 0:
        raise Refused(said(result))
    return result.stdout.strip()


def done(store: Path, words: str) -> str:
    """The person closes one thing that needs them. Returns what the kit said."""
    result = ask(store, "dream_proposal.py", "done", words)
    if result.returncode != 0:
        raise Refused(said(result))
    return result.stdout.strip()


def forget(store: Path, words: str) -> str:
    """The person takes one item off the declined list. Returns what the kit said."""
    result = ask(store, "dream_proposal.py", "forget", words)
    if result.returncode != 0:
        raise Refused(said(result))
    return result.stdout.strip()


# ── applying: what became of each ticked item ──
APPLY_RESULTS = ("applied", "already applied", "refused", "yours", "needs a session")
APPLY_ROW = {"key": str, "item": str, "line": int, "section": str, "target": str, "action": str, "kind": str, "result": str, "reason": str, "commits": list, "at": str}


def apply_record(store: Path, name: str):
    """What the kit's scripts/dream_apply.py did with each ticked item of a proposal, from the record it writes beside
    the proposal (<stem>-applied.json): {"rows": [...], "problem": ""}. None when nothing was applied yet.

    A row and an item are joined by `key`, the item's id (item_id above): an item rewritten after a first apply is
    another item. `line` is where it sat in the proposal, counted from 0. `result` is applied, already applied, refused, yours (ORG.md and Steering, which only the person
    applies) or needs a session. A record that isn't in that shape gets a plain line, and no fate is guessed.
    """
    path = store / "dreams" / (name[:-len("-proposal.md")] + "-applied.json")
    if not path.exists():
        return None
    try:
        rows = json.loads(path.read_text())["items"]
        good = isinstance(rows, list) and all(isinstance(row, dict) and all(type(row.get(key)) is kind for key, kind in APPLY_ROW.items())
                                              and row["result"] in APPLY_RESULTS and all(isinstance(c, str) for c in row["commits"]) for row in rows)
        why = "" if good else "its rows aren't what dream_apply.py writes"
    except (OSError, ValueError, KeyError, TypeError) as error:
        rows, why = [], str(error)
    if why:
        damaged(path, why)
        return {"rows": [], "problem": f"The kit's record of what was applied can't be read, so what became of each item isn't shown. Look at {str(path).replace(str(HOME), '~', 1)}."}
    return {"rows": rows, "problem": ""}


def yours_commands(store: Path, repo: str) -> dict:
    """The person's pass over the ticked ORG.md and Steering items, as two commands to run in a terminal: lift the lock
    on ORG.md, which is how the store knows a person is there, then the kit's own pass, which shows each change, asks
    for a typed yes and locks the file again. The hub shows these and never runs the first."""
    return {"lift": f"chflags nouchg {pasteable(store / 'ORG.md')}", "run": f"cd {pasteable(Path(repo))} && {YOURS_PASS}"}


SESSION_PASS = "/dream apply"  # in a session: applies an open proposal, and does what earlier proposals left waiting for a session


YOURS_PASS = "python3 scripts/dream_apply.py --yours"  # run in a folder of the project, in a real terminal


# ── the change log ──
LOG_ROWS = """import json, sys
sys.path.insert(0, sys.argv[1])
import memwrite
rows = []
for line in (memwrite.LOG.read_text(errors="ignore").splitlines() if memwrite.LOG.exists() else []):
    cells = [cell.strip() for cell in line.strip("|").split("|")]
    if not (line.startswith("| ") and len(cells) == 6):
        continue
    try:
        found = memwrite.change(cells[4])
    except Exception:  # a cell from before the kit wrote changes this way: all of it is the reason
        found = {"why": cells[4], "was": None, "now": None, "after": None}
    rows.append({"when": cells[0], "file": cells[1], "agent": cells[2], "session": cells[3], **found})
print(json.dumps(rows))
"""


def log_rows(store: Path) -> list:
    """Every row of the store's CHANGELOG.md, oldest first: {"when", "file", "agent", "session", "why", "was", "now", "after"}.

    The kit packs an edit's old and new line, or a removed line, into the row's reason; its own memwrite.change()
    takes that apart, so nothing here splits the text. `was` and `now` are the line before and after (`now` is
    None for a removal, `was` for an addition); `after` is the line a removed entry stood under.
    """
    def work():
        try:
            result = run_kit(store, ["python3", "-c", LOG_ROWS, str(KIT / "scripts")])
        except Silent as quiet:
            return damaged(store / "CHANGELOG.md", quiet) or []
        try:
            return json.loads(result.stdout) if result.returncode == 0 else damaged(store / "CHANGELOG.md", said(result)[:300]) or []
        except ValueError as error:
            return damaged(store / "CHANGELOG.md", error) or []

    return remembered(f"log:{store}", lambda: (stamp_of(store / "CHANGELOG.md"), stamp_of(KIT / "scripts" / "memwrite.py")), work)


# ── who is dreaming ──
def dreamer(store: Path) -> str:
    """The kit's sentence about the dream that is alive on this store, or empty when none is (scripts/dream_owner.py).
    A batch whose owner can't be read counts as held: the kit's sentence then says so, and giving it up is the way out."""
    result = ask(store, "dream_owner.py")
    return "" if result.returncode == 0 else said(result)


def release(store: Path) -> str:
    """The person gives up the dream that holds the store. Returns what the kit said it discarded."""
    result = ask(store, "dream_owner.py", "release")
    if result.returncode != 0:
        raise Refused(said(result))
    return result.stdout.strip()


# ── integrity ──
def seal(store: Path) -> str:
    """Have the kit version a change the person made to ORG.md: `memcheck.py --seal` writes the log row and commits,
    under the store's lock. Returns empty when ORG.md is sealed, else why it isn't, in the kit's words or the hub's."""
    result = ask(store, "memcheck.py", "--seal")
    first = (result.stdout.splitlines() or [""])[0]
    if first.startswith("sealed:") and "ORG.md" in [part.strip() for part in first[len("sealed:"):].split(",")]:
        return ""
    return said(result) if not first.startswith("sealed:") else "The kit could not commit the change (git may be busy in this memory)."


def problems(store: Path) -> list:
    """What the kit's memcheck.py finds wrong with the store, one line each: writes around the protocol, principles left
    unlocked, a record of proposals it can't read, proposals no dream finished."""
    result = ask(store, "memcheck.py")
    if result.returncode == 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()] or [said(result)]


# ── Claude Code's own notes ──
OWN_NOTES_LINE = re.compile(r"^(?P<repo>.+): auto memory (?P<on>on|off), (?P<notes>\d+) notes in (?P<folder>.+), \d+ not yet read by a dream$")


def own_notes_of(store: Path, repos: list, env=None):
    """Claude Code's own notes for each folder of a store, as the kit says: {repo: {"on", "notes", "folder"}}.

    Whether auto memory is on and where the notes are is the kit's rule (scripts/own_notes.py: managed settings, the
    folder's local and shared settings, the person's own; the env switch before autoMemoryEnabled). So the hub asks
    the kit and never works it out itself. Nothing when the kit can't say. `env` is for tests, which ask about a
    home folder of their own; those answers aren't kept.
    """
    script = KIT / "scripts" / "own_notes.py"
    base = [script, store / "roots.txt", store / "dreams" / "own-notes.json", HOME / ".claude" / "settings.json"]
    base += [Path(repo) / ".claude" / name for repo in repos for name in ("settings.local.json", "settings.json")]
    folders = []

    def work():
        try:
            result = ask(store, "own_notes.py", env=env)
        except Silent as quiet:
            folders[:] = []
            return damaged(script, quiet)
        rows = {m["repo"]: {"on": m["on"] == "on", "notes": int(m["notes"]), "folder": Path(m["folder"])} for m in map(OWN_NOTES_LINE.match, result.stdout.splitlines()) if m}
        answer = {repo: rows[repo] for repo in repos} if result.returncode == 0 and all(repo in rows for repo in repos) else None
        if answer is None:
            damaged(script, said(result)[:300])
        folders[:] = [info["folder"] for info in (answer or {}).values()]
        return answer

    if env is not None:
        return work()
    key = f"notes:{store}"
    folders[:] = [info["folder"] for info in (_kept[key][1] or {}).values()] if key in _kept else []
    return remembered(key, lambda: (tuple(repos), tuple(stamp_of(path) for path in base + folders)), work)


def only_an_empty_index(info: dict) -> bool:
    """One file, the index, with nothing in it: Claude Code makes that by itself, and it isn't a note."""
    index = info["folder"] / "MEMORY.md"
    try:
        return info["notes"] == 1 and index.is_file() and not index.read_text(errors="ignore").strip()
    except OSError:
        return False


def keeps_own_notes(store: Path, repos: list) -> bool:
    """Whether some folder of the store has notes of Claude's own and Claude Code still keeps them there."""
    found = own_notes_of(store, repos) or {}
    return any(info["on"] and info["notes"] and not only_an_empty_index(info) for info in found.values())

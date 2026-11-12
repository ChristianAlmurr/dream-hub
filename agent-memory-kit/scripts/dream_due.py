#!/usr/bin/env python3
"""Decide whether enough has happened in this store since the last dream to run another.

  python3 scripts/dream_due.py            one line; exit 0 when a dream is due, 3 when it is not
  python3 scripts/dream_due.py --json     the same as JSON, with the counts

A dream costs money, and a fixed day of the week is wrong both ways: one store has a hundred
sessions in a week and another has none. So the daily job asks here first. This counts what
happened in the window a dream would read, which is the time since the last dream and never more
than 30 days: the top-level sessions that ended in it (from the session archive, brought up to
date first) and the rows `CHANGELOG.md` has for `project/`, leaving out the rows the dream itself
wrote when its proposal was applied. A dream is due when a week has passed and there are a few
sessions, or there are many sessions, or many new memory lines, or a month has passed and there
was any session at all (a slow project is still checked for memory that went out of date).
Days are calendar days, so a dream on a Sunday evening can be due the next Sunday at the same hour.

enough() is the one definition of "enough to dream on", and counts() gives it its numbers. The
collector asks timing() for both and writes the answer into the batch (_timing.json) with the
moment it collected, so a dream this check starts is never turned away for having too little,
and reads exactly the window that was counted here.

Each check is recorded in ~/.local/state/agent-memory/<store>.json, which also says up to when
the last dream read (the moment its batch was collected, not the moment it finished, so what
happened in between is counted next time) and why it started. dream_finish.py writes those two. Before the first
dream of a store there is no such file, and the last dream is then taken from the store's git
history: the newest commit of a written proposal, else the first commit.

While a dream is alive on the store (dream_owner.holder(), the one rule the collector, dream_finish.py
and run-dream.sh use too) the check answers `not due: a dream is running`, with who and since when,
and starts nothing.

The same file lists the dreams the daily check started and how each ended. After one that wrote
no proposal the check waits 3 days before it starts another, whatever the counts say, so nothing
that goes wrong can start a paid run every day. run-dream.sh keeps that list through
scripts/dream_try.py, which agents have no permission rule for; a dream started by hand is
not held back and is not on the list.

While the store has an open proposal (dream_proposal.py) no dream is started either. On a day when one
would have been, the verdict names the proposal and its age, and the daily check (dream_try.py due)
reminds the person through the store's notify.sh: that day, and again every REMIND_DAYS days while
it stays open. `reminded` in the state file says which proposal was last said and when.

A dream that finishes voids the last check: `checked` and `verdict` go back to null, because that
verdict counted a window or named a running dream that is over. The next check writes them again.
"""
import argparse, contextlib, datetime, fcntl, json, math, os, re, sys
from pathlib import Path

import archive
import dream_owner
import dream_proposal
import needs
import memcheck
import slim

MEM = archive.MEM
WEEK_DAYS = 7  # this many days since the last dream, and at least ...
WEEK_SESSIONS = 3  # ... this many sessions
MANY_SESSIONS = 25  # this many sessions, however few days it took
MANY_LINES = 10  # this many rows for project/ in CHANGELOG.md, however few sessions wrote them
SLOW_DAYS = 30  # this many days since the last dream, and at least ...
SLOW_SESSIONS = 1  # ... this many sessions: enough to check memory against the code again
WINDOW_DAYS = 30  # a dream never reads further back, because Claude Code keeps transcripts that long
RETRY_DAYS = 3  # after a try that wrote no proposal, the daily check waits this long
REMIND_DAYS = 3  # while a proposal stays open and a dream is due, the person is reminded this often
HUNG_DAYS = 1  # a dream the daily check started that is still running after this long is said, once a day
TRIES_KEPT = 10
NOT_DUE = 3  # the exit code
HELD = 7  # run-dream.sh: another unattended run has the run lock
TURNED_AWAY = 4  # run-dream.sh: a dream is alive on the store, so a start by hand goes no further
BY_HAND = "started by hand"
REASON = "AGENT_MEMORY_DREAM_REASON"  # run-dream.sh sets it for the dream the daily check starts: the text after "due: "
TIMING = "_timing.json"  # in the batch: when it was collected, the window it holds, and whether that is enough to dream on
# In the message of a dream's commit: the store's commit the batch was collected at, and the moment up to which it read.
COLLECTED_AT, READ_UNTIL = "Collected-at", "Read-until"
SHA = re.compile(r"[0-9a-f]{40}")
DREAMER = "dreamer"  # the agent name /dream apply writes under
KEYS = ("last_dream", "last_reason", "checked", "verdict", "tries", "reminded", "waiting_said")
ENDINGS = {"proposal": "wrote a proposal", "nothing": "found nothing to propose", "failed": "failed", None: "did not finish"}
MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
ROW = re.compile(r"^\| ([^|]+?) \| project/[^|]* \| ([^|]*?) \| ")

REPAIR = "Correct it, or move it away and run again: the time of the last dream is then taken from the store's git history."

now = archive.now  # AGENT_MEMORY_NOW replaces the clock, so tests need no sleeping


def state_file():
    return archive.state() / f"{MEM.name}.json"


def day(at: float) -> str:
    d = datetime.datetime.fromtimestamp(at)
    return f"{d.day} {MONTHS[d.month - 1]}"


def days_between(start: float, end: float) -> int:
    """Calendar days in local time. The job runs at one hour of the day, and a dream that ended ten minutes past it is a week old a week later."""
    return max(0, (datetime.date.fromtimestamp(end) - datetime.date.fromtimestamp(start)).days)


def number(value) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


@contextlib.contextmanager
def locked():
    """One writer of the state file at a time: a daily check can meet a dream that is just finishing."""
    folder = archive.state()
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    with os.fdopen(os.open(folder / f".{MEM.name}.json.lock", os.O_WRONLY | os.O_CREAT, 0o600), "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def load() -> dict:
    """The state file, checked. With no file yet, every key is None and no dream has been tried."""
    path = state_file()
    found = {}
    if path.exists():
        try:
            found = json.loads(path.read_text())
        except (OSError, ValueError) as error:
            raise SystemExit(f"{path} can't be read as a state file ({type(error).__name__}). {REPAIR}")
        tries = found.get("tries") if isinstance(found, dict) else None
        good = (isinstance(found, dict)
                and all(found.get(key) is None or number(found[key]) for key in ("last_dream", "checked"))
                and all(found.get(key) is None or isinstance(found[key], str) for key in ("last_reason", "verdict"))
                and (tries is None or (isinstance(tries, list) and all(
                    isinstance(t, dict) and number(t.get("at")) and "ended" in t and t["ended"] in ENDINGS
                    and (t.get("alerted") is None or number(t["alerted"])) for t in tries)))
                and (found.get("waiting_said") is None or number(found["waiting_said"]))
                and (found.get("reminded") is None or (isinstance(found["reminded"], dict) and isinstance(found["reminded"].get("proposal"), str)
                                                       and number(found["reminded"].get("at")))))
        if not good:
            raise SystemExit(f"{path} is not a state file this version can read. {REPAIR}")
    state = {**dict.fromkeys(KEYS), **found}
    state["tries"] = state["tries"] or []
    return state


def update(change) -> dict:
    """Read the state file, let `change` alter it, and write it back, all under the lock, so nothing another
    writer did in between is lost. The whole file or nothing, so a reader never sees half. Unchanged: not written."""
    with locked():
        state = load()
        before = json.dumps(state, sort_keys=True)
        change(state)
        if json.dumps(state, sort_keys=True) != before:
            write(json.dumps(state, indent=1) + "\n")
    return state


def write(text: str, keep: bool = True):
    """Write the state file whole, by way of a temporary file. Called under the lock. `keep` False only tries it."""
    path = state_file()
    tmp = path.with_name(f".{path.name}.writing")  # one name is enough: only the holder of the lock writes
    try:
        with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as handle:
            handle.write(text)
        os.replace(tmp, path) if keep else os.unlink(tmp)
    except OSError as error:
        raise SystemExit(f"{path} can't be written ({type(error).__name__}: {error.strerror}). Nothing was recorded.")


def ready():
    """Stops, in plain words, unless the state file can be read, makes sense now and can be written.

    run-dream.sh asks before it starts a dream by hand, and dream_finish.py before it commits, so a finish
    does everything or nothing.
    """
    with locked():
        trusted(load(), now())
        write("", keep=False)


def clock(at: float) -> str:
    return f"{day(at)} {datetime.datetime.fromtimestamp(at):%H:%M}"


def running_words(kept: dict) -> str:
    """For whoever is turned away by a dream that is alive (dream_owner.holder): who has the store, since when, and until when at most if it has died."""
    said = f"A dream is already running for {MEM.name}: {kept['who']}, since {clock(kept['since'])}"
    if kept["until"] is None:  # a process holds a lock: it ends when that process does
        return said + "."
    if kept["heard"] > kept["since"]:
        said += f", last heard from {clock(kept['heard'])}"
    return said + f". If it has died, another can start from {clock(kept['until'])}, or its person can give it up now with `{dream_owner.RELEASE}`."


def take_run_lock(pid: str = "") -> int:
    """For run-dream.sh: take the run lock and write the run's process number into its file, or say who has it. The exit code: 0, or HELD."""
    since = dream_owner.take_run_lock()
    if since is None:
        dream_owner.note_pid(9, pid)
        return 0
    print(running_words({"who": dream_owner.UNATTENDED, "since": since, "heard": None, "until": None}))
    return HELD


def proposal_waiting() -> int:
    """For run-dream.sh before a start by hand: has the store an open proposal? Says which and what to do. The exit code: 0, or TURNED_AWAY."""
    waiting = dream_proposal.open_one(MEM)
    if waiting is None:
        return 0
    print(dream_proposal.waiting_words(waiting))
    return TURNED_AWAY


def turned_away() -> int:
    """For run-dream.sh before a start by hand, which has collected nothing yet: is a dream alive on the store? Says who. The exit code: 0, or TURNED_AWAY."""
    kept = dream_owner.holder()
    if not kept:
        return 0
    print(running_words(kept))
    return TURNED_AWAY


def from_history() -> int:
    """When the last dream was, for a store with no state file: its newest commit of a written proposal, else its first commit.

    `dream: no proposal` is not one: that run read nothing, so what came before it still counts.
    """
    when, _, body = memcheck.git(MEM, "log", "-1", "--format=%ct%n%b", r"--grep=^dream: .*-proposal\.md$").stdout.partition("\n")
    if when.strip():
        # The commit says up to when that dream read. One from before it did has only its own time.
        said = re.search(rf"^{READ_UNTIL}: (\d+)$", body, re.M)
        return int(said[1]) if said else int(when)
    times = memcheck.git(MEM, "log", "--max-parents=0", "--format=%ct").stdout.split()
    if times:
        return min(int(t) for t in times)
    raise SystemExit(f"Can't tell when {MEM.name} last dreamed: {state_file()} doesn't exist and the store at {MEM} has no git history.")


def last_dream(state: dict, at: float):
    """Up to when the last dream read. A time later than now is refused: see trusted()."""
    return trusted(dict(state, last_dream=state["last_dream"] if state["last_dream"] is not None else from_history()), at)["last_dream"]


def trusted(state: dict, at: float) -> dict:
    """The state, or a stop: a last dream or a try later than now would make every count empty or hold the check back for ever."""
    for what, when in ([("The last dream", state["last_dream"])] + [("A dream the daily check started", t["at"]) for t in state["tries"]]
                       + [("The last reminder about an open proposal", (state.get("reminded") or {}).get("at")),
                          ("The last reminder about approved changes that wait", state.get("waiting_said"))]):
        if when is not None and when > at:
            raise SystemExit(f"{what} of {MEM.name} is recorded as {archive.iso(when)}, which is later than now ({archive.iso(at)}). "
                             f"Correct it in {state_file()}, or move that file away and the time of the last dream is taken from the store's git history.")
    return state


def sessions_in(start: float, end: float) -> int:
    """Top-level sessions that ended in the window and in which work was done (slim.working, as the archive recorded it).

    A subagent's file has an `agent:` line and is part of its parent's session. A session kept from before the
    archive recorded this has no `working:` line and counts.
    """
    count = 0
    for path in archive.folder().glob("*.md"):
        try:
            h = archive.header(path)
        except FileNotFoundError:
            continue  # another archive run removed it between the listing and the read
        count += bool(h) and "agent" not in h and h.get("working") != "no" and slim.within(h["ended_at"], start, end)
    return count


def lines_in(start: float, end: float):
    """Rows in CHANGELOG.md for a file in project/ written in the window, and how many had a time that can't be read.

    Rows by the dreamer are left out: what the last dream applied is not new learning to consolidate.
    memwrite logs local time with no zone; a time with Z or an offset is read as what it says.
    """
    log = MEM / "CHANGELOG.md"
    count = skipped = 0
    for line in (log.read_text(errors="ignore").splitlines() if log.exists() else []):
        row = ROW.match(line)
        if not row or row[2] == DREAMER:
            continue
        try:
            when = datetime.datetime.fromisoformat(re.sub(r"Z$", "+00:00", row[1])).timestamp()
        except (ValueError, OverflowError):
            skipped += 1  # said in the verdict; one bad row in an append-only log must not stop every later check
            continue
        count += slim.within(when, start, end)
    return count, skipped


def counts() -> dict:
    """What happened in this store in the window a dream would read now. The archive is brought up to date first."""
    at = now()
    archive.update()
    last = last_dream(load(), at)
    since = max(last, at - WINDOW_DAYS * 86400)
    lines, skipped = lines_in(since, at)
    return {"sessions": sessions_in(since, at), "memory_lines": lines, "days": days_between(last, at),
            "last_dream": last, "since": since, "at": at, "skipped_rows": skipped}


def enough(sessions: int, memory_lines: int, days: int) -> bool:
    """Whether a dream has enough to work on. Every way of being due passes this, so a due dream always goes ahead."""
    return sessions >= WEEK_SESSIONS or memory_lines >= MANY_LINES or (days >= SLOW_DAYS and sessions >= SLOW_SESSIONS)


def plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def verdict(c: dict):
    """Whether a dream is due by the counts, and the one line that says why."""
    sessions, lines, days = c["sessions"], c["memory_lines"], c["days"]
    since = f" since {day(c['since'])}"
    note = f"; {plural(c['skipped_rows'], 'changelog row')} skipped (time unreadable)" if c["skipped_rows"] else ""
    if (sessions >= MANY_SESSIONS or (days >= WEEK_DAYS and sessions >= WEEK_SESSIONS)
            or (days >= SLOW_DAYS and sessions >= SLOW_SESSIONS)):
        return True, f"due: {plural(sessions, 'session')}{since}{note}"
    if lines >= MANY_LINES:
        return True, f"due: {plural(lines, 'memory line')}{since}{note}"
    return False, f"not due: {plural(sessions, 'session')}, {plural(lines, 'memory line')}, {plural(days, 'day')}{note}"


def waiting(state: dict, at: float) -> str:
    """Why the daily check must not start a dream yet, or empty. The guard that holds whatever the counts say."""
    if not state["tries"]:
        return ""
    last = state["tries"][-1]
    if last["ended"] == "proposal" or days_between(last["at"], at) >= RETRY_DAYS:
        return ""
    return f"the last try on {day(last['at'])} {ENDINGS[last['ended']]}; next try from {day(last['at'] + RETRY_DAYS * 86400)}"


def reason() -> str:
    """Why this dream started: what the daily check said, or by hand."""
    return " ".join(os.environ.get(REASON, "").split())[:200] or BY_HAND


def timing() -> dict:
    """What the collector stamps into the batch: the moment it collects, the window, and whether that is enough.

    `since` to `collected` is the window, the same one the daily check counted. `collected` becomes the
    store's last_dream if this dream writes a proposal, so the next window starts exactly where this one ends.
    """
    c = counts()
    ok = enough(c["sessions"], c["memory_lines"], c["days"])
    had = f"{plural(c['sessions'], 'session')}, {plural(c['memory_lines'], 'memory line')}, {plural(c['days'], 'day')} since the last dream"
    need = f"{WEEK_SESSIONS} sessions, or {MANY_LINES} memory lines, or {SLOW_DAYS} days and {plural(SLOW_SESSIONS, 'session')}"
    return {"collected": c["at"], "since": c["since"], "sessions": c["sessions"], "memory_lines": c["memory_lines"], "days": c["days"],
            "enough": ok, "by_hand": reason() == BY_HAND,
            "line": f"enough to dream on: {had}" if ok else f"too little to dream on: {had} (it takes {need})"}


def stamp(batch: Path) -> dict:
    """What the collector stamped into the batch behind a proposal: `collected` (up to there the dream has read), `head`, and `proposal`, the file name this dream's proposal has."""
    path = batch / TIMING
    try:
        found = json.loads(path.read_text())
    except (OSError, ValueError):
        found = None
    if not (isinstance(found, dict) and number(found.get("collected")) and isinstance(found.get("head"), str) and SHA.fullmatch(found["head"])
            and isinstance(found.get("proposal"), str) and dream_proposal.NAME.fullmatch(found["proposal"])):
        raise SystemExit(f"{path} doesn't say when this batch was collected, so there is no telling what the proposal read. Nothing was committed. "
                         "A proposal is written from a batch the collector finished in the same dream: start the dream again from its first step. "
                         "If the collector then says a dream is already running, a dream that died may have left its batch behind: "
                         f"the collector says from when another can start, or delete {batch}.")
    return found


def trailers(stamped: dict) -> str:
    """For the message of the dream's commit: where and when its batch was collected, which the next dream starts from."""
    return f"{COLLECTED_AT}: {stamped['head']}\n{READ_UNTIL}: {int(stamped['collected'])}"


def check(remind: bool = False) -> dict:
    """Count, decide and record the check.

    With `remind`, for the daily check only: when a dream is due and an open proposal holds it back, the answer
    carries `remind`, the words for the person, on the first such day and every REMIND_DAYS days after it. And
    when a person's dream has held the store for HUNG_DAYS or more, it carries `hung`, once a day. And when a change the
    person approved has waited WAIT_DAYS or more to be done, or something a dream said needs the person has been
    open that long (needs.py), it carries `waiting`, which says both, at the rhythm of `remind` (`waiting_said` in the state file).
    """
    found = counts()
    pending = dream_proposal.open_one(MEM)
    approved = dream_proposal.waiting(MEM)  # what the person approved and nobody has done yet, oldest first
    needed = sorted(needs.load(MEM), key=lambda e: e["at"])  # what dreams said needs the person and is not closed, oldest first
    at = found.pop("at")
    found["due"], found["line"] = verdict(found)
    kept = dream_owner.holder(silent=dream_owner.PATIENT)  # the daily check can wait a day; a person's dream that only waits for them is left alone
    if kept:  # a person's /dream is in progress: starting another would only be told so by the collector
        found["due"], found["line"] = False, f"not due: a dream is running ({kept['who']}, since {clock(kept['since'])})"
        owner = kept["owner"]
        if (remind and owner and not owner["run"] and days_between(owner["since"], at) >= HUNG_DAYS
                and (owner["alerted"] is None or days_between(owner["alerted"], at) >= 1)):
            dream_owner.alerted(dream_owner.batch(), at)
            found["hung"] = (f"{kept['who'].capitalize()} has held the store with a dream for {plural(days_between(owner['since'], at), 'day')}, since {clock(owner['since'])}. "
                             f"If nobody is working on it, give it up with `{dream_owner.RELEASE}`, and the daily check starts again by itself.")

    def change(state):
        # Only a store's first check settles last_dream, from the git history. Decided here, under the lock:
        # a dream that finished while this was counting has written a newer one, and that one stays.
        if state["last_dream"] is None:
            state["last_dream"] = found["last_dream"]
        hold = waiting(state, at)
        if found["due"] and hold:
            found["due"], found["line"] = False, f"not due: {hold}"
        if pending is None:
            state["reminded"] = None
        elif found["due"]:  # a dream would start today: its proposal would push the open one, and the ticks in it, out of sight
            was = found["line"][len("due: "):]
            found["due"], found["line"] = False, f"not due: the proposal {pending['name']} ({dream_proposal.age(pending)}) has not been applied or dismissed"
            said = state["reminded"]
            if remind and (said is None or said["proposal"] != pending["name"] or days_between(said["at"], at) >= REMIND_DAYS):
                state["reminded"] = {"proposal": pending["name"], "at": int(at)}
                found["remind"] = (f"A new dream is due ({was}) and waits for the proposal {dream_proposal.age(pending)}. "
                                   "Tick and apply it, or dismiss it. Dismissing loses nothing: every item goes to the next dream. "
                                   "Applying takes each item you leave unticked as a no: it comes back once, and not after a second no.")
        long = lambda group: bool(group) and days_between(group[0]["at"], at) >= dream_proposal.WAIT_DAYS  # the oldest has waited long enough to be said
        if not approved and not needed:
            state["waiting_said"] = None
        elif (remind and (long(approved) or long(needed))
              and (state["waiting_said"] is None or days_between(state["waiting_said"], at) >= REMIND_DAYS)):
            state["waiting_said"] = int(at)
            said = []
            if long(approved):
                mine, session = (sum(e["waiting"] == who for e in approved) for who in ("you", "a session"))
                routes = ([f"{mine} for you (ORG.md or Steering): in a terminal, `chflags nouchg memory/ORG.md`, then `python3 scripts/dream_apply.py --yours`"] if mine else []) \
                    + ([f"{session} for a session: `/dream apply` in a session does them"] if session else [])
                said.append(f"{plural(len(approved), 'approved change')} {'is' if len(approved) == 1 else 'are'} waiting to be done, the oldest for "
                            f"{plural(days_between(approved[0]['at'], at), 'day')}: {'; '.join(routes)}. "
                            "`python3 scripts/dream_proposal.py` lists them, and says how one is taken back.")
            if long(needed):
                said.append(f"{plural(len(needed), 'thing')} a dream said {'needs' if len(needed) == 1 else 'need'} you {'is' if len(needed) == 1 else 'are'} open, the oldest for "
                            f"{plural(days_between(needed[0]['at'], at), 'day')}. `python3 scripts/dream_proposal.py` lists them, and says how one is closed.")
            found["waiting"] = " Also: ".join(said)
            found["waiting_title"] = "Approved changes waiting" if long(approved) else "Needs you"
        state.update(checked=int(at), verdict=found["line"])

    update(change)
    return found


def finished(read_until=None):
    """Called by dream_finish.py: with the moment the batch was collected when a proposal was written, with nothing when none was.

    Only a written proposal moves the last dream, and it moves it to where the dream stopped reading. A dream
    the daily check started gets its ending; one started by hand is not on that list and changes nothing in it.
    Either way the last check is void: its verdict counted the window this dream read, or said this dream was
    running. Left in place it would stand beside a later last dream and read as the state now.
    """
    why = reason()

    def change(state):
        # Never backwards: a proposal from an older batch must not undo what a later dream read.
        if read_until is not None and (state["last_dream"] is None or read_until >= state["last_dream"]):
            state.update(last_dream=int(read_until), last_reason=why)
        if why != BY_HAND:
            end_try(state, "nothing" if read_until is None else "proposal")
        state.update(checked=None, verdict=None)

    update(change)


def end_try(state: dict, ending: str):
    if state["tries"] and state["tries"][-1]["ended"] is None:
        state["tries"][-1]["ended"] = ending


def try_start():
    """The daily check is about to start a dream. Recorded first, so even a run that is killed counts as a try."""
    update(lambda state: state.update(tries=(state["tries"] + [{"at": int(now()), "ended": None}])[-TRIES_KEPT:]))


def try_held() -> str:
    """run-dream.sh found a dream running. The words for an alert if the daily check's own try has been open too long, else empty.

    Once a day at most, however often the check runs: the try remembers when it was last said.
    """
    at, said = now(), []

    def change(state):
        last = state["tries"][-1] if state["tries"] else None
        if last is None or last["ended"] is not None or days_between(last["at"], at) < HUNG_DAYS:
            return
        if last.get("alerted") is not None and days_between(last["alerted"], at) < 1:
            return
        last["alerted"] = int(at)
        began = datetime.datetime.fromtimestamp(last["at"])
        said.append(f"It has been running for {plural(days_between(last['at'], at), 'day')}, since {day(last['at'])} {began:%H:%M}. "
                    f"Look at {archive.state() / ('dream-' + MEM.name + '.log')}: if nothing has been added to it for hours, the dream is stuck. "
                    "Stop that `claude -p /dream` process, and the daily check starts again by itself.")

    update(change)
    return "".join(said)


def try_failed() -> str:
    """The dream the daily check started never reached dream_finish.py. Returns when the next try will be, for the alert."""
    state = update(lambda state: end_try(state, "failed"))
    return f"Next try from {day(state['tries'][-1]['at'] + RETRY_DAYS * 86400)}." if state["tries"] else ""


def main():
    ap = argparse.ArgumentParser(description="Is a dream due for this store?")
    ap.add_argument("--json", action="store_true", help="print the verdict and the counts as JSON")
    a = ap.parse_args()
    found = check()
    print(json.dumps(found) if a.json else found["line"])
    sys.exit(0 if found["due"] else NOT_DUE)


if __name__ == "__main__":
    main()

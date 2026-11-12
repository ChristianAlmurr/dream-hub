#!/usr/bin/env python3
"""Who is dreaming on this store, and whether that dream is still alive.

  python3 scripts/dream_owner.py           who is dreaming on the store, or that nobody is (exit 4 when someone is)
  python3 scripts/dream_owner.py alive     for the dream itself, between its steps: this dream is still at work
  python3 scripts/dream_owner.py release   for the person: give up the dream that holds the store, and say what was discarded

One rule, in holder(), for everything that must not start a second dream or touch a batch that is
in use: the collector, the daily check (dream_due.py), dream_finish.py and run-dream.sh. And one
answer to "is this batch mine", mine(), asked under the store's lock by whoever then acts on it.

A dream owns the store's batch from the moment its collector starts until dream_finish.py clears
it. The collector writes the owner into the batch as _owner.json. Whether that owner is still
alive is read from what a dream leaves behind by being alive, never from a guess at how long a
dream takes, and never from signs that only show its session is busy with something:

- An unattended run (run-dream.sh) holds the run lock for as long as it lives, and the system takes
  the lock away the moment it ends, however it ends. It is alive exactly while the lock is held.
- A collector holds the collect lock while it runs, the same way. A batch whose collector is running
  is alive. A batch with an owner and no _timing.json, and no collector running, was left by a
  collector that was killed: it is dead at once.
- After that, a dream a person started (in a session, or from a terminal) can hold no lock across its
  many steps. Its signs of life are: the moment it collected; `renewed`, which its own scripts write
  (the collector when it ends, proposal_check.py, and `dream_owner.py alive`, which dream.md has the
  orchestrator run between its steps); and, for a session, every write to the transcript of an analyst
  it started after it collected. Claude Code writes those at each step an analyst takes, so the long
  stretch in which the orchestrator waits and can run nothing is covered without anyone remembering to.
  The dream is alive while the newest of these is younger than SILENT.
- The session's own transcript says the session is busy, not that it is dreaming: a person who
  interrupts a dream and carries on with other work keeps writing it. So it counts only for GRACE after
  the last sign of the dream itself, for the orchestrator's own work between two of its steps.

The daily check waits longer than a person does (PATIENT in place of SILENT): it loses nothing by
coming back tomorrow, and a dream that only waits for its person is then not replaced by it.

A dream that is wrongly taken for dead can't harm the one that took over: dream_finish.py finishes
only the batch its own dream owns, asked and acted on under one hold of the store's lock.
"""
import contextlib, datetime, fcntl, glob, json, math, os, shutil, sys, time
from pathlib import Path

import archive
import dream_proposal
import memcheck
import slim

MEM = archive.MEM
OWNER = "_owner.json"  # in the batch: {"run", "session", "terminal", "since", "renewed", "replaced", "alerted"}
STAMP = "_timing.json"  # the collector's last write: the batch is whole
RUN = "AGENT_MEMORY_DREAM_RUN"  # run-dream.sh sets it once it holds the run lock, so everything it starts knows it is that run
SESSION = "CLAUDE_CODE_SESSION_ID"  # Claude Code sets it for every command a session or its subagents run
SILENT = 30 * 60  # a person's dream that has shown no sign of life for this long is dead, for anyone who wants to start one
GRACE = 30 * 60  # the session's own transcript counts as the dream's for this long after the dream's last sign
PATIENT = 6 * 3600  # the daily check waits this long instead of SILENT
OLDER_KIT = 2 * 3600  # a batch with no owner was collected by an older kit, which called it running for this long
ANALYSTS = ("transcript-analyst", "notes-analyst")  # the subagents a dream starts
UNATTENDED = "an unattended run (run-dream.sh)"
LOCK_TRIES, LOCK_WAIT = 6, 0.25  # whoever takes a lock asks this often, this far apart, before believing another has it
RELEASE = "python3 scripts/dream_owner.py release"
HELD = 4  # the exit code when a dream is alive on the store

now = archive.now


def batch() -> Path:
    return MEM / "dreams" / "_batch"


def run_lock() -> Path:
    return archive.state() / f"{MEM.name}.run.lock"


def collect_lock() -> Path:
    return archive.state() / f"{MEM.name}.collect.lock"


@contextlib.contextmanager
def store_locked():
    """The store's lock (memcheck.locked), with a plain stop when the store can't be written."""
    try:
        hold = memcheck.locked(MEM)
        hold.__enter__()
    except OSError as error:
        raise SystemExit(f"{MEM} can't be written ({type(error).__name__}: {error.strerror}), so no dream can own its batch. Nothing was changed.")
    try:
        yield
    finally:
        hold.__exit__(None, None, None)


def terminal():
    """The terminal a command was typed in, as the number of its process session, or None for a process that has no terminal.

    Every command typed in one terminal window shares that number, and another window has another. A process
    with no terminal (a command a program runs, each in a process session of its own) has nothing that the
    next command of the same dream would share, so it has no name here.
    """
    try:
        os.close(os.open("/dev/tty", os.O_RDONLY))
    except OSError:
        return None
    return os.getsid(0)


def me() -> dict:
    """The dream this process belongs to: the unattended run it was started by, else the session it runs in, else the terminal it was typed in."""
    run, session = os.environ.get(RUN) or None, os.environ.get(SESSION) or None
    return {"run": run, "session": None if run else session, "terminal": None if run or session else terminal()}


def named(i: dict) -> bool:
    """Has this start a name that its later steps will have too?"""
    return any(i[key] is not None for key in ("run", "session", "terminal"))


NAMELESS = (f"This start has nothing its later steps could be known by: no {SESSION} (Claude Code sets it for every command of a session), "
            f"no unattended run, and no terminal. Set {SESSION} to one name of your choosing for every command of this dream, and start again.")


def same(a: dict, b: dict) -> bool:
    """Are these one dream? An unattended run is known by its run, a session's dream by its session, any other by its terminal. Two with no name are never one."""
    for key in ("run", "session"):
        if a[key] or b[key]:
            return a[key] == b[key]
    return a["terminal"] is not None and a["terminal"] == b["terminal"]


def who(found: dict) -> str:
    if found["run"]:
        return UNATTENDED
    if found["session"]:
        return f"session {found['session']}"
    return f"a dream started from a terminal (terminal session {found['terminal']})" if found["terminal"] is not None else "a dream that left no name"


def number(value) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def read(folder: Path):
    """The owner written in a batch, or None when it has none. One that can't be read or trusted stops the run: nobody can tell whose batch it is."""
    path = folder / OWNER
    way_out = f"If no dream is running on {MEM.name}, release the batch with `{RELEASE}`."
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise SystemExit(f"{path} can't be read ({type(error).__name__}), so there is no telling whose batch this is. {way_out}")
    try:
        found = json.loads(text)
    except ValueError:
        found = None
    replaced = found.get("replaced") if isinstance(found, dict) else None
    good = (isinstance(found, dict) and all(found.get(key) is None or isinstance(found[key], str) for key in ("run", "session"))
            and (found.get("terminal") is None or type(found["terminal"]) is int)
            and number(found.get("since")) and all(found.get(key) is None or number(found[key]) for key in ("renewed", "alerted"))
            and (replaced is None or (isinstance(replaced, dict) and all(replaced.get(key) is None or isinstance(replaced[key], str) for key in ("run", "session"))
                                      and (replaced.get("terminal") is None or type(replaced["terminal"]) is int)
                                      and all(number(replaced.get(key)) for key in ("since", "heard")))))
    if not good:
        raise SystemExit(f"{path} doesn't say who owns this batch, so there is no telling whose it is. {way_out}")
    at = now()
    for key in ("since", "renewed"):  # a time later than now would keep a dead dream alive for as long as the clock is behind it
        if found.get(key) is not None and not 0 <= found[key] <= at:
            raise SystemExit(f"{path} says this batch was claimed or renewed at a time later than now ({archive.iso(at)}), or before any clock began, so there is no telling whether its dream is alive. {way_out}")
    return {"run": found.get("run") or None, "session": found.get("session") or None, "terminal": found.get("terminal"),
            "since": found["since"], "renewed": found.get("renewed"), "replaced": replaced, "alerted": found.get("alerted")}


def write(folder: Path, found: dict):
    """Write the owner whole. Called under the store's lock."""
    path = folder / OWNER
    tmp = path.with_name(path.name + ".writing")
    tmp.write_text(json.dumps(found, indent=1) + "\n")
    os.replace(tmp, path)


def mine(folder: Path) -> bool:
    """Is the batch this dream's? The one way to ask. False when it has no owner. Ask it under the store's lock and act before letting go."""
    found = read(folder)
    return found is not None and same(found, me())


def replaced_me(found: dict) -> bool:
    """Did the dream that owns this batch take it from this dream?"""
    return found["replaced"] is not None and same(found["replaced"], me())


def started(path: Path):
    """When a transcript began: the time of its first entry, or None when it can't be read."""
    try:
        with open(path, errors="ignore") as handle:
            entry = json.loads(handle.readline())
        return datetime.datetime.fromisoformat(entry["timestamp"].replace("Z", "+00:00")).timestamp()
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None  # no first entry with a time: not a transcript this dream can be shown to have started


def analyst(path: Path) -> bool:
    """Is this the transcript of one of the subagents a dream starts? Claude Code names the agent in a file beside it."""
    try:
        return json.loads(path.with_name(path.stem + ".meta.json").read_text())["agentType"] in ANALYSTS
    except (OSError, ValueError, KeyError, TypeError):
        # Claude Code documents no such file. If it is gone or reads differently one day, every subagent the session started after
        # the claim counts as a sign of life: a dream then lives too long, and the one-hour and six-hour rules still end it,
        # where one that is wrongly dead loses what it has spent.
        return True


def heard(found: dict) -> float:
    """The last sign of life of a person's dream: see the top of this file."""
    last = max(found["since"], found["renewed"] or 0)
    if not found["session"]:
        return last
    base, session = Path.home() / ".claude" / "projects", glob.escape(found["session"])
    for path in base.glob(f"*/{session}/subagents/*.jsonl"):
        began = started(path)
        if began is not None and began >= found["since"] and analyst(path):
            with contextlib.suppress(FileNotFoundError):  # removed between the listing and the look
                last = max(last, path.stat().st_mtime)
    own = 0
    for path in base.glob(f"*/{session}.jsonl"):
        with contextlib.suppress(FileNotFoundError):
            own = max(own, path.stat().st_mtime)
    return max(last, min(own, last + GRACE))


def lock_held(path: Path) -> bool:
    """Does a live process hold this lock? Asked by someone who doesn't."""
    try:
        handle = open(path)
    except FileNotFoundError:
        return False  # nobody has ever taken it
    with handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)  # shared: two that ask at once don't take each other for a holder
        except BlockingIOError:
            return True
    return False


def take(fd: int) -> bool:
    """Take a lock for as long as this open file lasts. False when a live process has it.

    Whoever asks lock_held() has the lock, shared, for an instant. A start in that instant must not take
    that for a holder (a run would skip its day), so it asks again for a little over a second: a real
    holder keeps the lock for minutes, an asker for less than a millisecond.
    """
    for attempt in range(LOCK_TRIES):
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            time.sleep(LOCK_WAIT)
    return False


def take_run_lock(fd: int = 9):
    """For run-dream.sh, on the file it opened: take the run lock. Returns None when this run has it now, else the moment the run that holds it took it.

    The file's time is that moment: only the run that has just taken the lock sets it.
    """
    if not take(fd):
        return os.fstat(fd).st_mtime
    os.utime(fd)
    return None


def note_pid(fd: int, pid):
    """Write into a lock's file which process holds it, for the person who is told to stop that process."""
    os.ftruncate(fd, 0)
    os.pwrite(fd, f"{pid}\n".encode(), 0)


def pid_in(path: Path) -> str:
    """ "process 123" from a lock's file, or "a process" when it doesn't say. """
    try:
        text = path.read_text().strip()
    except OSError:
        text = ""
    return f"process {text}" if text.isdigit() else "a process"


def take_collect_lock():
    """For the collector: the open file that holds the collect lock until this process ends, or None when another collector is running."""
    path = collect_lock()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    handle = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
    if take(handle):
        os.utime(handle)
        note_pid(handle, os.getpid())
        return handle
    os.close(handle)
    return None


def holder(folder: Path = None, silent: float = SILENT, collecting: bool = False):
    """The dream that is alive on this store, or None: {"who", "since", "heard", "until", "owner"}.

    `heard` is its last sign of life and `until` the moment it counts as dead if nothing more is heard;
    both are None for a dream that holds a lock, which needs neither. `owner` is the batch's owner, or None
    when the batch has none (a run that has not collected yet, a batch from an older kit).
    `collecting` says the one who asks holds the collect lock, so no other collector can be running.
    """
    folder, i = folder or batch(), me()
    if not i["run"] and lock_held(run_lock()):
        return {"who": UNATTENDED, "since": run_lock().stat().st_mtime, "heard": None, "until": None, "owner": None}
    if not collecting and lock_held(collect_lock()):
        # A collector is at work, perhaps before it has claimed the batch: the store is its dream's from the moment it starts.
        return {"who": f"a dream whose collector is running now ({pid_in(collect_lock())})", "since": collect_lock().stat().st_mtime, "heard": None, "until": None, "owner": None}
    found = read(folder)
    if found is None:
        return older_kit(folder)
    if found["run"]:
        # Reached only with no other run alive: from outside a run the lock was free, and inside one this run holds it.
        return {"who": UNATTENDED, "since": found["since"], "heard": None, "until": None, "owner": found} if same(found, i) else None
    if not (folder / STAMP).exists():
        return None  # an owner, no stamp and no collector running: the collector was killed
    last = heard(found)
    if now() - last >= silent:
        return None
    return {"who": who(found), "since": found["since"], "heard": last, "until": last + silent, "owner": found}


def older_kit(folder: Path):
    """The holder of a batch that has no owner written, or None.

    A collector that stops takes its batch with it, so a batch with no owner was collected by a kit from
    before owners were written. By that kit's rule its dream is running for two hours after _metrics.json.
    """
    try:
        written = (folder / "_metrics.json").stat().st_mtime
    except FileNotFoundError:
        return None
    if time.time() - written >= OLDER_KIT:  # a file's time is the real clock's
        return None
    return {"who": "a dream started by an older kit", "since": written, "heard": written, "until": written + OLDER_KIT, "owner": None}


def draft(folder: Path):
    """The proposal a batch's dream wrote and never finished: the file its stamp names, if no record knows it. Else None."""
    try:
        name = json.loads((folder / STAMP).read_text())["proposal"]
    except (OSError, ValueError, KeyError, TypeError):
        return None  # no stamp that names a proposal: the dream never got as far as writing one
    path = MEM / "dreams" / name if isinstance(name, str) and dream_proposal.NAME.fullmatch(name) else None
    if path is None or not path.is_file() or any(p["name"] == name for p in dream_proposal.load(MEM)["proposals"]):
        return None
    return path


def drop_draft(folder: Path) -> str:
    """Delete the unfinished proposal of the dream that had this batch, with the file beside it, and return the proposal's name, or empty.

    It was never through proposal_check.py and never committed. Left in dreams/, it would look like a
    proposal to anything that lists them.
    """
    path = draft(folder)
    if path is None:
        return ""
    path.unlink()
    path.with_name(dream_proposal.side_file(path.name)).unlink(missing_ok=True)
    return path.name


def claim(folder: Path):
    """The collector, holding the collect lock: make this dream the owner of the batch, unless another dream that is alive has it.

    Returns (None, <name of the unfinished proposal it removed, or empty>) when the batch is this dream's
    now, else (the holder that keeps it, ""). The collector calls it after every check that could still
    stop it, so nothing of another dream's is touched by a start that then fails: only here, once the
    batch is this dream's, do the stamp of the dream before and the proposal it never finished go. Decided and written
    under the store's lock, so no finish and no release can act on the batch in between. A dream may
    collect again over its own batch. What a dead dream left stays until empty() clears it.
    A dream that takes the batch from a person's dream that went silent writes down whose it was and when
    that one was last heard from (`replaced`), so that if it was only waiting for its person, they can be
    told exactly what happened.
    """
    with store_locked():
        kept = holder(folder, collecting=True)
        if kept and not (kept["owner"] and mine(folder)):
            return kept, ""
        gone = drop_draft(folder)
        (folder / STAMP).unlink(missing_ok=True)  # whatever stops the collector from here on, an old stamp can't pass for a new one
        was, i = read(folder), me()
        replaced = None
        if was is not None and not was["run"] and not same(was, i):
            replaced = {"run": None, "session": was["session"], "terminal": was["terminal"], "since": was["since"], "heard": heard(was)}
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        folder.chmod(0o700)  # the batch holds what was said in sessions: its owner's only, whatever the umask
        write(folder, {**i, "since": now(), "renewed": None, "replaced": replaced, "alerted": None})
    return None, gone


def empty(folder: Path):
    """Clear the batch for the dream that owns it: everything goes but the owner, so the batch is never without one."""
    for path in folder.iterdir():
        if path.name == OWNER:
            continue
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()


def whole(folder: Path, stamp: str) -> bool:
    """The collector's last step: the batch is whole. Under the store's lock: if the batch is still this dream's, its stamp is written and it is a sign of life."""
    with store_locked():
        if not mine(folder):
            return False
        write(folder, dict(read(folder), renewed=now()))
        (folder / STAMP).write_text(stamp)
    return True


def renew(folder: Path = None) -> bool:
    """A sign of life from the dream itself: its own script ran. False, and nothing written, when the batch is not this dream's."""
    folder = folder or batch()
    with store_locked():
        if not mine(folder):
            return False
        write(folder, dict(read(folder), renewed=now()))
    return True


def give_up(folder: Path):
    """The collector stopped before the batch was whole: take the batch away, if it is this dream's, so nobody waits for it or mistakes it for one that is."""
    with store_locked():
        if folder.is_dir() and mine(folder):
            shutil.rmtree(folder)


def alerted(folder: Path, at: float):
    """The daily check has told the person about this long-held batch. Written under the store's lock."""
    with store_locked():
        found = read(folder)
        if found is not None:
            write(folder, dict(found, alerted=at))


def release(folder: Path = None) -> str:
    """The person gives up the dream that holds the store, from any session or a terminal. Returns what was discarded, in words.

    Not while a process is at work on it: an unattended run or a running collector is stopped first.
    """
    folder = folder or batch()
    if lock_held(run_lock()):
        raise SystemExit(f"{UNATTENDED.capitalize()} is dreaming on {MEM.name} right now ({pid_in(run_lock())}; its log is {archive.state() / ('dream-' + MEM.name + '.log')}). "
                         "Nothing was released. To end it, stop that process (`kill` and its number): once it has stopped, its batch counts as dead and the next dream starts by itself.")
    if lock_held(collect_lock()):
        raise SystemExit(f"A collector is running on {MEM.name} right now ({pid_in(collect_lock())}). Nothing was released. "
                         "Let it finish, or stop that process (`kill` and its number): it then takes its unfinished batch away, and there is nothing left to release.")
    with store_locked():
        if not folder.is_dir():
            return f"No dream holds {MEM.name}: there is no batch. Nothing was released."
        try:
            found = read(folder)
        except SystemExit:
            found = None  # this is the way out its message names
            whose = "a dream whose owner could not be read"
        else:
            whose = f"{who(found)}, which collected on {when(found['since'])} and was last heard from on {when(heard(found))}" if found else "a batch with no owner"
        sessions = slim.sessions_of([p.name for p in folder.glob("*.md")])
        notes = len([p for p in (folder / "_auto-memory").rglob("*.md")]) if (folder / "_auto-memory").is_dir() else 0
        unfinished = drop_draft(folder)
        shutil.rmtree(folder)
    return (f"Released {MEM.name} from {whose}. Discarded: its batch of {sessions} {'session' if sessions == 1 else 'sessions'} and {notes} of Claude's own notes, "
            + (f"its unfinished proposal {unfinished}, " if unfinished else "") + "and whatever its analysts had found. Nothing in memory changed, no note was marked read, and the next dream reads the same sessions.")


def when(at: float) -> str:
    return datetime.datetime.fromtimestamp(at).strftime("%Y-%m-%d %H:%M")


def main():
    args = sys.argv[1:]
    if args == ["alive"]:
        if not renew():
            sys.exit(f"The batch of {MEM.name} is not this dream's, so there is nothing of its own to keep alive. Stop here.")
        print("noted: this dream is still at work")
    elif args == ["release"]:
        print(release())
    elif not args:
        import dream_due  # the words are there; dream_due.py loads this file
        kept = holder()
        print(dream_due.running_words(kept) if kept else f"No dream is running on {MEM.name}.")
        sys.exit(HELD if kept else 0)
    else:
        sys.exit("usage: dream_owner.py [alive | release]")


if __name__ == "__main__":
    main()

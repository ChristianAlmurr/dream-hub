#!/usr/bin/env python3
"""What state every store is in, read from files and from nothing else.

  python3 scripts/status.py                    one line per store
  python3 scripts/status.py <store> [...]      those stores in full
  python3 scripts/status.py --json [store ...] the same as JSON, for programs (the keys are in docs/one-kit-design.md)
  python3 scripts/status.py --text ...         with the text of the items a proposal carries, which is memory content

What it prints may be logged, pasted or piped anywhere, so by default it holds no memory content: an
item that is carried is named by its proposal and its number, and its words (`item`) are left out.
--text puts them in, for the one reader that shows them to the person.
  AGENT_MEMORY_DIR=<store> python3 scripts/status.py --one    one store's record as JSON

It only reads. It waits for no lock, brings no archive up to date, writes no file and starts
no launchctl. The one thing it does to a lock is dream_owner.holder()'s: to tell whether an
unattended run or a collector is alive, that asks for the run lock and the collect lock, shared
and without waiting, and lets go at once. What it asks git, per store, is each listed repository's `agent-memory.store` key
(store.linked); the kit's own commit is asked once, and git is told to take no lock of its own
for any of it (GIT_OPTIONAL_LOCKS=0). A file in project/ that changed outside memwrite costs two
more git questions, once, which is how memcheck.drift() tells a rollback apart, and a store from
before proposals had a record costs two (dream_proposal.before_ledger) until its first is written.

Nothing here decides a rule. The state file is read and judged by dream_due.load() and trusted(),
the back-off by dream_due.waiting(), a dream that is alive by dream_owner.holder(), the proposals and
their states by dream_proposal.py, what a start loads by
memory_context.parts() and wanted(), integrity by memcheck.drift(), entries by build_index.entries(),
Claude's own notes by own_notes.py, a store's repositories by store.py. Every one of those serves
the store it was started for (AGENT_MEMORY_DIR), so each store's record is made by a process of
its own (--one), all of them at once, and all of them within LIMIT seconds.

Problems are part of the answer, not a failure. A record is always whole: a field that can't be
read is null and `problems` says which and why, in the kit's own words where the kit refused it.
The command exits 0 whenever it could report, 1 with one line when it could not (a store that
was asked for doesn't exist, the clock can't be read), and 2 for arguments that make no sense.

What costs more than reading a few files is not looked at, and each record says so under
`not_checked`. A later step has the scheduler write those answers to a health file.
"""
import argparse, concurrent.futures, datetime, json, os, plistlib, re, subprocess, sys, time
from pathlib import Path
from xml.parsers.expat import ExpatError

import build_index
import dream_due
import dream_owner
import dream_proposal
import interpreter
import memcheck
import memory_context
import own_notes
import store

KIT = Path(__file__).resolve().parent.parent
MEM = dream_due.MEM
KEYS = ("store", "path", "repos", "state", "check", "last_dream", "dream", "proposal", "proposals", "tries", "reminders", "schedule",
        "loads", "entries", "problems", "not_checked")
NOT_CHECKED = ("uncommitted and untracked files in the store (amk check)",
               "memory lines that name files no repository has")
LIMIT = 10  # seconds for all the stores together; a store whose record is not made by then is stopped and says so
TICK = re.compile(r"^- \[([ xX])\] (\d+\. )?")
STARTS = (("session", {"hook_event_name": "SessionStart"}),
          ("subagent", {"hook_event_name": "SubagentStart", "agent_type": "general-purpose"}))

# git may rewrite a repository's index while it only answers a question. Not for a reader: nothing this starts takes a lock or writes.
os.environ["GIT_OPTIONAL_LOCKS"] = "0"


def field(problems: list, name: str, read):
    """One field of a record. What stops its reading costs that field alone: it is None, and `problems` names it and says why."""
    try:
        return read()
    except SystemExit as stop:  # how the kit's own readers refuse a file, with the reason in words
        why = str(stop)
    except (OSError, ValueError) as stop:
        why = f"{type(stop).__name__}: {stop}"
    problems.append(f"{name} could not be read: {why}")
    return None


def schedule(name: str, problems: list):
    """When the store's daily check runs, or None when it has no job or its job can't be used (then `problems` says so).

    Today that is the store's own launchd job. The one scheduler of a later step replaces this function.
    A job from before the daily check runs on one weekday, and reads that way until it is set up again.
    """
    plist = Path.home() / "Library" / "LaunchAgents" / f"ai.agent-memory.dream.{name}.plist"
    try:
        with open(plist, "rb") as handle:
            job = plistlib.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, ValueError, ExpatError) as stop:
        problems.append(f"{plist} can't be read as a launchd job ({type(stop).__name__}), so there is no telling when the daily check runs")
        return None
    when = job.get("StartCalendarInterval") if isinstance(job, dict) else None
    whole = lambda value: type(value) is int
    if not (isinstance(when, dict) and whole(when.get("Hour")) and 0 <= when["Hour"] <= 23
            and ("Weekday" not in when or whole(when["Weekday"])) and isinstance(job.get("WorkingDirectory"), str)):
        problems.append(f"{plist} doesn't say one time of day and a folder, so there is no telling when the daily check runs")
        return None
    return {"every": "week" if "Weekday" in when else "day", "hour": when["Hour"], "weekday": when.get("Weekday"),
            "cwd": job["WorkingDirectory"]}


def dreaming() -> dict:
    """Is a dream alive on this store, whose is it, since when, and when was it last heard from?

    dream_owner.holder() is the kit's one rule for it. `heard` is None for a dream that holds a lock
    (an unattended run, a collector at work): it is alive for exactly as long as it holds it.
    """
    batch = MEM / "dreams" / "_batch"
    kept = dream_owner.holder(batch) or {"who": None, "since": None, "heard": None}
    return {"running": kept["who"] is not None, "who": kept["who"], "since": kept["since"], "heard": kept["heard"],
            "batch_files": len(list(batch.glob("[!_]*.md")))}


def proposal(ledger: dict, at: float):
    """The proposal written last, by the store's own record of them (dream_proposal.load): its file, the day it was written,
    how many days ago that is, its state (open, applied or dismissed), and how many of its items are ticked. None when there is none.

    The record lists proposals oldest first, so the last is the one written last (dream_proposal.newest reads it the same way).
    The counts are None when the file is no longer there: a proposal that was dealt with may be removed.
    """
    if not ledger["proposals"]:
        return None
    found = ledger["proposals"][-1]
    date = datetime.date.fromtimestamp(found["written"])
    path = MEM / "dreams" / found["name"]
    ticks = [m for m in map(TICK.match, path.read_text(errors="ignore").splitlines()) if m] if path.is_file() else None
    count = lambda wanted: None if ticks is None else sum(1 for m in ticks if wanted(m))
    return {"file": found["name"], "date": date.isoformat(), "age_days": max(0, (datetime.date.fromtimestamp(at) - date).days),
            "items": count(lambda m: True), "numbered": count(lambda m: m[2]), "ticked": count(lambda m: m[1] != " "),
            "state": found["state"]}


def carried(left: list, text: bool) -> dict:
    """What closed proposals left behind, split the way the record says to split it: by `waiting`, and by nothing else.

    `approved`: the person approved it and it is not done; `waiting` says who has still to do it ("you" or
    "a session"), and `stuck` why nothing can do it any more, in the kit's words (dream_proposal.stuck),
    or None when something can. `another_look`: not waiting and passed over fewer than DECLINED times;
    it goes to the next dream once. `declined`: passed over that often and still held against a proposal
    (dream_proposal.declined). `ticked` only says the item was ticked in its proposal. An entry is named
    by `from` and `id`; `number` can be null. The one place that reads these rules.
    `item` is the item's own words, which are memory content: None unless `text` asks for them.
    """
    declined = {(e["from"], e["id"]) for e in dream_proposal.declined(MEM).values()} if left else set()
    brief = lambda e: {"item": e["item"] if text else None, "from": e["from"], "id": e["id"], "number": e["number"], "left": e["left"], "ticked": e["ticked"],
                       "waiting": e["waiting"], "at": e["at"]}
    return {"approved": [dict(brief(e), stuck=dream_proposal.stuck(MEM, e) or None) for e in sorted(left, key=lambda e: e["at"]) if e["waiting"]],
            "another_look": [brief(e) for e in left if not e["waiting"] and e["left"] < dream_proposal.DECLINED],
            "declined": [brief(e) for e in left if not e["waiting"] and (e["from"], e["id"]) in declined]}


def proposals(ledger: dict, text: bool) -> dict:
    """The store's whole record of proposals (dream_proposal.load): the open one, every one with its state, and what is carried."""
    return {"open": next((p["name"] for p in ledger["proposals"] if p["state"] == "open"), None),
            "all": [{"file": p["name"], "state": p["state"], "written": p["written"], "closed": p["closed"]} for p in ledger["proposals"]],
            "carried": carried(ledger["left"], text)}


def next_try(state: dict, at: float) -> str:
    """The first day on which the kit's own back-off rule (dream_due.waiting) no longer holds the daily check back."""
    day = datetime.date.fromtimestamp(at)
    while dream_due.waiting(state, datetime.datetime.combine(day, datetime.time(12)).timestamp()):
        day += datetime.timedelta(days=1)
    return day.isoformat()


def starts(repos: list, broken: list):
    """What each start is given, as the hook's own code builds it: {kind: [(note, ORG.md, Steering, INDEX.md) for each repository]}. None when the store has nothing to load.

    Steering is each repository's own, and so is the start note, which says what the person has not written there.
    `broken` is the store's drift, worked out once and handed to the code that builds the start note.
    """
    roots = [Path(repo) for repo in repos] or [MEM]  # a store with no repository loads no Steering
    found = {kind: [memory_context.parts(event, root, MEM, broken) for root in roots] for kind, event in STARTS}
    return None if any(one is None for every in found.values() for one in every) else {kind: [one[1:] for one in every] for kind, every in found.items()}


def loads(repos: list, broken: list):
    """What a session and a subagent load at their start, in characters before anything gives way, against the limit: the largest over the store's repositories. None when the store has nothing to load."""
    found = starts(repos, broken)
    return found and {"limit": memory_context.LIMIT, **{kind: max(memory_context.wanted(*one) for one in every) for kind, every in found.items()}}


def over(repos: list, broken: list) -> list:
    """What to say about each start that would load more than the limit: what gives way there."""
    found, said = starts(repos, broken) or {}, []
    for kind, every in found.items():
        most = max(memory_context.wanted(*one) for one in every)
        if most > memory_context.LIMIT:
            crowded = not all(memory_context.theirs_fit(*one[:3]) for one in every)  # the person's own text alone no longer fits
            said.append(f"a {kind} start would load {most:,} characters, over the limit of {memory_context.LIMIT:,}: "
                        + ("ORG.md and Steering alone no longer fit, so they are cut and the index is left out" if crowded
                           else "the oldest titles of the index are left out"))
    return said


def repo_record(repo: str, problems: list) -> dict:
    """One line of roots.txt: whether the folder exists, how it is tied to the store, and Claude Code's own notes there."""
    tie, wrong = store.linked(MEM, repo)
    problems += [wrong] if wrong else []
    notes = lambda: {"on": own_notes.enabled(repo), "notes": len(own_notes.notes(repo)), "folder": str(own_notes.folder(repo))}
    return {"path": repo, "exists": Path(repo).is_dir(), "linked": tie, "own_notes": field(problems, f"Claude's own notes for {repo}", notes)}


def shipped(org, repos: list) -> list:
    """What is still as the kit shipped it: ORG.md, and each repository's Steering."""
    found = []
    if org is not None and not memory_context.own(org, memory_context.ORG_SHIPPED):
        found.append("ORG.md is still the kit's template: write this project's principles in it")
    for repo in repos:
        if memory_context.block(Path(repo), MEM) is None:
            if Path(repo).is_dir():
                found.append(f"{repo} has no Steering block in .claude/commands/dream.md")
        elif not memory_context.steering(Path(repo), MEM):
            found.append(f"{memory_context.STEERING} says nothing yet for {repo}: say what matters in this project" if (MEM / memory_context.STEERING).exists()
                         else f"Steering in {repo} is still the kit's default: say what matters in this project")
    return found


def record(text: bool = False) -> dict:
    """Everything about the store this process serves. Always whole: a field that can't be read is None and is said under problems."""
    at = dream_due.now()
    problems = []
    out = dict.fromkeys(KEYS)
    out.update(store=MEM.name, path=str(MEM), not_checked=list(NOT_CHECKED), problems=problems)
    repos = field(problems, "roots.txt", lambda: store.repos(MEM)) or []
    path = dream_due.state_file()
    out["state"] = {"file": str(path), "status": "ok" if path.exists() else "none", "why": None}
    try:
        state = dream_due.trusted(dream_due.load(), at)
    except SystemExit as stop:  # how load() and trusted() refuse a state file, with the reason in words
        out["state"].update(status="refused", why=str(stop))
        problems.append(str(stop))
    else:
        tries = state["tries"]
        hold = dream_due.waiting(state, at)
        out["check"] = {"at": state["checked"], "verdict": state["verdict"]}
        out["last_dream"] = {"at": state["last_dream"], "reason": state["last_reason"]}
        out["reminders"] = {"proposal": state["reminded"], "waiting_said": state["waiting_said"]}
        out["tries"] = {"count": len(tries), "last": {"at": tries[-1]["at"], "ended": tries[-1]["ended"]} if tries else None,
                        "waiting": hold or None, "next_try": next_try(state, at) if hold else None}
    out["dream"] = field(problems, "who is dreaming", dreaming)
    ledger = field(problems, "the record of proposals", lambda: dream_proposal.load(MEM))  # read once, and a refusal said once
    if ledger is not None:
        out["proposals"] = field(problems, "what the record of proposals carries", lambda: proposals(ledger, text))
        out["proposal"] = field(problems, "the newest proposal", lambda: proposal(ledger, at))
    out["schedule"] = schedule(MEM.name, problems)
    out["repos"] = [repo_record(repo, problems) for repo in repos]
    try:
        org = (MEM / "ORG.md").read_text()
    except OSError as stop:
        org = None
        problems.append(f"ORG.md can't be read ({type(stop).__name__}), so no session loads any memory")
    problems += shipped(org, repos)
    problems += memory_context.trouble(MEM) + ([f"{memory_context.STEERING} is in the store's history and gone from its folder: "
                                                 "every repo is back on its dream.md block (amk check says how to put it right)"] if memcheck.steering_gone(MEM) else [])
    broken = field(problems, "project/, for what changed outside memwrite,", lambda: memcheck.drift(MEM))
    problems += [f"integrity: {line}" for line in broken or []]
    said = len(problems)
    out["loads"] = field(problems, "what a start loads", lambda: loads(repos, broken or []))
    if out["loads"] is None and org is not None and len(problems) == said:  # ORG.md is there, so it is the index that is not
        problems.append("INDEX.md can't be read, so no session loads any memory")
    elif out["loads"]:
        problems += over(repos, broken or [])
    out["entries"] = field(problems, "the entries in project/", entries)
    return out


def entries() -> dict:
    counts = {f"project/{p.name}": len(build_index.entries(p)) for p in sorted((MEM / "project").glob("*.md"))}
    return {"total": sum(counts.values()), "files": counts}


def unknown(path: Path, why: str) -> dict:
    """The record of a store nothing could be read for."""
    return dict(dict.fromkeys(KEYS), store=path.name, path=str(path), problems=[why], not_checked=list(NOT_CHECKED))


def kit() -> dict:
    """The kit that answered: where it is, its commit, and whether its files differ from that commit.

    The commit and `dirty` are None when the kit's folder is not the top of a git checkout of its own
    (a plain copy has no commit, and inside someone else's repository the commit would be theirs) or git
    could not be asked; `why` then says which.
    """
    kept = interpreter.recorded()
    python = {"python": interpreter.running(), "python_version": ".".join(map(str, sys.version_info[:3])), "python_recorded": kept,
              "problems": [f"the Python on record for amk, {kept}, {interpreter.unusable(kept)}, so amk goes on with another: "
                           "`amk interpreter record` records one again"] if kept and interpreter.unusable(kept) else []}
    unknown = lambda why: {"path": str(KIT), "commit": None, "dirty": None, "why": why, **python}
    try:
        asked = store.git(KIT, "rev-parse", "--show-toplevel", "--short", "HEAD")
        top, _, commit = asked.stdout.strip().partition("\n")
        if asked.returncode != 0 or not commit or not store.same(top, KIT):
            return unknown("the kit's folder is not a git checkout of its own")
        changed = store.git(KIT, "status", "--porcelain")
    except store.Problem as stop:
        return unknown(str(stop))
    if changed.returncode != 0:
        return unknown(f"git can't say whether the kit's files are as committed: {store.said(changed)}")
    return {"path": str(KIT), "commit": commit, "dirty": bool(changed.stdout.strip()), "why": None, **python}


def start(paths: list, text: bool) -> list:
    """A process per store, each to make that store's record. All are started before any is waited for."""
    return [(path, subprocess.Popen([sys.executable, __file__, "--one"] + ["--text"] * text, env={**os.environ, store.ENV: str(path)},
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)) for path in paths]


def made(path: Path, process, until: float) -> dict:
    """The record one process made. One that made none, or was not done by `until`, still gets a record, which says so."""
    try:
        said, errors = process.communicate(timeout=max(until - time.monotonic(), 0))
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        return unknown(path, f"the status of this store was not worked out within {LIMIT} seconds, so it was stopped: something it reads never ends")
    try:
        found = json.loads(said) if process.returncode == 0 else None
    except ValueError:
        found = None
    last = (errors.strip().splitlines() or [f"it exited with {process.returncode} and said nothing"])[-1]
    return found if isinstance(found, dict) else unknown(path, f"the status of this store could not be worked out: {last}")


def collect(started: list, until: float) -> tuple:
    """(what kit() says, every process's record in the order the stores were given).

    All of it is waited for together, so a store that never ends takes no time from the others, and
    the questions about the kit, which go to git, take none from the stores.
    """
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(started) + 1) as pool:
        about = pool.submit(kit)
        records = list(pool.map(lambda one: made(*one, until), started))
        return about.result(), records


def moment(at) -> str:
    """12 Nov 07:56, or `never`."""
    return "never" if at is None else f"{dream_due.day(at)} {datetime.datetime.fromtimestamp(at):%H:%M}"


def hour(found) -> str:
    if found is None:
        return "no daily check"
    if found["every"] == "day":
        return f"checks daily at {found['hour']}:00"
    days = "Sun Mon Tue Wed Thu Fri Sat Sun".split()
    day = days[found["weekday"]] if 0 <= found["weekday"] <= 7 else f"weekday {found['weekday']}"
    return f"dreams weekly, {day} at {found['hour']}:00 (set up before the daily check)"


def headline(r: dict) -> str:
    """The one thing to know about a store's dreaming right now."""
    if r["state"] is None:
        return "not read"
    if r["state"]["status"] == "refused":
        return "state file refused"
    if r["dream"] and r["dream"]["running"]:
        return "a dream is running" + (f" ({r['dream']['who']})" if r["dream"]["who"] else "")
    if r["tries"]["waiting"]:
        return f"waiting: {r['tries']['waiting']}"
    return r["check"]["verdict"] or "never checked"


def proposed(found) -> str:
    if found is None:
        return "no proposal"
    ticked = f"{found['ticked']} of {found['items']} ticked" if found["items"] is not None else "its file is gone"
    return f"proposal {found['date']} ({found['state']}, {dream_due.plural(found['age_days'], 'day')} old, {ticked})"


def line(r: dict, width: int) -> str:
    parts = [headline(r)]
    if r["state"] and r["state"]["status"] != "refused":
        parts.append(f"last dream {dream_due.day(r['last_dream']['at']) if r['last_dream']['at'] is not None else 'not recorded'}")
    if r["state"]:
        parts += [proposed(r["proposal"]), hour(r["schedule"])]
    parts.append(dream_due.plural(len(r["problems"]), "problem") if r["problems"] else "no problems")
    return f"{r['store']:<{width}}  " + " · ".join(parts)


def detail(r: dict) -> str:
    out = [f"{r['store']}  ({r['path']})"]
    row = lambda name, value: out.append(f"  {name + ':':<13}{value}")
    if r["state"] is None:
        return "\n".join(out + [f"  problem:     {why}" for why in r["problems"]])
    for repo in r["repos"]:
        notes = repo["own_notes"]
        row("repo", repo["path"] + ("" if repo["exists"] else " (no longer exists)") + f" · tied by {repo['linked']}"
            + (f" · Claude's own notes {'on' if notes['on'] else 'off'}, {notes['notes']} kept" if notes else ""))
    if not r["repos"]:
        row("repo", "none")
    if r["state"]["status"] == "refused":
        row("state", f"refused ({r['state']['file']})")
    else:
        check, last, tries = r["check"], r["last_dream"], r["tries"]
        row("checked", f"{moment(check['at'])}" + (f" · {check['verdict']}" if check["verdict"] else ""))
        row("last dream", (f"{moment(last['at'])}" + (f" · {last['reason']}" if last["reason"] else "")) if last["at"] is not None
            else "not recorded yet (the first check takes it from the store's history)")
        if tries["last"]:
            ended = dream_due.ENDINGS[tries["last"]["ended"]]
            row("tries", f"{tries['count']} on record · the last, on {moment(tries['last']['at'])}, {ended}"
                + (f" · next try from {tries['next_try']}" if tries["next_try"] else ""))
        else:
            row("tries", "none on record")
    dream = r["dream"]
    if dream:
        row("dreaming", ("yes" + (f", {dream['who']}" if dream["who"] else "") if dream["running"] else "no")
            + (f" · {dream_due.plural(dream['batch_files'], 'file')} in the batch" if dream["batch_files"] else ""))
    row("proposal", proposed(r["proposal"]) + (f" · {r['proposal']['file']}" if r["proposal"] else ""))
    if r["proposals"]:
        kept = r["proposals"]
        row("carried", f"{len(kept['carried']['approved'])} approved and not done · {len(kept['carried']['another_look'])} for the next dream to look at again"
            f" · {len(kept['carried']['declined'])} declined · {len(kept['all'])} proposals on record")
        for entry in kept["carried"]["approved"]:
            named = entry["item"] or ("item " + str(entry["number"]) if entry["number"] is not None else "an item with no number")
            row("waits for", f"{entry['waiting']}: {named} (from {entry['from']})"
                + (f" · nothing can do it any more: {entry['stuck']}" if entry["stuck"] else ""))
    row("schedule", hour(r["schedule"]))
    if r["loads"]:
        row("loads", " · ".join(f"a {kind} {r['loads'][kind]:,}" for kind, _ in STARTS) + f" of {r['loads']['limit']:,} characters")
    if r["entries"]:
        row("entries", f"{r['entries']['total']} in project/" + "".join(f" · {Path(name).name} {n}" for name, n in r["entries"]["files"].items()))
    for why in r["problems"]:
        row("problem", why)
    if not r["problems"]:
        row("problems", "none")
    row("not checked", "; ".join(r["not_checked"]))
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description="The state of every store, read from files only.")
    ap.add_argument("stores", nargs="*", help="store names; with none, every store")
    ap.add_argument("--json", action="store_true", help="print JSON")
    ap.add_argument("--one", action="store_true", help="the record of the store AGENT_MEMORY_DIR names, as JSON")
    ap.add_argument("--text", action="store_true", help="include the words of the items a proposal carries: memory content, left out otherwise")
    a = ap.parse_args()
    store.settle()  # memcheck.py and dream_proposal.py start git with the environment as it is
    if a.one:
        if not os.environ.get(store.ENV):
            ap.error(f"--one makes the record of the store {store.ENV} names, and it is not set")
        print(json.dumps(record(a.text)))
        return
    try:
        paths = [store.named(name) for name in a.stores] or store.stores()
    except store.Problem as stop:
        sys.exit(str(stop))
    at = dream_due.now()  # before anything is started: a clock that can't be read stops it here
    until = time.monotonic() + LIMIT
    about, records = collect(start(paths, a.text), until)
    answer = {"at": at, "kit": about, "stores": records}
    if a.json:
        print(json.dumps(answer, indent=1))
    elif not answer["stores"]:
        print(f"No stores in {store.home()}.")
    elif a.stores:
        print("\n\n".join(detail(r) for r in answer["stores"]))
    else:
        width = max(len(r["store"]) for r in answer["stores"])
        print("\n".join(line(r, width) for r in answer["stores"]))
        dirty = {True: ", with changes that are not committed", False: "", None: ""}[answer["kit"]["dirty"]]
        print(f"kit {answer['kit']['commit'] or '(no commit: ' + answer['kit']['why'] + ')'} at {answer['kit']['path']}{dirty}"
              f" · Python {answer['kit']['python_version']}")
        print("".join(f"kit problem: {why}\n" for why in answer["kit"]["problems"]), end="")


if __name__ == "__main__":
    main()

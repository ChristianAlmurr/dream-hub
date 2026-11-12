"""Which hub the tests talk to, and the throwaway project they work in.

The tests make and remove projects, sessions and dreams, so they only ever run against a second instance: all six of
HUB_PORT, HUB_STATE, HUB_TMUX, HUB_CONFIG, HUB_KIT and HUB_JOBS, with HUB_JOBS a throwaway folder and never launchd's
own. Anything else stops here with one sentence. The rule for the six is the server's own (read_instance in
hub_base.py).
"""
import json, os, plistlib, re, shutil, subprocess, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import hub_base as base
import hub_data as hub
import hub_kit as kit

if not any(name in os.environ for name in base.INSTANCE):
    raise SystemExit(f"These tests make and remove projects, sessions and dreams, so they don't run against the installed hub. Start a second instance and set {', '.join(base.INSTANCE)} (see the README).")
# Paths are compared as the folders they really are, and nothing of the installed hub's may be this instance's.
if base.JOBS.resolve() == base.LAUNCHD_JOBS:
    raise SystemExit(f"HUB_JOBS is launchd's own folder, {base.LAUNCHD_JOBS}. The tests write dream jobs, so point it at a throwaway folder.")
for what, mine, installed in (("HUB_PORT", base.PORT, base.INSTALLED["port"]), ("HUB_STATE", base.STATE.resolve(), base.INSTALLED["state"]), ("HUB_TMUX", base.SOCKET_NAME, base.INSTALLED["socket"])):
    if mine == installed:
        raise SystemExit(f"{what} is the installed hub's own ({installed}). The tests start and stop sessions, so give the second instance its own.")

HOME = Path.home()
PORT, STATE, KIT, JOBS = base.PORT, base.STATE, base.KIT, base.JOBS
TMUX = [shutil.which("tmux", path=base.ENV["PATH"]) or "tmux", "-L", base.SOCKET_NAME]
CFG = json.loads(base.CONFIG.read_text())
HOST = CFG["origin"].split("//")[1]
OWNER = {"Host": HOST, "Tailscale-User-Login": CFG["login"]}
WRITE = {**OWNER, "Origin": CFG["origin"], "Content-Type": "application/json"}
# A test project says what it is in its name, and ends in the number of the test run that made it. It lives where the kit
# and the hub keep every project (~/agent-memory, ~/projects): the kit fixes a store's place, and Claude needs the real
# home folder to be signed in. So anything that walks those folders can tell these by name, and by the note in the store.
THROWAWAY = re.compile(r"^hub-test-throwaway-(\d+)$")
NAME = f"hub-test-throwaway-{os.getpid()}"  # the one project a test run makes, and removes
NOTE = "THROWAWAY-TEST-STORE.md"
KIT_STATE = HOME / ".local/state/agent-memory"
DREAMER = f"hub-test-{os.getpid()}"  # the name a dream this test runs itself is known by, as a session's dream is by its session


def launchd_jobs():
    """What a test could have put in launchd's own folder: a job named after a throwaway project. No test makes one, and
    each says so at its end. Other jobs there are other people's, and come and go."""
    return sorted(p.name for p in base.LAUNCHD_JOBS.iterdir() if "hub-test-throwaway" in p.name)


def in_repo(repo, script, *args, session=DREAMER):
    """Run one of the kit's scripts the way a dream in a session does: in the project's folder, from that folder's copy."""
    env = {key: value for key, value in base.ENV.items() if key not in kit.NOT_THE_HUBS}
    return subprocess.run(["python3", f"scripts/{script}", *args], cwd=repo, env={**env, "CLAUDE_CODE_SESSION_ID": session}, capture_output=True, text=True)


def collect(repo, session=DREAMER):
    """The first step of a dream, by the kit's own collector: from here on the dream holds the store's batch."""
    return in_repo(repo, "collect_transcripts.py", session=session)


def write_dream(repo, text, session=DREAMER):
    """A whole dream by the kit's own scripts, with the proposal's text supplied in place of the model's: the collector
    names the proposal, dream_finish.py checks it, commits it and records it as the store's open proposal. Returns its name."""
    ran = collect(repo, session)
    if ran.returncode != 0:
        raise SystemExit(f"The kit's collector stopped: {ran.stderr.strip() or ran.stdout.strip()}")
    dreams = (Path(repo) / "memory").resolve() / "dreams"
    name = json.loads((dreams / "_batch" / "_timing.json").read_text())["proposal"]
    (dreams / name).write_text(text.replace("{DAY}", name[:10]).replace("{SIDE}", name.replace("-proposal.md", "-notes.md")))
    if "{SIDE}" in text:  # what the dream read and did not propose, in the file it writes beside the proposal
        (dreams / name.replace("-proposal.md", "-notes.md")).write_text(SIDE)
    ran = in_repo(repo, "dream_finish.py", session=session)
    if ran.returncode != 0:
        raise SystemExit(f"The kit's dream_finish.py stopped: {ran.stderr.strip() or ran.stdout.strip()}")
    return name


# One change and nothing else: for a proposal that only has to exist, be ticked, applied or dismissed.
SMALL = """# Dream {DAY} — 3 sessions, 1 proposed changes

## Summary
A dream written for this test.

## Proposed changes (tick to approve)
- [ ] 1. project/gotchas.md — add — "{LINE}"
      why: test · seen: 3/3 sessions · cost: ~1 · evidence: none

## Trend
Nothing to compare.
"""


# A proposal in the shape the kit's dream writes (.claude/commands/dream.md, step 7): eight numbered items that run
# on across the sections, the first indented line of each starting with why:, and nine boxes with the scratchpad's.
PROPOSAL = """# Dream {DAY} — 4 sessions, 8 proposed changes

## Summary
A dream written for this test.
Not proposed ({SIDE}): 1 stale from Claude's own notes, 1 one-offs and injections

## Proposed changes (tick to approve)
- [ ] 1. project/gotchas.md — add — "- [{DAY}] The export hangs on empty input → pass --allow-empty (checked: ran it)"
      why: two sessions lost time to it · seen: 2/4 sessions · cost: ~6 · evidence: a1b2c3d4
- [ ] 2. project/gotchas.md — edit — "- [{DAY}] The importer needs Python 3.11 · 3.12 also works"
      why: said more strongly than the session shows · seen: 1/4 sessions · cost: ~2 · evidence: e5f6a7b8
      replaces: "- [2025-12-01] The importer needs Python 3.11 · nothing newer"
- [ ] 3. project/gotchas.md — merge — "- [{DAY}] Builds need the lock: run them under lockf (checked: both sessions)"
      why: 2 lines say the same thing · evidence: both name the lock
      replaces: - [2025-11-02] Builds collide ; - [2025-11-20] Run one build
- [ ] 4. project/gotchas.md — split — "project/builds.md, project/importer.md"
      why: 64 entries, over 60 · evidence: _health.md
      project/builds.md: - [2025-11-02] Builds collide ; - [2025-11-20] Run one build · and wait
      project/importer.md: - [2025-12-01] The importer needs
- [ ] 5. project/decisions.md — add — "Decision: releases go out on Tuesdays. Why: the team is in on Tuesdays. (by Sam)"
      why: the person decided it in one session · seen: 1/4 sessions · cost: ~0 · evidence: c9d0e1f2

## ORG.md and Steering (yours to change)
- [ ] 6. ORG.md — edit — "- Keys live in the keychain, never in a repo."
      why: the folder the line names is gone · evidence: ls ~/.keys
      replaces: "- Keys live in ~/.keys · never in a repo."
- [ ] 7. Steering — add — "- Matters here: the importer and its fixtures."
      why: three sessions rediscovered it · evidence: a1b2c3d4

## Claude's own notes (auto memory)
- [ ] 8. notes shop/feedback_importer.md — carry — 1 checked facts
      why: checked, useful to every agent, not in memory · evidence: shop/feedback_importer.md
      + project/gotchas.md "- [{DAY}] The importer reads dates as local time → run it with TZ=UTC (checked: importer.py:41)"
{REST}
## Trend
The first dream, so nothing to compare.
"""


# The file beside PROPOSAL: each note's counts, the fact that was stale, and what was turned away.
SIDE = """# Dream: read and not proposed
## Claude's own notes, file by file
- shop/feedback_importer.md — facts: 2 · checked: 1 · unchecked: 0 · already in memory: 0 · stale: 1 · rejected: 0
## Not carried over
- shop/feedback_importer.md — which port the server uses — stale: the server moved to another port
## Rejected (one-offs, injections)
- Seen once: a missing font.
"""


def job_of(name):
    """A store's dream job, in the folder this instance reads them from. Never launchd's own."""
    return JOBS / f"ai.agent-memory.dream.{name}.plist"


def write_job(name, repo, hour, weekday=None):
    """A dream job as the kit writes it: every day with --if-due, or, before that, on one weekday. A file in the instance's own folder."""
    when = {"Hour": hour, "Minute": 0} if weekday is None else {"Weekday": weekday, "Hour": hour, "Minute": 0}
    with open(job_of(name), "wb") as f:
        plistlib.dump({"Label": job_of(name).stem, "WorkingDirectory": str(repo), "StartCalendarInterval": when,
                       "ProgramArguments": ["/bin/sh", str(KIT / "run-dream.sh"), str(repo)] + (["--if-due"] if weekday is None else [])}, f)


def check_file(name):
    """Where the kit records what it last decided about dreaming for a store."""
    return KIT_STATE / f"{name}.json"


def own_notes(repo):
    """Claude Code's own notes for a folder: its path with every character that isn't a letter or digit made a dash."""
    return HOME / ".claude/projects" / re.sub(r"[^A-Za-z0-9]", "-", str(repo)) / "memory"


def set_own_notes(repo, on, file="settings.local.json"):
    """What the kit's retire-auto-memory.sh does to one folder, and its --undo: one key, the rest of the file kept.
    on is False (off), True (the key removed) or "on" (the key set to true, which outranks a file read after it)."""
    path = Path(repo) / ".claude" / file
    data = json.loads(path.read_text()) if path.exists() else {}
    data.pop("autoMemoryEnabled", None)
    if on is not True:
        data["autoMemoryEnabled"] = on == "on"
    path.write_text(json.dumps(data, indent=2) + "\n")


def quiet_alert(name):
    """Right after a throwaway project is made: its dreams must not reach a phone, so its alert script stays in place and
    sends nothing; and a note in the store says what this is, for a person or a script that finds it there."""
    store = HOME / "agent-memory" / name
    (store / "notify.sh").write_text("#!/bin/sh\n# Stand-in while testing: says nothing to anyone.\nexit 0\n")
    (store / NOTE).write_text(f"# A throwaway store made by Dream Hub's tests\n\nMade {time.strftime('%Y-%m-%d %H:%M')} by test run {os.getpid()} ({Path(sys.argv[0]).name}), with its folder ~/projects/{name}.\n"
                              "It holds nothing real. The run removes it when it ends; if that run is gone, `python3 tests/sweep.py` in the hub's\n"
                              "working copy removes it, with the instance's HUB_* variables set. Don't sync the kit into it or dream on it.\n")


# What Claude Code shows a session in a folder it hasn't been in before, and what it shows once it is ready.
TRUST_NO, TRUST_YES = "❯ No, exit", "❯ Yes, I trust this folder"  # the trust question, by where its cursor is
MCP_QUESTION = "Esc to reject all"  # the project's MCP servers, asked after the folder is trusted
INPUT_BOX = re.compile(r"^─{10,}\n❯[^\n]*\n─{10,}", re.M)  # Claude's own input: a ❯ line between two rules


def screen(sid):
    return subprocess.run(TMUX + ["capture-pane", "-p", "-t", f"hub-{sid}"], capture_output=True, text=True).stdout


def tell(sid, text, seconds=60):
    """Give a Claude session something to do, the way the hub itself does: as one bracketed paste, then Enter.

    Typed keys this long arrive as a paste and Claude keeps only the tail, so typing a prompt and pressing Enter
    after a fixed wait can send half of it. The text goes in whole through a tmux buffer; Enter is pressed once it
    is seen in Claude's input, and again only while it is still seen there well after.
    """
    target, buffer = f"hub-{sid}", f"hub-test-{sid}"
    if hub.typed_now(target):  # it would be sent glued to this (a suggestion in grey isn't typed)
        raise SystemExit(f"Session {sid} already has something typed in Claude's input, so nothing was added to it. Its screen:\n{screen(sid)}")
    subprocess.run(TMUX + ["set-buffer", "-b", buffer, text], check=True)
    subprocess.run(TMUX + ["paste-buffer", "-p", "-d", "-b", buffer, "-t", target], check=True)
    end, pressed, seen = time.time() + seconds, 0.0, False
    while time.time() < end:
        typed = hub.typed_now(target)
        if typed:
            seen = True
            if time.time() - pressed >= 3:
                subprocess.run(TMUX + ["send-keys", "-t", target, "Enter"])
                pressed = time.time()
        elif seen:
            return
        time.sleep(0.25)
    raise SystemExit(f"Session {sid} never took what it was told within {seconds} seconds. Its screen:\n{screen(sid)}")


def seen(sid, words, seconds=20):
    """Wait until words are on a session's screen. True when they are. A terminal breaks a line wherever its width
    says, in the middle of a word too, so the words are looked for with every space and line end taken out."""
    wanted, end = "".join(words.split()), time.time() + seconds
    while time.time() < end:
        if wanted in "".join(said_in(sid).split()):
            return True
        time.sleep(0.2)
    return False


def said_in(sid):
    """Everything a session's terminal has shown, not only what is on its screen now: a pane that is made smaller,
    as one is when a page opens it, pushes its first lines out of sight."""
    return subprocess.run(TMUX + ["capture-pane", "-p", "-J", "-S", "-500", "-t", f"hub-{sid}"], capture_output=True, text=True).stdout


def answer_first_run(sid, seconds=90):
    """Trusts the folder and declines the project's MCP servers, as a person would, then waits for Claude's input.

    Every key is decided by what is on screen, never by a wait. The trust question starts on "No, exit", draws
    before it takes keys (a Down in its first third of a second is lost) and wraps, so a blind Down then Enter can
    answer No and end the session. So: Down only while the cursor is seen on No, Enter only once it has been seen
    on Yes twice in a row, Escape only while the MCP question is seen. A key is sent again only if the screen
    still asks for it well after it was sent. A folder already trusted shows neither question, and nothing is sent.
    """
    sent = {"Down": 0.0, "Enter": 0.0, "Escape": 0.0}
    # A key is sent again only if the screen still asks for it this many seconds after it was sent. Measured on this
    # Mac: the trust question drops keys for up to 0.3 s after it is drawn and redraws within 0.2 s of a key it took,
    # so one second tells a dropped Down from a slow one. Enter and Escape change the screen for good, and a second
    # one would land in whatever comes next, so they wait three: no redraw seen in the tests took a tenth of that.
    again = {"Down": 1.0, "Enter": 3.0, "Escape": 3.0}

    def press(key):
        if time.time() - sent[key] >= again[key]:
            subprocess.run(TMUX + ["send-keys", "-t", f"hub-{sid}", key])
            sent[key] = time.time()

    end, text, steady = time.time() + seconds, "", 0
    while time.time() < end:
        text = screen(sid)
        steady = steady + 1 if TRUST_YES in text else 0
        if TRUST_NO in text:
            press("Down")
        elif TRUST_YES in text:
            if steady >= 2 and time.time() - sent["Down"] >= 0.5:
                press("Enter")
        elif MCP_QUESTION in text:
            press("Escape")
        elif INPUT_BOX.search(text):
            return
        time.sleep(0.25)
    raise SystemExit(f"Session {sid} never reached Claude's input within {seconds} seconds. Its screen:\n{text}")


def sessions_of(name):
    """This hub's sessions in a project, read from its state folder, so it works when the server doesn't answer."""
    found = []
    for path in (STATE / "sessions").glob("*.json"):
        try:
            if json.loads(path.read_text()).get("project") == name:
                found.append(path.stem)
        except ValueError:
            pass  # not a session of this project, whatever it is
    return found


def traces(name):
    """Everything a throwaway project can leave on this Mac."""
    repo = HOME / "projects" / name
    slug = own_notes(repo).parent
    claude = [slug, *slug.parent.glob(f"{slug.name}--claude-worktrees-*")]
    # The kit's state file, locks, logs and markers for the store all carry its name, and nothing else's does.
    named = re.compile(rf"(?<![0-9a-z]){re.escape(name)}(?![0-9])")  # dream-<name>.log, .<name>.json.lock, <name>.run.lock …
    kit = [p for p in KIT_STATE.iterdir() if named.search(p.name)] if KIT_STATE.is_dir() else []
    kit.append(KIT_STATE / "sessions" / name)
    mine = [STATE / folder / f"{sid}{ext}" for sid in sessions_of(name) for folder, ext in (("events", ".json"), ("uploads", ""), ("records", ""), ("sessions", ".json"))]
    mine += [STATE / "projects" / f"{name}{ext}" for ext in (".json", ".png", ".jpg", ".webp")]
    return [p for p in (repo, HOME / "agent-memory" / name, job_of(name), *kit, *claude, *mine) if p.exists() or p.is_symlink()]


def remove_project(name):
    """Removes a throwaway project and all it left behind, without asking the server for anything, and returns
    whatever is still there. Only ever a hub-test-throwaway-<n> name."""
    if not THROWAWAY.match(name):
        raise SystemExit(f"{name} isn't a throwaway project, so it stays")
    for target in [f"hub-{sid}" for sid in sessions_of(name)] + [f"hub-dream-{name}"]:
        subprocess.run(TMUX + ["kill-session", "-t", target], capture_output=True)
    subprocess.run(["git", "-C", str(HOME / "projects" / name), "worktree", "prune"], capture_output=True)
    subprocess.run(["chflags", "-R", "nouchg", str(HOME / "agent-memory" / name)], capture_output=True)  # ORG.md is locked against every other route
    for path in traces(name):
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink()
    return [str(p) for p in traces(name)]


def running(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def leftovers():
    """Throwaway projects whose test run is no longer running: every hub-test-throwaway-<n> named anywhere a run leaves things."""
    places = [HOME / "projects", HOME / "agent-memory", JOBS, KIT_STATE, KIT_STATE / "sessions", HOME / ".claude/projects"]
    names = set()
    for place in places:
        for path in place.iterdir() if place.is_dir() else []:
            names.update(re.findall(r"hub-test-throwaway-\d+", path.name))
    return sorted(n for n in names if THROWAWAY.match(n) and n != NAME and not running(int(THROWAWAY.match(n)[1])))


def sweep():
    """Run first by every test script, and by tests/sweep.py: clears what test runs that are no longer running left behind, and says so."""
    for name in leftovers():
        left = remove_project(name)
        print(f"swept {name}" + (f", still there: {left}" if left else ""), flush=True)

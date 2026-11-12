"""Is there a newer Dream Hub than the one running? Asked of the clone the hub runs from, never of anything else.

At most once a day, at start and when the person asks, a background thread asks the clone's own `origin` for its default
branch with `git ls-remote` and, only when that differs from HEAD, fetches that one branch to count the new commits and
read their subjects. Nothing about the person or their projects is sent: git asks the remote for refs and objects, and
that is all. The answer is kept in the hub's state folder (update.json) and read by the page; a request never waits for
git. Nothing is installed by itself: the person presses Update, or runs update.sh, which refuse an unsafe clone.

Off, and quiet, when the config says "updates": false, when the hub doesn't run from a git clone, when the clone has no
origin, or when the remote can't be reached. A clone with local changes, on another branch, detached, or with commits
of its own that the remote lacks is told to update by hand.
"""
import json, os, re, subprocess, threading, time
from pathlib import Path

from hub_base import APP, ENV, SETTINGS, STATE, Refused

ROOT = APP.parent          # the clone: hub/ and agent-memory-kit/ sit in it
FILE = STATE / "update.json"
LOG = STATE / "update.log"
EVERY = 24 * 3600          # seconds between two checks the hub starts by itself
TIMEOUT = 20               # seconds for one git call that talks to the remote
SHOWN = 20                 # commit subjects kept for the notice
_lock = threading.Lock()
_busy = False


def enabled() -> bool:
    return SETTINGS.get("updates", True) is not False


def git(*args, timeout=10):
    """git in the clone, with nothing from the caller's environment that could point it elsewhere, and no prompt: a
    remote that wants a password fails at once rather than waiting for one."""
    env = {k: v for k, v in ENV.items() if not k.startswith("GIT_")}
    env.update(GIT_TERMINAL_PROMPT="0", GIT_ASKPASS="/usr/bin/false", SSH_ASKPASS="/usr/bin/false",
               GIT_SSH_COMMAND="ssh -o BatchMode=yes -o ConnectTimeout=10")
    try:
        return subprocess.run(["git", "-C", str(ROOT), *args], env=env, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return subprocess.CompletedProcess(args, 124, "", "timed out")


def out(*args, timeout=10) -> str:
    done = git(*args, timeout=timeout)
    return done.stdout.strip() if done.returncode == 0 else ""


def version():
    """The commit this hub runs from and its date, or None outside a git clone."""
    if not (ROOT / ".git").exists():
        return None
    line = out("log", "-1", "--format=%h%x09%cs")
    return dict(zip(("commit", "date"), line.split("\t"))) if "\t" in line else None


def default_branch() -> str:
    named = out("symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    if named.startswith("origin/"):
        return named[len("origin/"):]
    listed = out("ls-remote", "--symref", "origin", "HEAD", timeout=TIMEOUT)
    match = re.search(r"^ref: refs/heads/(\S+)\tHEAD$", listed, re.M)
    return match[1] if match else "main"


def commits_page(url: str, branch: str):
    """The remote's page of commits, for a GitHub remote; None for any other."""
    match = re.match(r"^(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)([\w.-]+)/([\w.-]+?)(?:\.git)?/?$", url)
    return f"https://github.com/{match[1]}/{match[2]}/commits/{branch}" if match else None


def look() -> dict:
    """One check, start to end. Every outcome is a record with a `state`; only `available` shows a notice that can be acted on."""
    now = time.time()
    if not enabled():
        return {"state": "off", "at": now}
    if not (ROOT / ".git").exists():
        return {"state": "not a clone", "at": now}
    url = out("remote", "get-url", "origin")
    if not url:
        return {"state": "no origin", "at": now}
    branch = default_branch()
    head = out("rev-parse", "HEAD")
    listed = git("ls-remote", "origin", f"refs/heads/{branch}", timeout=TIMEOUT)
    remote = listed.stdout.split()[0] if listed.returncode == 0 and listed.stdout.split() else ""
    if not remote:
        return {"state": "offline", "at": now, "branch": branch}
    base = {"at": now, "branch": branch, "link": commits_page(url, branch), "head": head}
    if remote == head:
        return {**base, "state": "current"}
    if git("fetch", "--quiet", "--no-tags", "origin", branch, timeout=TIMEOUT * 3).returncode != 0:
        return {**base, "state": "offline"}
    if git("merge-base", "--is-ancestor", remote, "HEAD").returncode == 0:
        return {**base, "state": "current"}  # this copy is ahead of the remote: nothing to take
    if git("merge-base", "--is-ancestor", "HEAD", remote).returncode != 0:
        return {**base, "state": "by hand", "why": "This copy has commits of its own that the repository doesn't have, so it can't simply move forward. Update it by hand with git."}
    count = int(out("rev-list", "--count", f"HEAD..{remote}") or 0)
    rows = out("log", f"-{SHOWN}", "--format=%h%x09%cs%x09%s", f"HEAD..{remote}").splitlines()
    record = {**base, "state": "available", "behind": count, "newest": out("log", "-1", "--format=%cs", remote),
              "commits": [dict(zip(("commit", "date", "subject"), row.split("\t", 2))) for row in rows if row.count("\t") >= 2]}
    current = out("symbolic-ref", "--short", "-q", "HEAD")
    if not current:
        record["why"] = f"This copy isn't on a branch (a detached HEAD). Check out {branch}, then update."
    elif current != branch:
        record["why"] = f"This copy is on {current}, not {branch}. Switch to {branch} to update, or update by hand."
    elif out("status", "--porcelain", "--untracked-files=no"):
        record["why"] = "This copy has changes of its own that aren't committed. Commit or stash them, or update by hand."
    return record


def _run():
    global _busy
    try:
        record = look()
    except Exception as error:  # a check must never take the hub down; say it once in the log
        print(f"error: update check: {type(error).__name__}: {error}", flush=True)
        record = {"state": "offline", "at": time.time()}
    spare = FILE.with_suffix(".tmp")
    spare.write_text(json.dumps(record))
    os.replace(spare, FILE)
    with _lock:
        _busy = False


def check(force=False) -> bool:
    """Start a check in the background unless one runs, or (unless forced) one ran in the last day. True when started."""
    global _busy
    with _lock:
        if _busy or (not force and time.time() - (last().get("at") or 0) < EVERY):
            return False
        _busy = True
    threading.Thread(target=_run, daemon=True).start()
    return True


def last() -> dict:
    try:
        found = json.loads(FILE.read_text())
        return found if isinstance(found, dict) else {}
    except (OSError, ValueError):
        return {}


def status() -> dict:
    """What the page shows: the last check, whether one runs now, and the version this hub runs. Starts a daily check if due."""
    record = last() if enabled() else {"state": "off"}
    if record.get("head") and (ROOT / ".git").exists() and record["head"] != out("rev-parse", "HEAD"):
        check(force=True)  # the clone moved (update.sh ran): what was found before no longer holds
        record = {"state": "current", "at": record.get("at")}
    else:
        check()
    return {**record, "checking": _busy, "version": version()}


def check_now() -> dict:
    if not enabled():
        raise Refused('Update checks are off in this hub\'s config ("updates": false).')
    check(force=True)
    return {"checking": True}


def apply() -> dict:
    """Run update.sh from the clone, detached, so it survives the hub's own restart. It takes nothing from the request."""
    record = last()
    if record.get("state") != "available" or record.get("why"):
        raise Refused(record.get("why") or "There is no update to apply.")
    script = ROOT / "update.sh"
    if not script.is_file():
        raise Refused("This copy has no update.sh. Update it by hand with git.")
    with open(LOG, "w") as log:
        subprocess.Popen(["/bin/sh", str(script), "--from-hub"], cwd=str(ROOT), env={k: v for k, v in ENV.items() if not k.startswith("GIT_")},
                         stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    return {"started": True, "log": str(LOG).replace(str(Path.home()), "~", 1)}

#!/usr/bin/env python3
"""Offline checks of the update check (hub_update.py) and update.sh. No server, no Claude session, no network.

Everything happens in a temporary folder that is HOME for every command run here: a bare repository stands in for the
project's origin, a clone of it is the install, and a store in the temporary HOME has one repo attached. The hub's
check runs in a child process with all six HUB_* variables pointing into that folder, so nothing of a real hub is read.
Run: /usr/bin/python3 tests/update_check.py
"""
import json, os, shutil, subprocess, sys, tempfile
from pathlib import Path

RELEASE = Path(__file__).resolve().parents[2]
fails = 0


def check(label, ok, extra=""):
    global fails
    print(("PASS  " if ok else "FAIL  ") + label + ("" if ok else f"  [{extra}]"), flush=True)
    fails += 0 if ok else 1


place = Path(tempfile.mkdtemp(prefix="update-check."))
HOME = place / "home"
HOME.mkdir()
ENV = {"HOME": str(HOME), "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "AGENT_KIT_NO_SCHEDULE": "1", "LANG": "en_US.UTF-8",
       "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com", "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.com"}


def sh(*cmd, cwd=None, extra=None):
    return subprocess.run(list(map(str, cmd)), cwd=cwd, env={**ENV, **(extra or {})}, capture_output=True, text=True, timeout=300)


def git(where, *args):
    done = sh("git", "-C", where, *args)
    assert done.returncode == 0, f"git {' '.join(map(str, args))}: {done.stderr}"
    return done.stdout.strip()


def look(install, updates=True):
    """What the hub's check finds for this install, run as a second instance of the hub would run it."""
    config = place / "config.json"
    config.write_text(json.dumps({"login": "you@example.com", "origin": "http://127.0.0.1:1", **({} if updates else {"updates": False})}))
    instance = {"HUB_PORT": "1", "HUB_STATE": str(place / "state"), "HUB_TMUX": "update-check", "HUB_CONFIG": str(config),
                "HUB_KIT": str(install / "agent-memory-kit"), "HUB_JOBS": str(place / "jobs")}
    code = "import json, sys; sys.path.insert(0, sys.argv[1]); import hub_update as u; print(json.dumps(u.look()))"
    done = sh("/usr/bin/python3", "-c", code, install / "hub", extra=instance)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def update(install):
    return sh("/bin/sh", install / "update.sh", cwd=install)


try:
    # origin: a bare repository holding the release as it is now
    source, origin, install = place / "source", place / "origin.git", place / "install"
    shutil.copytree(RELEASE, source, ignore=shutil.ignore_patterns(".git", "__pycache__"))
    git(source, "init", "-q", "-b", "main"); git(source, "add", "-A"); git(source, "commit", "-qm", "release")
    sh("git", "clone", "-q", "--bare", source, origin)
    sh("git", "clone", "-q", origin, install)

    # a store with one repo, attached by the install's own kit
    repo = HOME / "projects" / "app"
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main"); (repo / "README.md").write_text("app\n"); git(repo, "add", "-A"); git(repo, "commit", "-qm", "init")
    done = sh("sh", install / "agent-memory-kit/install-store.sh", "app", repo)
    check("a store is attached by the install's kit", done.returncode == 0, done.stderr)
    git(repo, "add", "-A"); git(repo, "commit", "-qm", "the kit")

    found = look(install)
    check("a fresh clone is current", found["state"] == "current", found)

    # the author pushes two commits, one of them to the kit
    git(source, "remote", "add", "origin", origin)
    (source / "agent-memory-kit/scripts/needs.py").write_text((source / "agent-memory-kit/scripts/needs.py").read_text() + "\n# a change to the kit\n")
    git(source, "commit", "-qam", "Kit: a small change"); (source / "README.md").write_text((source / "README.md").read_text() + "\nMore.\n")
    git(source, "commit", "-qam", "README: one more line"); git(source, "push", "-q", "origin", "main")
    found = look(install)
    check("two new commits upstream: available, 2 changes, both subjects, newest first, nothing to do by hand",
          found["state"] == "available" and found["behind"] == 2 and [c["subject"] for c in found["commits"]] == ["README: one more line", "Kit: a small change"] and not found.get("why"), found)
    check("the check itself changes nothing in the clone", git(install, "rev-parse", "HEAD") != git(source, "rev-parse", "HEAD") and not git(install, "status", "--porcelain"))
    check("with \"updates\": false nothing is asked", look(install, updates=False)["state"] == "off")

    # a dirty clone, a detached clone: told why, and update.sh refuses with nothing changed
    head = git(install, "rev-parse", "HEAD")
    (install / "README.md").write_text("local edit\n")
    found, ran = look(install), update(install)
    check("a clone with uncommitted changes is told to commit, stash or update by hand", found["state"] == "available" and "aren't committed" in found.get("why", ""), found)
    check("update.sh refuses a dirty clone and changes nothing", ran.returncode == 1 and "uncommitted changes" in ran.stderr and git(install, "rev-parse", "HEAD") == head, ran.stderr)
    git(install, "checkout", "-q", "--", "README.md")
    git(install, "checkout", "-q", "--detach")
    found, ran = look(install), update(install)
    check("a detached clone is told to check out the branch", "detached" in found.get("why", ""), found)
    check("update.sh refuses a detached clone and changes nothing", ran.returncode == 1 and "detached HEAD" in ran.stderr and git(install, "rev-parse", "HEAD") == head, ran.stderr)
    git(install, "checkout", "-q", "main")

    # offline: a remote that can't be reached is quiet, and update.sh says nothing changed
    url = git(install, "remote", "get-url", "origin")
    git(install, "remote", "set-url", "origin", str(place / "nowhere.git"))
    found, ran = look(install), update(install)
    check("an unreachable remote is quiet (offline)", found["state"] == "offline", found)
    check("update.sh with no network changes nothing", ran.returncode == 1 and "couldn't reach origin" in ran.stderr and git(install, "rev-parse", "HEAD") == head, ran.stderr)
    git(install, "remote", "set-url", "origin", url)

    # the update itself
    ran = update(install)
    check("update.sh moves the clone forward to the remote", ran.returncode == 0 and git(install, "rev-parse", "HEAD") == git(source, "rev-parse", "HEAD"), ran.stdout + ran.stderr)
    check("it says what changed and copies the kit into the store's repo, which now has kit files to commit",
          "Kit: a small change" in ran.stdout and f"synced {repo}: its kit files changed" in ran.stdout and "# a change to the kit" in (repo / "scripts/needs.py").read_text(), ran.stdout)
    check("it commits nothing in the repo", git(repo, "log", "--oneline").count("\n") == 1)
    check("with no launchd job it says to restart the hub", "Restart the hub" in ran.stdout, ran.stdout)
    found, again = look(install), update(install)
    check("afterwards the check is current, and a second run says up to date", found["state"] == "current" and again.returncode == 0 and "up to date" in again.stdout, again.stdout)

    # diverged: a local commit and a new remote commit
    (install / "LOCAL.md").write_text("mine\n"); git(install, "add", "LOCAL.md"); git(install, "commit", "-qm", "a local commit")
    (source / "NEW.md").write_text("theirs\n"); git(source, "add", "NEW.md"); git(source, "commit", "-qm", "upstream"); git(source, "push", "-q", "origin", "main")
    mine = git(install, "rev-parse", "HEAD")
    found, ran = look(install), update(install)
    check("a clone with commits of its own is told to update by hand", found["state"] == "by hand", found)
    check("update.sh refuses diverged history and changes nothing", ran.returncode == 1 and "commits of its own" in ran.stderr and git(install, "rev-parse", "HEAD") == mine, ran.stderr)

    # no origin, not a clone
    git(install, "remote", "remove", "origin")
    check("a clone with no origin is quiet", look(install)["state"] == "no origin")
    shutil.rmtree(install / ".git")
    check("a copy that isn't a git clone is quiet", look(install)["state"] == "not a clone")
finally:
    subprocess.run(["chflags", "-R", "nouchg", str(place)], capture_output=True)
    shutil.rmtree(place, ignore_errors=True)
print(f"--- failures: {fails}")
sys.exit(1 if fails else 0)

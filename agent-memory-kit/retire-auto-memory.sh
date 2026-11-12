#!/bin/sh
# Turns off Claude Code's own auto memory in every repo of a store and every worktree of those repos.
#   sh agent-memory-kit/retire-auto-memory.sh <store-name>          turn it off
#   sh agent-memory-kit/retire-auto-memory.sh <store-name> --undo   turn it back on
# Run it after you have applied the dream that carried Claude's own notes into memory: from then on
# agents keep their notes in one place, and dreams stop reading the old ones. It says whether every
# note file was dealt with by a dream or some still hold facts no dream has placed (the store's
# dreams/own-notes.json; `python3 scripts/own_notes.py` in a repo says the same). It sets
# "autoMemoryEnabled": false in each folder's .claude/settings.local.json and keeps everything else
# in that file. --undo removes that one key. Nothing is deleted: the notes stay where Claude Code
# put them. Worktrees made later follow the repo (link-worktrees.sh's post-checkout hook).
set -eu
kit=$(cd "$(dirname "$0")" && pwd)
exec python3 - "$kit" "$@" <<'PY'
import json, os, stat, subprocess, sys
from pathlib import Path

sys.path.insert(0, os.path.join(sys.argv[1], "scripts"))
import own_notes  # where a repo's notes are, and whether Claude Code still keeps them

KEY = own_notes.KEY
USAGE = "usage: retire-auto-memory.sh <store-name> [--undo]"


def repos_of(store):
    listed = os.path.join(store, "roots.txt")
    if not os.path.isfile(listed):
        sys.exit(f"{store} has no roots.txt, so it isn't an installed store. Nothing was changed.")
    with open(listed) as f:
        repos = [line.rstrip("\n") for line in f if line.strip()]
    for repo in repos:
        if not os.path.isdir(repo):
            sys.exit(f"The store lists {repo}, which isn't there. Fix {listed}. Nothing was changed.")
    return repos


def folders(repos):
    """Every repo and every worktree of each that is on disk, each once, in order."""
    found = []
    for repo in repos:
        r = subprocess.run(["git", "-C", repo, "worktree", "list", "--porcelain"], capture_output=True, text=True)
        if r.returncode != 0:
            sys.exit(f"Can't list the worktrees of {repo}: {r.stderr.strip()[:200]} Nothing was changed.")
        # A worktree whose folder was removed by hand is still listed by git; there is nothing to set there.
        trees = [l.split(" ", 1)[1] for l in r.stdout.splitlines() if l.startswith("worktree ")]
        for path in [repo] + trees:  # git lists the repo itself among its worktrees
            if os.path.isdir(path) and path not in found:
                found.append(path)
    return found


def load(path):
    """The settings in a file, or None when there is no file. One that can't be read stops the run before any is written."""
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            data = json.load(f)
    except ValueError as error:
        sys.exit(f"{path} is not valid JSON ({error}). Fix it and run this again. Nothing was changed.")
    except OSError as error:
        sys.exit(f"Can't read {path}: {error.strerror}. Nothing was changed.")
    if not isinstance(data, dict):
        sys.exit(f"{path} does not hold a settings object. Fix it and run this again. Nothing was changed.")
    return data


def save(path, data):
    """Writes the file whole, or not at all. A link stays a link: the file it points at is the one written. The mode is kept."""
    real = os.path.realpath(path)
    os.makedirs(os.path.dirname(real), exist_ok=True)
    mode = stat.S_IMODE(os.stat(real).st_mode) if os.path.exists(real) else None
    tmp = real + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, real)  # a session reading its settings never sees half a file
    except OSError:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def main():
    args = sys.argv[2:]
    undo = "--undo" in args
    names = [a for a in args if a != "--undo"]
    if len(names) != 1 or names[0].startswith("-"):
        sys.exit(USAGE)
    store = os.path.join(os.path.expanduser("~"), "agent-memory", names[0])
    repos = repos_of(store)
    try:  # before any file is written: a record that can't be read stops the run whole
        notes = own_notes.state(Path(store), repos)
    except SystemExit as stop:
        sys.exit(f"{stop} Nothing was changed.")
    # Every file is read before any is written, so one that can't be read leaves the others as they were.
    files = [(path, load(path)) for path in (os.path.join(folder, ".claude", "settings.local.json") for folder in folders(repos))]
    changed = []
    for path, data in files:
        if undo:
            if data is None or KEY not in data:
                continue
            del data[KEY]
        else:
            if data is not None and data.get(KEY) is False:
                continue
            data = dict(data or {}, **{KEY: False})
        try:
            save(path, data)
        except OSError as error:
            before = "Changed before that: " + ", ".join(changed) + "." if changed else "No file was changed before that."
            sys.exit(f"Can't write {path}: {error.strerror}. {before} Fix it and run this again; a second run only changes what is left.")
        print(f"changed: {path}")
        changed.append(path)
    same = len(files) - len(changed)
    kept = sum(len(own_notes.notes(repo)) for repo in repos)
    if undo:
        print(f"auto memory is back on: {len(changed)} file(s) changed, {same} had nothing to remove. Dreams read Claude's own notes again ({kept} on disk).")
        return
    print(f"auto memory is off: {len(changed)} file(s) changed, {same} already off. "
          f"{kept} of Claude's own notes stay on disk, nothing was deleted, and dreams stop reading them.")
    print(f"{names[0]}: {own_notes.summary(notes)}"
          + (" It is off now, so no dream will until you turn it back on (--undo)." if notes["unread"] + notes["earlier"] else ""))
    for repo in repos:
        if own_notes.enabled(repo):  # the repo's own file is not the last word
            print(f"still on in {repo}: a setting above the repo's own turns it on (managed settings, or CLAUDE_CODE_DISABLE_AUTO_MEMORY=0 in an env block).")


main()
PY

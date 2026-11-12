#!/usr/bin/env python3
"""Which memory store a folder belongs to, and which repositories a store has.

  python3 scripts/store.py where [folder]          the store of that folder (default: here), or that it has none
  python3 scripts/store.py attach <store> <repo>   make <repo> one of the store's repositories
  python3 scripts/store.py detach <store> <repo>   take it out again

The one place that answers both questions, so nothing else works either out for itself.

From a folder to its store, find() looks in this order and stops at the first that answers:

1. AGENT_MEMORY_DIR, when it is set: that folder is the store. It has to be a whole path to a
   store: a folder with ORG.md and a git history of its own.
2. The key `agent-memory.store` in the configuration of the repository the folder is in, read with
   `git config --local`. A repository keeps that configuration in its shared git folder, so every
   subfolder and every linked worktree, wherever it sits, gives the same answer. The value is a
   store's name, letter for letter, and the store is ~/agent-memory/<name>. The key is also the
   switch: a repository without it has no memory.
3. For a folder with no `.git` above it, the nearest `.jj`. `.jj/repo` is a folder in a jj
   repository's first workspace and, in every other workspace, a file holding the path to that
   folder, relative to the `.jj` it is in. The git repository jj keeps is named in
   `<that>/store/git_target`, relative to `store`: `git` (so `<that>/store/git`) when jj keeps
   it for itself. The same key is read from there.
4. A repository with no key that sits inside another one (a nested checkout, a submodule) belongs
   to whatever the one around it belongs to: the search goes on outward. That is how a dream
   decides what is inside a repository too (slim.inside: by path), so the two agree. Not a linked
   worktree: it belongs to its own repository, wherever it was put, and that one has no key.
5. Otherwise the folder has no store.

The store is decided by the folder alone. git is always started in the top folder of the
checkout and without the variables that point it at another repository (REPOINTS), so GIT_DIR in
the caller's environment, which every git hook sets, can't pick the store.

The key and the store's list have to agree. A key that names a store that doesn't exist, or a
store whose roots.txt doesn't list the repository, is a fault and is said as one, with the
command that repairs it: it is not "no store", and nothing runs for that store until it is put
right. (A script that ran anyway would bring the session archive in line with a list that
leaves this repository out.)

From a store to its repositories, repos() reads `roots.txt` in the store, one path per line.
attach() writes both directions together and detach() removes both, under the store's lock, so
two at once can't lose each other's line. linked() says how one listed repository is tied to
the store today: by the `memory` link of the kit's older layout, by the key, by both or by
neither.

What can be expected to go wrong is raised as Problem, with one plain line to print.
"""
import argparse, contextlib, os, re, shlex, subprocess, sys
from pathlib import Path
from typing import NamedTuple, Optional

KEY = "agent-memory.store"
ENV = "AGENT_MEMORY_DIR"
FROM_KIT = "AGENT_MEMORY_FROM_KIT"  # bin/amk sets it to the kit's folder: a script that sits there is the kit's own copy, in no repository of the store
LISTED = "roots.txt"
NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]*")
REFUSED = 78  # the exit code when a folder has no store, or what was asked can't be done; no script of the kit uses it
GIT_SECONDS = 10  # git answers these questions in milliseconds; one that takes this long is stuck
# What makes git read another repository than the one in the folder it is started in. Never passed on.
# Not in the list, because they can't change the answer: GIT_CEILING_DIRECTORIES and GIT_DISCOVERY_ACROSS_FILESYSTEM
# (git is started in the checkout's top folder and never searches upward), and GIT_CONFIG_GLOBAL, GIT_CONFIG_SYSTEM,
# GIT_CONFIG_NOSYSTEM, GIT_CONFIG_COUNT with its keys and values, and GIT_CONFIG_PARAMETERS (`--local` reads the
# repository's own file and none of those; they stay because they are where a person says which folders git may trust).
REPOINTS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
            "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE", "GIT_CONFIG")


class Problem(Exception):
    """Something expected went wrong. Its text is one plain line for the person."""


class Checkout(NamedTuple):
    root: Path  # the top of the checkout the folder is in: a repository, one of its worktrees, or a jj workspace
    git: Path  # where git is asked for that repository's configuration
    first: Optional[Path]  # found through `.jj`: the jj repository's first workspace. None for a checkout git reads itself


class Found(NamedTuple):
    store: Optional[Path]  # the store, or None when the folder has none
    why: str  # what named the store, or why there is none


def home() -> Path:
    """Where the stores are."""
    return Path.home() / "agent-memory"


def stores() -> list:
    """Every store on this machine, by name."""
    base = home()
    return sorted(p for p in base.iterdir() if p.is_dir() and NAME.fullmatch(p.name)) if base.is_dir() else []


def named(name: str) -> Path:
    """The store with this name. Stops when the name can't be one or no store has exactly that name."""
    if not NAME.fullmatch(name):
        raise Problem(f"{name!r} is not a store name: letters, digits, dots, dashes and underscores only.")
    store = home() / name
    if store not in stores():  # listed, not asked for by path: a disk that takes Atlas for atlas must not
        close = [p.name for p in stores() if p.name.casefold() == name.casefold()]
        raise Problem(f"There is no store called {name}: the one at {home()} is {close[0]}, and a name has to match letter for letter."
                      if close else f"There is no store at {store}.")
    return store


def quiet() -> dict:
    """The environment git and every script of the kit is given: the caller's, without what points git at another repository."""
    return {name: value for name, value in os.environ.items() if name not in REPOINTS}


def git(folder: Path, *args):
    try:
        return subprocess.run(["git", "-C", str(folder), *args], capture_output=True, text=True, env=quiet(), timeout=GIT_SECONDS)
    except FileNotFoundError:
        raise Problem("git is not installed, or not on the PATH, so no repository's store can be read.")
    except subprocess.TimeoutExpired:
        raise Problem(f"git did not answer within {GIT_SECONDS} seconds in {folder}, so its store could not be read.")


def said(result) -> str:
    """What git said when it failed, on one line."""
    return " ".join((result.stderr.strip() or f"it exited with {result.returncode}").split())[:200]


def quoted(path) -> str:
    """A path as it can be pasted into a shell."""
    return shlex.quote(str(path))


def through_jj(root: Path) -> Optional[Checkout]:
    """The checkout of a folder that holds `.jj` and no `.git`, or None when jj keeps no git repository behind it."""
    jj = root / ".jj"
    repo = jj / "repo"
    try:
        if repo.is_file():  # a later workspace: the file holds the way to the first workspace's .jj/repo
            repo = (jj / repo.read_text().strip()).resolve()
        behind = (repo / "store" / (repo / "store" / "git_target").read_text().strip()).resolve()
    except FileNotFoundError:
        return None
    return Checkout(root, behind, repo.parent.parent)


def checkouts(folder: Path):
    """Every checkout this folder is in, the nearest first: each folder on the way up that holds `.git` or `.jj`.

    `.git` comes first at each level, so a repository that jj shares with git is read as git reads it.
    """
    try:
        folder = folder.resolve()
        for at in (folder, *folder.parents):
            if os.path.lexists(at / ".git"):
                yield Checkout(at, at, None)
            elif (at / ".jj").is_dir():
                found = through_jj(at)
                if found:
                    yield found
    except OSError as stop:
        raise Problem(f"{folder} can't be searched for its repository ({type(stop).__name__}: {stop.strerror}).")


def checkout(folder: Path) -> Optional[Checkout]:
    """The nearest checkout this folder is in, or None."""
    return next(checkouts(folder), None)


def key(found: Checkout) -> Optional[str]:
    """The store this checkout's repository names, or None when it names none."""
    result = git(found.git, "config", "--local", "--get", KEY)
    if result.returncode == 0:
        return result.stdout.rstrip("\n")
    if result.returncode == 1:  # git's answer for a key that is not set
        return None
    raise Problem(f"git can't read the configuration of {found.root}: {said(result)}")


def keyed(root: Path) -> Optional[Path]:
    """The store the repository at `root` names with its key, or None when it has no key. For slim.py, which asks of a folder it knows is a checkout.

    Only what the key says: whether that store exists and lists the repository is find()'s to judge.
    A configuration git can't read stops the caller, in words, as the kit's other readers stop.
    """
    try:
        name = key(Checkout(root, root, None))
    except Problem as stop:
        raise SystemExit(str(stop))
    return (home() / name).resolve() if name and NAME.fullmatch(name) else None


def main_folder(found: Checkout) -> Path:
    """The folder roots.txt lists for this checkout's repository: its main checkout, which every worktree shares the key with.

    A checkout whose `.git` is a folder is the main one, and nothing is asked. For a linked worktree git
    says where the shared folder is. A submodule's shared folder is inside its parent's, so it is its own main folder.
    """
    if found.first:
        return found.first
    if (found.root / ".git").is_dir():
        return found.root
    result = git(found.root, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if result.returncode != 0:
        raise Problem(f"git can't say which repository {found.root} belongs to: {said(result)}")
    common = Path(result.stdout.rstrip("\n"))
    return common.parent if common.name == ".git" else found.root


def same(a, b) -> bool:
    return os.path.realpath(a) == os.path.realpath(b)


def repos(store: Path) -> list:
    """The repositories of a store, as roots.txt lists them: one path per line, in the file's order.

    Blank lines are not repositories, space around a path is not part of it, and `~` is the home folder.
    """
    try:
        text = (store / LISTED).read_text()
    except FileNotFoundError:
        return []
    return [os.path.expanduser(line.strip()) for line in text.split("\n") if line.strip()]


def told() -> Optional[Path]:
    """The store AGENT_MEMORY_DIR names, or None when it is not set. One that can't be a store is refused."""
    value = os.environ.get(ENV)
    if value is None:
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise Problem(f"{ENV} is {value!r}, which is not a whole path. Unset it, or give the store's folder from the root.")
    if not ((path / "ORG.md").is_file() and os.path.lexists(path / ".git")):
        raise Problem(f"{ENV} names {path}, which is not a memory store: a store is a folder with ORG.md and a git history of its own. "
                      "Unset it, or give the store's folder.")
    return path.resolve()


def find(folder=None) -> Found:
    """The store this folder (default: the working directory) belongs to. See the top of this file for the order."""
    store = told()
    if store:
        return Found(store, f"{ENV} names it")
    try:
        folder = Path(folder or os.getcwd())
    except OSError as stop:
        raise Problem(f"The working directory can't be read ({type(stop).__name__}: {stop.strerror}), so there is no telling which store it belongs to.")
    nearest = None
    for found in checkouts(folder):
        nearest = nearest or found
        name = key(found)
        if name is None:
            main = main_folder(found)
            if not same(main, found.root):  # a worktree is its repository's (repository()), wherever it stands: that one has no key
                return Found(None, f"{found.root} is a worktree of the repository at {main}, which has no {KEY} key")
            continue  # a checkout inside another belongs to what the one around it belongs to
        if not NAME.fullmatch(name):
            raise Problem(f"The {KEY} key of {found.root} is {name!r}, which is not a store name.")
        main = main_folder(found)
        try:
            store = named(name)
        except Problem as stop:
            raise Problem(f"The {KEY} key of {main} names {name}. {stop}")
        if not any(same(line, main) for line in repos(store)):
            raise Problem(f"The {KEY} key of {main} names {name}, but {store / LISTED} doesn't list that repository, so nothing is run for it. "
                          f"To put it right: amk attach {name} {quoted(main)}")
        return Found(store, f"the {KEY} key of {main} names it")
    if nearest is None:
        return Found(None, f"{folder} is not inside a git or jj repository")
    return Found(None, f"the repository at {nearest.root} has no {KEY} key")


def run_for(root: Path, store: Path) -> Optional[Path]:
    """Which repository a run of a kit script is for: the one rule for every script that needs a repository and not only the store.

    `root` is the folder the script's copy sits in (its ROOT). A copy inside a repository is that repository's,
    as it always was: the answer is `root`, and nothing is asked. The kit's own copy, which bin/amk names in
    AGENT_MEMORY_FROM_KIT, sits in no repository: its run is for the repository the working directory is in,
    the nearest checkout on the way up whose repository (repository(): a worktree's is the one it was made
    from, a jj workspace's its first) is a line of the store's roots.txt. None when it is in none of them.
    """
    named = os.environ.get(FROM_KIT)
    if not named or os.path.realpath(named) != os.path.realpath(root):  # by the real path: /tmp and /private/tmp are one folder
        return root
    listed = repos(store)
    try:
        for found in checkouts(Path.cwd()):
            main = main_folder(found)
            if any(same(line, main) for line in listed):
                return main
    except (OSError, Problem):
        return None
    return None


def persons(what: str):
    """Stops a command that is the person's when an unattended dream runs it: the kit's one rule for that (dream_proposal.read_only)."""
    import dream_proposal  # when it is asked, not with this file: see writers()
    try:
        dream_proposal.read_only(what)
    except SystemExit as stop:
        raise Problem(str(stop))


def repository(root: Path) -> Path:
    """The main folder of the repository a checkout belongs to: itself, or for a linked worktree the checkout it was made from.

    A worktree belongs where its repository belongs, wherever it stands. find() goes by that, and so does
    slim.py when it asks whether a folder inside a store's repo is that store's. A git that can't say
    stops the caller, in words, as the kit's other readers stop.
    """
    try:
        return main_folder(Checkout(root, root, None))
    except Problem as stop:
        raise SystemExit(str(stop))


def writers():
    """memcheck.py, for the store's one lock and its commit helper.

    Loaded when a store is first written, not with this file: memcheck.py and every script that loads it
    take their store from AGENT_MEMORY_DIR as they load, and bin/amk asks this file which store that is
    before it sets the variable.
    """
    import memcheck
    return memcheck


@contextlib.contextmanager
def locked(store: Path):
    """The store's one lock, for everything attach and detach read, decide and write. A lock that can't be taken stops them."""
    with contextlib.ExitStack() as held:
        try:
            held.enter_context(writers().locked(store))
        except OSError as stop:
            raise Problem(f"The lock of the store at {store} can't be taken ({type(stop).__name__}: {stop.strerror}), so nothing was changed.")
        yield


def record(store: Path, lines: list, was: list, message: str):
    """Write roots.txt if its lines changed, and commit it. Called under the store's lock.

    Written and committed, or as it was: when the commit fails the file is put back, byte for byte,
    and the failure is raised for the caller to put the key back too.
    """
    path = store / LISTED
    before = path.read_bytes() if path.exists() else None
    if lines != was:
        tmp = path.with_name(LISTED + ".tmp")
        tmp.write_text("".join(f"{line}\n" for line in lines))
        os.replace(tmp, path)
    if git(store, "rev-parse", "--is-inside-work-tree").returncode != 0:
        return  # a store with no history of its own has nothing to commit to
    error = writers().commit(store, message, [LISTED])
    if error:
        if before is None:
            path.unlink()
        else:
            path.write_bytes(before)
        git(store, "reset", "-q", "--", LISTED)  # what the failed commit may have staged
        raise Problem(f"The store couldn't commit {LISTED} ({error}), so nothing was changed. Run the same command again.")


def unwritten(store: Path, stop: Exception) -> Problem:
    """What attach and detach say when roots.txt could not be written or committed, once the key is as it was."""
    if isinstance(stop, Problem):
        return stop
    return Problem(f"{store / LISTED} couldn't be written ({type(stop).__name__}: {stop.strerror}), so nothing was changed.")


def part_of(folder: Path, main: Path, verb: str, name: str) -> Problem:
    """What attach and detach say to a subfolder or a worktree: which folder to give instead."""
    return Problem(f"{folder} is part of the repository at {main}. Its subfolders and worktrees share the key, "
                   f"so it is that folder you {verb}: amk {verb} {name} {quoted(main)}")


def settle():
    """Take out of this process's environment what points git at another repository, before anything is written.

    The store's lock and commit helper (memcheck.py) start git with the environment as it is.
    """
    for name in REPOINTS:
        os.environ.pop(name, None)


def attach(name: str, repo: str) -> str:
    """Make `repo` one of the store's: the key in the repository and the line in roots.txt, both or neither.

    Refused for a folder that is not the main folder of a repository, for a store's own folder, and for a
    repository that already belongs to another store, by its key or by that store's roots.txt. Attaching
    again changes nothing. Everything is read, decided and written under the store's lock.
    """
    persons("Which store a repository belongs to is the person's to change")
    store = named(name)
    folder = Path(repo).expanduser().resolve()
    if not folder.is_dir():
        raise Problem(f"{folder} is not a folder.")
    if any(same(at, home()) for at in (folder, *folder.parents)):
        raise Problem(f"{folder} is in {home()}, where the stores are: a store is not attached to a store.")
    found = checkout(folder)
    if found is None:
        raise Problem(f"{folder} is not inside a git or jj repository, so it has nowhere to keep the {KEY} key.")
    main = main_folder(found)
    if not same(main, folder):
        raise part_of(folder, main, "attach", name)
    with locked(store):
        before = key(found)
        if before not in (None, name):
            raise Problem(f"{folder} already belongs to the store {before}. Detach it there first: amk detach {before} {quoted(folder)}")
        for other in stores():
            if other != store and any(same(line, folder) for line in repos(other)):
                raise Problem(f"{folder} already belongs to the store {other.name}: its {LISTED} lists it. "
                              f"Detach it there first: amk detach {other.name} {quoted(folder)}")
        was = repos(store)
        listed = any(same(line, folder) for line in was)
        if before is None:
            result = git(found.git, "config", "--local", KEY, name)
            if result.returncode != 0:
                raise Problem(f"git couldn't write the {KEY} key in {folder}, so nothing was changed: {said(result)}")
        try:
            record(store, was if listed else was + [str(folder)], was, f"memory: attach {folder}")
        except (OSError, Problem) as stop:  # the key and the line go together: without the line, the key goes back
            if before is None:
                git(found.git, "config", "--local", "--unset", KEY)
            raise unwritten(store, stop)
    return f"{folder} already belongs to {name}. Nothing changed." if before == name and listed else f"attached: {folder} belongs to {name}."


def detach(name: str, repo: str) -> str:
    """Take `repo` out of the store: its line in roots.txt, and its key when the key names this store.

    The same folder attach takes. A worktree or a subfolder is told which folder to give, because unsetting
    the key there would switch memory off for the whole repository and leave its line behind. Any line of
    roots.txt can be removed by the path status reports it with: one whose folder is gone, one that is no
    whole path, one that is no repository's main folder. The key is touched only in a repository's main folder.
    """
    persons("Which store a repository belongs to is the person's to change")
    store = named(name)
    folder = Path(repo).expanduser().resolve()
    found = checkout(folder) if folder.is_dir() else None
    main = main_folder(found) if found else None
    with locked(store):
        was = repos(store)
        kept = [line for line in was if not same(line, folder)]
        if found and not same(main, folder) and kept == was:
            raise part_of(folder, main, "detach", name)
        holds = found is not None and same(main, folder) and key(found) == name
        if not holds and kept == was:
            raise Problem(f"{folder} does not belong to {name}: {LISTED} doesn't list it and its key doesn't name that store.")
        if holds:
            result = git(found.git, "config", "--local", "--unset", KEY)
            if result.returncode != 0:
                raise Problem(f"git couldn't remove the {KEY} key in {folder}, so nothing was changed: {said(result)}")
        try:
            record(store, kept, was, f"memory: detach {folder}")
        except (OSError, Problem) as stop:
            if holds:
                git(found.git, "config", "--local", KEY, name)
            raise unwritten(store, stop)
    return f"detached: {folder} no longer belongs to {name}."


def linked(store: Path, repo: str) -> tuple:
    """How one line of roots.txt is tied to the store: (`link`, `key`, `both` or `neither`; what is wrong with it, or empty).

    `link` is the kit's older layout: `memory` in the repository leads to the store. `key` is the
    repository's agent-memory.store key naming it. Until a repository is moved to the key (step 5 of
    docs/one-kit-design.md onwards) the link alone is its normal state and no fault. Faults are a line
    that leads nowhere, a key that names another store, and a repository tied by neither.
    """
    name = store.name
    out = f"(amk detach {name} {quoted(repo)} removes the line)"
    if not os.path.isabs(repo):
        return "neither", f"{repo!r} in {LISTED} is not a whole path, so it names no repository {out}"
    if not Path(repo).is_dir():
        return "neither", f"{repo} is listed in {LISTED} but no longer exists {out}"
    link = (Path(repo) / "memory").exists() and same(Path(repo) / "memory", store)
    try:
        found = checkout(Path(repo))
        value = key(found) if found and same(found.root, repo) else None
    except Problem as stop:
        return ("link" if link else "neither"), f"{repo} is listed in {LISTED}, but its key can't be read: {stop}"
    if value not in (None, name):
        return ("link" if link else "neither"), f"{repo} is listed in {LISTED}, but its {KEY} key names {value}"
    state = {(True, True): "both", (True, False): "link", (False, True): "key", (False, False): "neither"}[link, value == name]
    wrong = f"{repo} is listed in {LISTED}, but has neither a memory/ link to this store nor the {KEY} key, so its sessions load no memory"
    return state, wrong if state == "neither" else ""


def main():
    ap = argparse.ArgumentParser(prog="amk" if os.environ.get(FROM_KIT) else None,
                                 description="Which memory store a folder belongs to, and which repositories a store has.")
    sub = ap.add_subparsers(dest="verb", required=True)
    where = sub.add_parser("where", help="the store of a folder, or that it has none")
    where.add_argument("folder", nargs="?", help="default: the working directory")
    for verb, does in (("attach", "make a repository one of the store's"), ("detach", "take it out again")):
        one = sub.add_parser(verb, help=does)
        one.add_argument("store", help="the store's name")
        one.add_argument("repo", help="the repository's main folder")
    a = ap.parse_args()
    settle()
    if a.verb == "where":
        found = find(a.folder)
        if found.store is None:
            print(f"No memory store: {found.why}.", file=sys.stderr)
            sys.exit(REFUSED)
        print(found.store)
    else:
        print({"attach": attach, "detach": detach}[a.verb](a.store, a.repo))


if __name__ == "__main__":
    try:
        main()
    except Problem as stop:
        print(stop, file=sys.stderr)
        sys.exit(REFUSED)

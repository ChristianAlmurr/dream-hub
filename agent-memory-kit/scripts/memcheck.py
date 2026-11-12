#!/usr/bin/env python3
"""Store integrity: catches what no guard can prevent, and keeps every tier versioned.

  python3 scripts/memcheck.py          report problems, exit 1 if there are any
  python3 scripts/memcheck.py --seal   first commit human ORG.md and STEERING.md edits, scratchpads and the inbox

Every write to project/ goes through memwrite.py, which logs the file's new hash. So the last
hash logged for a file must be the hash it has now. A mismatch means something wrote around
the protocol, by any route at all. The other scripts share the lock and commit helper here.
"""
import contextlib, datetime, fcntl, hashlib, os, re, stat, subprocess, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MEM = Path(os.environ.get("AGENT_MEMORY_DIR", ROOT / "memory")).expanduser().resolve()
STEERING = "STEERING.md"  # the store's Steering, once it has one: the person's, like ORG.md
ROW = re.compile(r"^\| [^|]+ \| ([^|]+?) \| [^|]+ \| [^|]+ \| .* \| \S+ → (\S+) \|$")


def git(mem: Path, *args):
    return subprocess.run(["git", "-C", str(mem), *args], capture_output=True, text=True)


@contextlib.contextmanager
def locked(mem: Path):
    """The one lock for everything that writes to or commits in the store."""
    with open(mem / ".memwrite.lock", "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def commit(mem: Path, message: str, paths: list) -> str:
    """Commit exactly these paths and nothing else that is staged. Returns the error, or empty.

    Empty means the paths are committed, or had nothing to commit. Any step of git that fails is an error for
    the caller: a `git add` that could not stage is not "nothing to commit".
    """
    error = ""
    for attempt in range(6):  # a human's own git command can hold git's index lock for a moment
        step = git(mem, "add", "-A", "--", *paths)
        if step.returncode == 0:
            step = git(mem, "diff", "--cached", "--quiet", "--", *paths)
            if step.returncode == 0:
                return ""  # staged, and no different from what is committed
            if step.returncode == 1:  # there is something to commit
                step = git(mem, "commit", "-q", "-m", message, "--", *paths)
                if step.returncode == 0:
                    return ""
        error = " ".join(step.stderr.split())[:200] or f"git exited with {step.returncode}"  # one line: it goes into messages
        if "index.lock" not in error:
            break
        time.sleep(0.25 * (attempt + 1))
    return error


def is_locked(path: Path):
    """Whether a file carries the kernel's immutable flag (uchg), which only a person lifts. None where files have no such flag."""
    flags = getattr(os.stat(path), "st_flags", None)
    return None if flags is None else bool(flags & stat.UF_IMMUTABLE)


def org_locked(mem: Path):
    """Whether ORG.md carries the flag."""
    return is_locked(mem / "ORG.md")


def lock_org(mem: Path):
    """Set the flag on ORG.md, and on STEERING.md when the store has one. install-store.sh sets it once; whoever finds it off sets it again."""
    for path in (mem / "ORG.md", mem / STEERING):
        if path.name == "ORG.md" or path.exists():
            os.chflags(path, os.stat(path).st_flags | stat.UF_IMMUTABLE)


def steering_gone(mem: Path) -> bool:
    """STEERING.md is tracked by the store's git and not on disk: a file that went around the protocol, not one taken out of history."""
    if (mem / STEERING).exists() or STEERING not in logged(mem):
        return False  # git is asked only of a store whose log shows it once had one: a status of the others costs nothing more
    return git(mem, "ls-files", "--error-unmatch", "--", STEERING).returncode == 0


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def logged(mem: Path) -> dict:
    log = mem / "CHANGELOG.md"
    found = {}
    for line in (log.read_text(errors="ignore").splitlines() if log.exists() else []):
        match = ROW.match(line)
        if match:
            found[match[1].strip()] = match[2]
    return found


def drift(mem: Path) -> list:
    """Files in project/ whose content isn't what the log says memwrite last wrote."""
    out = []
    for rel, want in sorted(logged(mem).items()):
        path = mem / rel
        if not rel.startswith("project/"):
            continue
        if not path.exists():
            out.append(f"{rel} was deleted outside memwrite")
        elif sha(path) != want:
            last = git(mem, "log", "-1", "--format=%s", "--", rel).stdout
            clean = not git(mem, "status", "--porcelain", "--", rel).stdout.strip()
            if not (clean and last.startswith('Revert "')):  # a human rollback is the documented route
                out.append(f"{rel} changed outside memwrite (log says {want}, file is {sha(path)})")
    return out


def problems(mem: Path) -> list:
    out = drift(mem)
    if git(mem, "rev-parse", "--is-inside-work-tree").returncode != 0:
        return out + ["the store is not a git repository, so nothing is versioned"]
    known = logged(mem)
    for rel in (str(p.relative_to(mem)) for p in sorted((mem / "project").rglob("*.md"))):
        if rel not in known and git(mem, "ls-files", "--error-unmatch", rel).returncode != 0:
            out.append(f"{rel} appeared outside memwrite and was never committed")
    for row in git(mem, "status", "--porcelain", "--", "ORG.md", "INDEX.md", "CHANGELOG.md", "project", *[STEERING] * (mem / STEERING).exists()).stdout.splitlines():
        out.append(f"uncommitted change: {row[3:]}")
    if (mem / "ORG.md").exists() and org_locked(mem) is False:
        out.append(f"ORG.md is not locked, so anything can write your principles. Look at `git -C {mem} diff ORG.md`, then lock it: chflags uchg {mem / 'ORG.md'}")
    if steering_gone(mem):
        out.append(f"{STEERING} is in the store's history and gone from its folder, so every repo is back on the Steering block of its "
                   f".claude/commands/dream.md. Put it back (git -C {mem} checkout -- {STEERING}), or remove it from history (git -C {mem} rm {STEERING})")
    if (mem / STEERING).exists() and is_locked(mem / STEERING) is False:
        out.append(f"{STEERING} is not locked, so anything can write what matters in your projects. Look at `git -C {mem} diff {STEERING}`, "
                   f"then lock it: chflags uchg {mem / STEERING}")
    import dream_proposal  # here and not at the top: dream_proposal.py loads this file
    trouble = dream_proposal.problem(mem)  # no dream starts and no proposal can be closed until it is put right
    if trouble:
        return out + [trouble]
    return out + [f"dreams/{name} is a proposal no dream finished: it was never checked or committed, and no record knows it. "
                  f"Read it if you want to, then remove it: rm memory/dreams/{name}" for name in dream_proposal.orphans(mem)]


def seal(mem: Path) -> list:
    """Versions what is allowed to change without memwrite, so no tier sits outside history."""
    done = []
    with locked(mem):
        for name, what in (("ORG.md", "principles"), (STEERING, "steering")):
            if (name == "ORG.md" or (mem / name).exists()) and git(mem, "status", "--porcelain", "--", name).stdout.strip():
                before = git(mem, "rev-parse", "--short=12", f"HEAD:{name}").stdout.strip() or ("" if name == "ORG.md" else "new")
                when = datetime.datetime.now().isoformat(timespec="seconds")
                with open(mem / "CHANGELOG.md", "a") as log:
                    log.write(f"| {when} | {name} | human | - | {what} edited by hand | {before} → {sha(mem / name)} |\n")
                if not commit(mem, f"memory(human): {what} edited by hand", [name, "CHANGELOG.md"]):
                    done.append(name)
        paths = [p for p in ("agents", "inbox") if (mem / p).exists() and git(mem, "status", "--porcelain", "--", p).stdout.strip()]
        if paths and not commit(mem, "memory: snapshot of scratchpads and inbox", paths):
            done += paths
    return done


if __name__ == "__main__":
    if "--seal" in sys.argv:
        print("sealed:", ", ".join(seal(MEM)) or "nothing to seal")
    found = problems(MEM)
    print("\n".join(found) if found else "Store is intact.")
    sys.exit(1 if found else 0)

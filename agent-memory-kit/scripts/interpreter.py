#!/usr/bin/env python3
"""Which Python bin/amk runs under.

  python3 scripts/interpreter.py                    the Python this runs under, and the one on record
  python3 scripts/interpreter.py record [python3]   put one on record (default: the one this runs under). The person's.

bin/amk starts every verb, and from step 4 every hook, under one Python. /usr/bin/python3 on a Mac is
a launcher that belongs to Xcode: while Xcode updates, or its licence is not accepted, it can't start
anything. So the kit keeps on record the real program an interpreter is (what it says its own
executable is), in ~/.config/agent-memory/interpreter, and bin/amk uses that first. `record` starts the
interpreter it is given, has it load the kit's store.py, and refuses one older than 3.9.

recorded() and unusable() are the one place that reads the record; status.py reports from them.
"""
import os, subprocess, sys
from pathlib import Path

OLDEST = (3, 9)
SECONDS = 20  # an interpreter that takes this long to say its version is not one to run every hook under


def record_file() -> Path:
    return Path.home() / ".config" / "agent-memory" / "interpreter"


TESTED = "tested"  # the second line of the record, which `record` writes after it has run the kit under the interpreter


def recorded():
    """The interpreter on record, or None when none is: the first line of the record."""
    try:
        return (record_file().read_text().split("\n") + [""])[0].strip() or None
    except FileNotFoundError:
        return None


def unusable(path: str) -> str:
    """Why bin/amk would not start the interpreter on record, or empty when it would. The same tests bin/amk makes, in its order.

    bin/amk starts an interpreter once and gives the process over to it, so it can't try one and then another.
    It goes by what can be seen without starting anything: the file is a program, `record` tested it (the
    record says so), and it has not changed since (it is not newer than the record).
    """
    if not (os.path.isfile(path) and os.access(path, os.X_OK)):
        return "is not a program that can be started"
    lines = record_file().read_text().split("\n")
    if not (len(lines) > 1 and lines[1].startswith(TESTED)):
        return "was not put on record by `amk interpreter record`, which tests it first"
    if os.stat(path).st_mtime > os.stat(record_file()).st_mtime:
        return "has changed since it was put on record"
    return ""


def running() -> str:
    """The real program this process is."""
    return os.path.realpath(sys.executable)


def record(python: str) -> str:
    """Try an interpreter and put the real program it is on record. Returns what to say."""
    import dream_proposal  # for the kit's one rule on what is the person's; it takes a store as it loads, and this file needs none
    dream_proposal.read_only("Which Python amk runs under is the person's to choose")
    ask = "import os, sys; sys.path.insert(0, sys.argv[1]); import store; print(os.path.realpath(sys.executable)); print(*sys.version_info[:3], sep='.')"
    try:
        tried = subprocess.run([python, "-c", ask, str(Path(__file__).resolve().parent)], capture_output=True, text=True, timeout=SECONDS)
    except (OSError, subprocess.TimeoutExpired) as stop:
        sys.exit(f"{python} can't be started ({type(stop).__name__}), so nothing was recorded.")
    if tried.returncode != 0:
        said = " ".join(tried.stderr.split())[-200:] or f"it exited with {tried.returncode}"
        sys.exit(f"{python} can't run the kit, so nothing was recorded: {said}")
    real, version = tried.stdout.split("\n")[:2]
    if tuple(int(part) for part in version.split(".")[:2]) < OLDEST:
        sys.exit(f"{python} is Python {version}; the kit needs {OLDEST[0]}.{OLDEST[1]} or later. Nothing was recorded.")
    path = record_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(f"{real}\n{TESTED} with Python {version}\n")
    os.replace(tmp, path)
    return f"recorded: amk runs under {real} (Python {version})."


def main():
    args = sys.argv[1:]
    if args[:1] == ["record"] and len(args) <= 2:
        print(record(args[1] if len(args) == 2 else sys.executable))
    elif not args:
        kept = recorded()
        print(f"running under {running()} (Python {'.'.join(map(str, sys.version_info[:3]))})")
        why = kept and unusable(kept)
        print("on record: " + (kept + (f", which {why}: bin/amk goes on with another" if why else "") if kept else "none; bin/amk takes /usr/bin/python3, else the first python3 on the PATH"))
    else:
        sys.exit("usage: interpreter.py [record [python3]]")


if __name__ == "__main__":
    main()

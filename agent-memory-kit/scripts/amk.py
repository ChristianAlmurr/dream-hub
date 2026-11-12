#!/usr/bin/env python3
"""amk: the one way into the agent memory kit.

  amk <verb> [arguments]      run from anywhere inside a repository that has a store
  amk help                    the verbs

It finds the kit from where this file really is, so it works by its full path, through a link and
by name from the PATH. It asks scripts/store.py which store the working directory belongs to, sets
AGENT_MEMORY_DIR to it, and runs the verb's script with the rest of the arguments.

bin/amk, a few lines of sh, chooses the Python and starts this file under it: the one on record
(scripts/interpreter.py), else /usr/bin/python3, else the first python3 on the PATH. So a session,
a hook and the scheduler run the same code on the same Python whatever the PATH is.

The script runs in this same process (runpy), not in one of its own: a second interpreter costs
about 20 ms on every call, and this is to run on every Bash call. So what the script reads,
prints and exits with is its own, and a signal sent to amk is a signal sent to the script. Two
things are set first that a script run by hand doesn't have: SIGPIPE ends the process quietly, so
`amk past … | head -1` leaves no "Exception ignored" behind, and the variables that point git at
another repository (GIT_DIR and the like, which every git hook sets) are taken out, so the
store's commits land in the store. No script of the kit is loaded before the store is known:
they take it from AGENT_MEMORY_DIR as they load.

amk's own two refusals have exit codes no script of the kit uses: 64 for a verb it doesn't have
(or one that takes nothing after it and was given something), 78 when the folder has no store, or names one that can't be used, and the
verb needs one.
"""
import os, runpy, signal, sys
from pathlib import Path

KIT = Path(__file__).resolve().parent.parent
SCRIPTS = KIT / "scripts"
sys.path.insert(0, str(SCRIPTS))  # where a script finds the others, as when it is run by its own path

import store
from verbs import ALONE, VERBS

UNKNOWN = 64  # the exit code for a verb amk doesn't have


def table() -> str:
    width = max(len(verb) for verb in VERBS)
    return "\n".join(["usage: amk <verb> [arguments]", ""] + [f"  {verb:<{width}}  {does}" for verb, (_, _, _, does) in VERBS.items()]
                     + ["", f"  {'help':<{width}}  this list"])


def main():
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)  # a reader that closes early ends this quietly, as it ends any shell tool
    verb, rest = (sys.argv[1], sys.argv[2:]) if len(sys.argv) > 1 else ("help", [])
    if verb in ("help", "--help", "-h"):
        print(table())
        return
    if verb not in VERBS:
        print(f"amk: there is no verb {verb!r}. `amk help` lists them.", file=sys.stderr)
        sys.exit(UNKNOWN)
    if verb in ALONE and rest:
        print(f"amk {verb} takes nothing after it. `amk help` lists the verbs.", file=sys.stderr)
        sys.exit(UNKNOWN)
    script, first, needs_store, _ = VERBS[verb]
    store.settle()
    if needs_store:
        try:
            found = store.find()
        except store.Problem as stop:
            print(f"amk {verb}: {stop}", file=sys.stderr)
            sys.exit(store.REFUSED)
        if found.store is None:
            print(f"amk {verb}: no memory store here: {found.why}.", file=sys.stderr)
            sys.exit(store.REFUSED)
        os.environ[store.ENV] = str(found.store)
    os.environ[store.FROM_KIT] = str(KIT)
    sys.argv = [str(SCRIPTS / script), *first, *rest]
    runpy.run_path(sys.argv[0], run_name="__main__")


if __name__ == "__main__":
    main()

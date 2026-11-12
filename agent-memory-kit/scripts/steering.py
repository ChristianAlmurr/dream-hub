#!/usr/bin/env python3
"""The store's Steering, STEERING.md: the person's, kept and locked like ORG.md.

  amk steering init     the person's: the store's STEERING.md, made from its repositories' Steering blocks

Until a store has STEERING.md, a repository's Steering is the block at the end of its
.claude/commands/dream.md, one copy per repository. `init` moves it into the store so that every
repository's sessions load exactly what they loaded before, and refuses when they would not:
- every repository has the same block: it is the text at the top, for all of them;
- otherwise each repository's own block is its section, `## <name of its main folder>`;
- a block that is still the kit's default, or none, becomes nothing.
The file is committed with a row in CHANGELOG.md, as memwrite.py commits a write, and locked (uchg).
From then on it is the only source of Steering (memory_context.block()): the blocks in the
repositories' dream.md are not read any more. Changes to it are the person's, by hand (then
`memcheck.py --seal`) or through `dream_apply.py --yours`.
"""
import argparse, os, sys
from pathlib import Path

import dream_proposal
import memcheck
import memory_context
import memwrite
import store

MEM = memwrite.MEM
TITLE = "# Steering: what matters in this store's projects (the person's to write; agents propose changes)"
HOW = ("<!-- The text here is for every repository of this store. A section `## <repo>`, named after a repository's main folder, "
       "is for that repository alone. -->")


def made(mem: Path) -> str:
    """The STEERING.md that says to each repository of the store what its Steering block says now."""
    repos = store.repos(mem)
    own = {repo: memory_context.own(memory_context.block(Path(repo), mem) or "", memory_context.STEERING_SHIPPED).strip("\n") for repo in repos}
    texts = set(own.values())
    if len(texts) == 1:
        body = [texts.pop()]
    else:
        names = [Path(repo).name for repo in repos if own[repo].strip()]
        twice = sorted({name for name in names if names.count(name) > 1})
        if twice:
            raise SystemExit(f"Two repositories of {mem.name} are called {twice[0]} and say different things in their Steering blocks, and a section of "
                             "STEERING.md names a repository by its folder's name. Nothing was written.")
        body = [f"## {Path(repo).name}\n{own[repo]}" for repo in repos if own[repo].strip()]
    text = "\n\n".join([TITLE, HOW] + [part for part in body if part.strip()]) + "\n"
    for repo in repos:  # what every repository loads must not change, or this is no move
        if memory_context.given(memory_context.part_for(text, Path(repo))) != memory_context.given(memory_context.block(Path(repo), mem)):
            raise SystemExit(f"STEERING.md would give {repo} other Steering than its block in .claude/commands/dream.md does "
                             "(a heading `## ` inside a block, or a block that only looks like the kit's default). Nothing was written.")
    return text


def worktrees_apart(mem: Path, text: str) -> list:
    """A line for each worktree of the store's repos whose own Steering block says something else than its repo will load from `text`."""
    out = []
    for repo in store.repos(mem):
        listed = store.git(Path(repo), "worktree", "list", "--porcelain").stdout.splitlines()
        for tree in (line[len("worktree "):] for line in listed if line.startswith("worktree ")):
            if store.same(tree, repo):
                continue
            own = memory_context.block(Path(tree), Path(tree) / ".no-store")  # its dream.md block: the store's STEERING.md is not asked
            if own is not None and memory_context.given(own) != memory_context.given(memory_context.part_for(text, Path(repo))):
                out.append(f"{tree}: its own Steering block differs from {Path(repo).name}'s; it loads {Path(repo).name}'s Steering from "
                           "STEERING.md once it carries the new kit")
    return out


def init(mem: Path) -> str:
    dream_proposal.read_only("Where a store's Steering is kept is the person's to change")
    path = mem / memory_context.STEERING
    if path.exists():
        raise SystemExit(f"{path} is there already, and it is the store's Steering. Nothing was written.")
    text = made(mem)
    apart = worktrees_apart(mem, text)
    why = "steering moved from the repositories' .claude/commands/dream.md"
    try:
        with memcheck.locked(mem):
            written = memwrite.record(path, text, "new", "human", "-", why)
    except memwrite.Refused as stop:
        raise SystemExit(f"{stop} STEERING.md was not made.")
    if hasattr(os, "chflags"):
        memcheck.lock_org(mem)
    common, named = memory_context.sections(text)
    holds = (f"a section for {', '.join(named)}" if named else "one text for every repository" if memory_context.given(common)
             else "nothing yet, since every block was the kit's default")
    return (f"{path} is the Steering of {mem.name} now, locked like ORG.md ({'commit ' + written.commit if written.commit else 'the store has no history'}). "
            f"It holds {holds}. The Steering blocks in the repositories' .claude/commands/dream.md are no longer read."
            + "".join("\n" + line for line in apart))


def main():
    ap = argparse.ArgumentParser(prog="amk steering" if os.environ.get(store.FROM_KIT) else None, description=__doc__.split("\n\n")[0])
    ap.add_argument("command", choices=["init"])
    ap.parse_args()
    store.settle()
    print(init(MEM))


if __name__ == "__main__":
    main()

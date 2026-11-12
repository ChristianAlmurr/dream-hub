#!/usr/bin/env python3
"""Flag memory lines that name a file no longer in any repo using this store.

  python3 scripts/check_stale.py

A dream's verify step starts from this list. A hit is a candidate, not a verdict: the file may
have moved, or the token may not be a file at all, so the dream checks each one.
"""
import os, re, subprocess
from pathlib import Path

import slim

ROOT = Path(__file__).resolve().parent.parent
MEM = Path(os.environ.get("AGENT_MEMORY_DIR", ROOT / "memory")).expanduser().resolve()
EXT = r"\.(?:py|sh|swift|md|json|ya?ml|tsx?|jsx?|mjs|css|html|plist|sb|toml|txt|entitlements)"
TICKED = re.compile(r"`([^`\s]+)`")
BARE = re.compile(r"(?<![\w/.~-])((?:~/)?[\w.-]+(?:/[\w.-]+)+" + EXT + r")\b")
LOOKS_LIKE_FILE = re.compile(r"^[~/\w][^<>*{}$=:()\[\]|]*" + EXT + r"$")
REAL_ABSOLUTE = ("/Users/", "/opt/", "/usr/", "/Library/", "/System/", "/etc/")


def repos() -> list:
    """The store's repos: one answer for the whole kit (slim.repos)."""
    return slim.repos()


def tracked(repo: str) -> set:
    r = subprocess.run(["git", "-C", repo, "ls-files"], capture_output=True, text=True)
    return set(r.stdout.splitlines())


def exists(token: str, roots: list, files: set) -> bool:
    if token.startswith("~") or token.startswith("/"):
        if token.startswith("/") and not token.startswith(REAL_ABSOLUTE):
            return True  # an API route such as /api/health, not a file on disk
        return Path(token).expanduser().exists()
    # The parent folder too: a shared store's notes often write web/server.py.
    if any((Path(r) / token).exists() or (Path(r).parent / token).exists() for r in roots):
        return True
    return any(f == token or f.endswith("/" + token) for f in files)


def candidates() -> list:
    roots = repos()
    files = set().union(*(tracked(r) for r in roots))
    hits = []
    for path in sorted((MEM / "project").glob("*.md")):
        for number, line in enumerate(path.read_text(errors="ignore").splitlines(), 1):
            tokens = {t for t in TICKED.findall(line) if LOOKS_LIKE_FILE.match(t)} | set(BARE.findall(line))
            for token in sorted(tokens):
                if not exists(token, roots, files):
                    hits.append(f"- project/{path.name}:{number} names `{token}`, which no repo has")
    return hits


if __name__ == "__main__":
    found = candidates()
    print("\n".join(found) if found else "No memory line names a missing file.")

#!/usr/bin/env python3
"""Collect recent Claude Code transcripts for THIS project and slim them for dreaming.

  python3 scripts/collect_transcripts.py            everything since the last dream, 30 days at most
  python3 scripts/collect_transcripts.py --days 14  at least 14 days back, and never less than since the last dream

Claude Code stores sessions as JSONL under ~/.claude/projects/<slug>/ (subagent
sessions included). Entries are matched by the `cwd` recorded on each one, so work from
git worktrees of this repo counts, and so does a session started elsewhere that moved
here. Entries from outside the repo are dropped, so dreaming has the same permission
scope as the agents even when a session also touched something private.

Output: one compact .md per session in <memory>/dreams/_batch/ (git-ignored), every entry labelled
as what it is (slim.py), with credentials and whatever the store's reject.txt lists replaced, as in
the archive. A long session is several files, `<session>.md` and then `<session>.part-02-of-05.md`
and so on, read in order; only a session over slim.DREAM_CHARS is trimmed, and its files say what went.
A dream is given what it needs to judge a session, not all of it (slim.dream): a subagent's run is a
digest, and a session in which no work was done (slim.working) gets no file and no count; what the
person typed in one is in said-in-short-sessions.md. `_groups.md` says which files each analyst reads.
Previous /dream sessions and the analysts they started are skipped, so dreaming doesn't learn from
itself. A subagent's transcript is its own file, marked with its parent, and doesn't count as a session.
Only entries inside the window count, however long the session has been open.
The window starts where the last dream stopped reading (dream_due.py), so it is the one the daily
check counted, and it ends now. _timing.json holds both moments, the counts, whether they are
enough to dream on by the one rule for that (dream_due.enough), which /dream reads instead of a
rule of its own, and the store's commit at this moment. dream_finish.py makes the moment of
collection the store's last dream and records both in the dream's commit, so the next dream's
list of new lines starts here and a line written while this dream runs is still checked.
_timing.json is written last and a leftover one is removed first: it is there only when this
run finished, so nothing can take an old batch for a new one.

Beside them: _metrics.json (counts only, the baseline dreams compare against) and _stale.md
(memory lines that name files no repo has) and _integrity.md (anything written around the protocol).
Also _auto-memory/<repo-name>/, a copy of the notes Claude Code keeps by itself for each repo of this
store that no dream has read yet (own_notes.py), so the dream can check them and carry over what is
true; a note that several repos hold word for word is there once, and _health.md names every repo it
is in. And _health.md itself: the size of what every start loads, the size of each project/ file and the lines
added since the last dream. Those numbers are counted here because a model asked to count gets them wrong.

One collector runs on a store at a time: it holds the collect lock until it ends. The first thing it
writes is _owner.json: this dream now owns the batch (dream_owner.py). If another dream that is alive
owns it, this stops and says who and since when, and touches nothing. A collector that stops before the
batch is whole, by an error or because it was told to stop (SIGTERM, as when a command runs out of
time), takes its unfinished batch away. One that is killed outright leaves an owner and no
_timing.json, which the next start reads as dead at once.

No dream starts while the store has an open proposal (dream_proposal.py): this stops before it claims
anything and says which proposal waits and how to apply or dismiss it. Otherwise the batch gets
_left-open.md, the items the person left unticked in the proposals they closed, and _timing.json names
the file this dream's proposal goes to (`proposal`), a name no other proposal has. Beside them are
_proposal-template.md and _side-template.md, the two files the dream writes as it fills them in
(proposal_check.py makes them, so the shapes a dream is shown are the shapes that are checked).
"""
import argparse, datetime, json, os, re, signal
from pathlib import Path

import build_index
import check_stale
import dream_due
import dream_owner
import dream_proposal
import memcheck
import memory_context
import machine_check
import own_notes
import proposal_check
import redact
import slim
import store

ROOT = Path(__file__).resolve().parent.parent
MEM = Path(os.environ.get("AGENT_MEMORY_DIR", ROOT / "memory")).expanduser().resolve()
LIMIT = memory_context.LIMIT  # what one start may load
SPLIT_AT = 60  # a project/ file with more entries than this gets a proposed split
LISTED = 100  # new lines listed in _health.md; a dream that has more to check than this is told how many
# How dream_finish.py names the commit of a dream that wrote a proposal. One that found too little to
# dream on reviewed nothing, so it doesn't count as the last dream.
DREAM_COMMIT = r"^dream: .*-proposal\.md$"
STARTS = (("A session", {"hook_event_name": "SessionStart"}),
          ("A subagent", {"hook_event_name": "SubagentStart", "agent_type": "general-purpose"}))


def store_has_history() -> bool:
    """Is the store a git repository of its own? One that merely sits inside another repo is not."""
    top = memcheck.git(MEM, "rev-parse", "--show-toplevel")
    return top.returncode == 0 and os.path.realpath(top.stdout.strip()) == os.path.realpath(MEM)


def last_dream():
    """(commit, subject, epoch seconds) of the newest dream that wrote a proposal, or None when there has been none.

    The commit is the one its batch was collected at, which its message names: what was added after
    that, also while it ran, is new to the next dream. A dream from before messages said so has only
    its own commit, so what was added while it ran is not listed.
    """
    r = memcheck.git(MEM, "log", "-1", "--format=%H%x09%s%x09%ct%x09%b", "--grep", DREAM_COMMIT)
    if r.returncode != 0 or not r.stdout.strip():
        return None
    commit, subject, when, body = r.stdout.split("\t", 3)
    said = re.search(rf"^{dream_due.COLLECTED_AT}: ({dream_due.SHA.pattern})$", body, re.M)
    return said[1] if said else commit, subject, int(when)


def changes(patch: str) -> dict:
    """One commit's diff as {path: (lines added, lines removed)}.

    A `+++ ` row names a file only in the header of a file's diff, before its first `@@`: after that,
    a row starting with `+++ ` is an added line that begins with `++ `. git ends a name that holds a
    space with a tab.
    """
    files, path, body = {}, "", False
    for row in patch.split("\n"):  # not splitlines(): a memory line may hold U+2028
        if row.startswith("diff --git "):
            path, body = "", False
        elif not body and row.startswith("+++ "):
            name = row[4:].rstrip("\t")
            path = name[2:] if name.startswith("b/") else ""
        elif row.startswith("@@"):
            body = True
        elif body and path and row[:1] in ("+", "-"):
            files.setdefault(path, ([], []))[row[0] == "-"].append(row[1:].strip())
    return files


def added_since(commit, head="HEAD") -> list:
    """Entries added to project/ after `commit` (or ever, when it is None) and up to `head` that are still there, oldest first: (file, line, agent, session).

    Read from the store's history: memwrite.py commits a file together with its CHANGELOG.md row, so the
    row in the same commit says which agent and session wrote the line. A line that came in with no row
    was written around the protocol, and has no session. A line a commit both removes and adds is one it
    only touched (a last line that had no newline is rewritten by whoever appends next), so it stays with
    its first writer.
    """
    r = memcheck.git(MEM, "-c", "core.quotePath=false", "log", "--reverse", "-p", "--unified=0", "--no-color", "--format=%x01",
                     f"{commit}..{head}" if commit else head, "--", "project", "CHANGELOG.md")
    if r.returncode != 0:
        raise SystemExit(f"Can't read the store's history to list the lines added since the last dream: {r.stderr.strip()[:200]}")
    found = {}
    for patch in r.stdout.split("\x01"):
        files, writers = changes(patch), {}
        for row in files.get("CHANGELOG.md", ([], []))[0]:
            cells = [c.strip() for c in row.strip("|").split("|")]
            if len(cells) == 6:
                writers[cells[1]] = (cells[2], cells[3])
        for path, (plus, minus) in files.items():
            if not (path.startswith("project/") and path.endswith(".md")):
                continue
            for line in plus:
                if build_index.ENTRY.match(line) and line not in minus:
                    found.pop((path, line), None)  # added again later: the later writer is the one to ask
                    found[(path, line)] = writers.get(path, ("", ""))
    now = {}
    for path in {p for p, _ in found}:
        file = MEM / path
        now[path] = {l.strip() for l in file.read_text(errors="ignore").split("\n")} if file.exists() else set()
    return [(path, line, agent, session) for (path, line), (agent, session) in found.items() if line in now[path]]


def plural(n: int, one: str, many: str) -> str:
    return f"{n:,} {one if n == 1 else many}"


def pins() -> list:
    """The part of _health.md a dream reads before it proposes a pin or takes one off: what is pinned, the rule and the item's form."""
    lines = [line for path in sorted((MEM / "project").glob("*.md")) for line in build_index.lines(path)]
    pinned = [line for line in lines if line.pinned]
    out = ["## Pins", f"- Pinned: {len(pinned)} of at most {build_index.PINS}. A pinned line ends in ` (pin)` and is in the index of every session, first, with its cure."]
    out += [f"  - project/{line.file}: {build_index.title(line.text)}" for line in pinned]
    out += ["- Propose a pin when the batch shows a line is worth that place: the failure it prevents came back in two or more sessions, or cost many turns "
            "when it did. Give the numbers in `why:`.",
            f"- Take a pin off when the line is no longer true, or when {build_index.PINS} are pinned and a candidate has stronger numbers from the batch "
            "(recurrence and cost) than the weakest pinned line. A pinned line with no failure in the batch may be working: silence is not a reason to unpin.",
            "- The item is an `edit` that changes nothing but the pin: its line is the line as it is with ` (pin)` added at the end, after any `(checked: …)`, "
            "or with it taken off, and its `replaces:` is the line as it is: `- [ ] <n>. project/<file>.md — edit — \"<the line> (pin)\"`, then `why:`, then "
            "`replaces: \"<the line>\"`. It adds and removes no entry, so it is not part of the Summary's net change. A correction of the same line is an "
            "item of its own, in either order.",
            "- Every other edit keeps a line pinned or unpinned as it was, and any item can name a pinned line with or without ` (pin)`. "
            "In a merge the merged line stays pinned if one of the lines it stands for was.",
            "- When you consider merges, read the ` · distinct: ` reasons in `memory/CHANGELOG.md`: a line that was written past the hold on lines alike, "
            "with a reason that doesn't hold up, is a candidate."]
    return out


def health(batch: Path, notes: list, head: str) -> str:
    """_health.md: every number the dream's Summary and its size rules need."""
    out = ["# Memory health (counted by scripts/collect_transcripts.py: use these numbers, don't count again)", "",
           f"## Loaded at every start (the limit is {LIMIT:,} characters)"]
    stored = (MEM / memory_context.STEERING).exists()
    for label, event in STARTS:  # the same code that does the loading, so this can't say less than a session gets
        found = memory_context.parts(event, store.run_for(ROOT, MEM), MEM)
        if found is None:
            raise SystemExit(f"ORG.md or INDEX.md is missing from {MEM}, so there is nothing a session could load.")
        note, org, steer, index = found[1:]
        total = memory_context.wanted(note, org, steer, index)
        out.append(f"- {label}: {total:,} characters, {round(100 * total / LIMIT)}% of the limit (start note {len(note):,}, ORG.md {len(org):,}, "
                   f"Steering {'from STEERING.md' if stored else 'of this repo'} {len(steer):,}, INDEX.md {len(index):,})")
        if total > LIMIT:
            out.append(f"- {label} is over the limit by {total - LIMIT:,} characters: something gives way, the oldest titles of the index first, and the start note says what.")
    # The last start counted is a subagent's; what the person has not written is the same for both.
    if not org:
        out.append("- ORG.md is still as the kit shipped it, so no session is given it. Draft it for the person, under \"ORG.md and Steering (yours to change)\": "
                   "at most six `add` items (`- Product: …`, `- Quality bar: …`, `- Style: …`, `- Never: …`), each from the person's own words in `**user**:` "
                   "entries, quoted in `evidence:` with the session. Write nothing the sessions don't show, and don't propose deleting the placeholder lines: "
                   "they stop loading once the person has a line of their own.")
    if not steer and stored:
        out.append("- STEERING.md says nothing yet for this repo, so no session is given Steering. Draft it the same way, in the same section: "
                   "`add` items for what mattered in this project's sessions (`- Matters here: …`) and what didn't (`- Doesn't matter: …`).")
    elif not steer:
        out.append("- This repo's Steering is still the kit's default, or it has none, so no session is given it. Draft it the same way, in the same section: "
                   "`add` items for what mattered in this project's sessions (`- Matters here: …`) and what didn't (`- Doesn't matter: …`).")
    out += ["", "## project/ files"]
    files = sorted((MEM / "project").glob("*.md"))
    for path in files:
        n = len(build_index.entries(path))
        out.append(f"- project/{path.name}: {plural(n, 'entry', 'entries')}, {path.stat().st_size:,} bytes"
                   + (f". Over {SPLIT_AT} entries: propose one split." if n > SPLIT_AT else ""))
    out.append(f"- In all: {plural(sum(len(build_index.entries(p)) for p in files), 'entry', 'entries')}" if files else "None.")
    out += [""] + pins()
    dream = last_dream()
    if dream:
        when = datetime.datetime.fromtimestamp(dream[2]).isoformat(sep=" ", timespec="minutes")
        out += ["", f"## Lines added since the last dream ({dream[1]}, {when}), newest first"]
    else:
        out += ["", "## Lines added since the last dream (there has been none, so every line ever added), newest first"]
    added = added_since(dream[0] if dream else None, head)[::-1]
    for path, line, agent, session in added[:LISTED]:
        if not session:
            where = "no session: written around the protocol"
        else:
            where = f"session {session} — agent {agent} — " + (f"transcript: {session}.md" if (batch / f"{session}.md").exists() else "transcript: not in this batch")
        out.append(f"- {path} — {where} — \"{line}\"")
    if len(added) > LISTED:
        out.append(f"- and {plural(len(added) - LISTED, 'older line', 'older lines')}, not listed and not checked by this dream")
    if not added:
        out.append("None.")
    out += ["", "## Claude's own notes (auto memory)"]
    for row in notes:
        if row["files"]:
            out.append(f"- _auto-memory/{row['folder']}/ — from {row['repo']} — {plural(row['files'], 'file', 'files')}, {row['chars']:,} characters")
        out += [f"- _auto-memory/{given} — also {file} in {row['repo']} — one note, given once: check it against every repo it is in" for file, given in row["same"]]
        if row["read"]:
            out.append(f"- already read by an earlier dream and unchanged, so left out: {plural(row['read'], 'note', 'notes')} in {row['repo']}")
        if row["earlier"]:
            out.append(f"- of those, read by a dream that kept only what every agent needs and a repo file confirms, so facts may be left behind: "
                       f"{plural(row['earlier'], 'note', 'notes')} in {row['repo']}. Needs the person: `python3 scripts/own_notes.py reread` has the next dream read them again")
        if row["off"]:
            out.append(f"- auto memory off in {row['repo']}: {plural(row['off'], 'note', 'notes')} not read")
        out += [f"- left out in {row['repo']}: {why}" for why in row["skipped"]]
    if notes:
        copies = sum(len(row["same"]) for row in notes)
        out.append(f"- In all: {plural(sum(row['files'] for row in notes), 'note', 'notes')} to read"
                   + (f", and {plural(copies, 'copy', 'copies')} of them in other places, not given again" if copies else ""))
    else:
        out.append("None.")
    return "\n".join(out) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, help="read at least this many days back (never less than since the last dream)")
    ap.add_argument("--out", default=str(MEM / "dreams" / "_batch"))
    a = ap.parse_args()
    out = Path(a.out)
    signal.signal(signal.SIGTERM, told_to_stop)
    waiting = dream_proposal.open_one(MEM)
    if waiting:
        raise SystemExit(f"{dream_proposal.waiting_words(waiting)} Nothing was collected.")
    if not dream_owner.named(dream_owner.me()):  # before anything is claimed: its own finish could never show the batch is its own
        raise SystemExit(f"{dream_owner.NAMELESS} Nothing was collected.")
    lock = dream_owner.take_collect_lock()  # held until this process ends, however it ends
    if lock is None:
        kept = dream_owner.holder(out)
        raise SystemExit((dream_due.running_words(kept) if kept else f"A collector is already running for {MEM.name}.") + " Stop here: the batch belongs to that dream.")
    stop_if_held(dream_owner.holder(out, collecting=True), out)
    base = Path.home() / ".claude" / "projects"
    if not base.exists():
        raise SystemExit(f"No Claude Code transcripts at {base}")
    if not store_has_history():  # before anything is claimed, sealed, slimmed or cleared
        raise SystemExit(f"{MEM} is not a git repository of its own, so nothing in it is versioned and a dream can't tell "
                         "what was added since the last one. Nothing was collected.")
    timing = dream_due.timing()  # before anything is claimed or cleared: a state file that can't be trusted stops it here
    # Only now, with every check passed: the batch becomes this dream's, and what the dream before left that could pass
    # for a finished collection goes (its stamp, and the proposal it wrote and never finished).
    again = dream_owner.mine(out)  # this dream collecting over its own batch: what it has asked of this machine stays asked
    kept, unfinished = dream_owner.claim(out)
    stop_if_held(kept, out)
    if unfinished:
        print(f"Removed {unfinished}: the dream that had this batch wrote it and never finished, so it was never checked or committed.")
    for name in dream_proposal.orphans(MEM):
        print(f"Left alone: dreams/{name}, a proposal no dream finished and no record knows. `python3 scripts/memcheck.py` says how to remove it.")
    try:
        collect(a, out, base, timing, again)
    except BaseException:  # whatever stopped it, an exit or SIGTERM included: a batch that isn't whole is taken away, so nobody waits for it
        dream_owner.give_up(out)
        raise
    os.close(lock)


def told_to_stop(signum, frame):
    raise SystemExit("The collector was told to stop (SIGTERM) before the batch was whole. Its batch was taken away; start the dream again.")


def stop_if_held(kept, out: Path):
    """Stop when a dream that is alive, and is not this one, has the batch."""
    if kept and not (kept["owner"] and dream_owner.mine(out)):
        raise SystemExit(f"{dream_due.running_words(kept)} Stop here: the batch belongs to that dream.")


def collect(a, out: Path, base: Path, timing: dict, again: bool = False):
    """Fill the batch this dream now owns. `again`: it owned the batch already, and collects over it."""
    # --days can only widen the window. A narrower one would leave sessions that no later dream reads.
    cutoff = timing["since"] if a.days is None else min(timing["since"], timing["collected"] - a.days * 86400)
    answers = out / machine_check.ANSWERS  # a dream gets its answers about this machine once: collecting again does not give it more
    asked = answers.read_bytes() if again and answers.is_file() else None
    dream_owner.empty(out)  # whatever a crashed run left must not leak into this one
    if asked is not None:
        out.mkdir(parents=True, exist_ok=True)
        answers.write_bytes(asked)
    sealed = memcheck.seal(MEM)
    broken = memcheck.problems(MEM)
    memo = {}
    rs = slim.roots()
    clean = redact.scrubber(MEM, redact.rules(MEM), redact.Tally())  # as in the archive: nothing the store refuses is in the batch
    batch, idle = [], []
    n = skipped = kit = 0
    totals = dict(sessions=0, subagent_runs=0, tool_calls=0, tool_errors=0, human_messages=0, assistant_turns=0, input_tokens=0,
                  cache_read_tokens=0, output_tokens=0, sessions_using_memory_files=0)
    for f in sorted(base.rglob("*.jsonl")):
        if not f.is_file() or f.stat().st_mtime < cutoff:
            continue
        if slim.is_dream(f, memo):
            skipped += 1
            continue
        parent = slim.parent_of(f)
        s = slim.read([f], rs, cutoff, clean)
        if not s["entries"]:
            continue
        if s["kit_only"]:  # inside the repos it only worked on the memory kit: not this project's session, nor what was said in it
            kit += 1
            continue
        if not parent and not slim.working(s):  # someone checking that Claude answers: no file, no count
            idle.append((f.stem, s["entries"]))
            continue
        for key, value in s["count"].items():
            totals[key] += value
        totals["subagent_runs" if parent else "sessions"] += 1
        totals["tool_errors"] += s["errors"]
        totals["human_messages"] += len(s["humans"])
        totals["sessions_using_memory_files"] += s["used_memory"]
        n += 1
        head = f"# {'subagent of ' + parent if parent else 'session ' + f.stem}\nsource: {f}\ntool_errors: {s['errors']}\n"
        batch.append((f.stem, parent, slim.store(out, f"{parent + '--' if parent else ''}{f.stem}", head, s["entries"], bool(parent))))
    slim.finish(out, batch, idle)
    # Before _metrics.json, which marks a batch as complete: if the history can't be read, the next run starts clean.
    notes = own_notes.copy(slim.repos(), out, MEM)
    head = memcheck.git(MEM, "rev-parse", "HEAD").stdout.strip()  # after the seal: what is in the store up to here, this dream lists
    if not dream_due.SHA.fullmatch(head):
        raise SystemExit(f"Can't tell which commit {MEM} is at, so the next dream couldn't tell what is new. Nothing more was collected.")
    (out / "_health.md").write_text(health(out, notes, head))
    calls = totals["tool_calls"]
    totals.update(date=datetime.date.today().isoformat(), days=round((timing["collected"] - cutoff) / 86400, 2),
                  errors_per_100_tool_calls=round(100 * totals["tool_errors"] / calls, 1) if calls else 0)
    (out / "_metrics.json").write_text(json.dumps(totals, indent=1))
    stale = check_stale.candidates()
    (out / "_stale.md").write_text("# Memory lines naming files no repo has (candidates, check each)\n\n"
                                   + ("\n".join(stale) if stale else "None.") + "\n")
    (out / "_integrity.md").write_text("# Written around the protocol (check each one)\n\n" + ("\n".join(f"- {b}" for b in broken) if broken else "None.") + "\n")
    (out / dream_proposal.HANDOVER).write_text(dream_proposal.handover(MEM))
    name = dream_proposal.name_for(MEM, timing["collected"])
    # What the dream fills in: the two files it writes, with their names, sections, item shapes and limits, from the script that checks them.
    (out / proposal_check.PROPOSAL_TEMPLATE).write_text(proposal_check.proposal_template(name))
    (out / proposal_check.SIDE_TEMPLATE).write_text(proposal_check.side_template(name))
    stamp = json.dumps(dict(timing, head=head, proposal=name), indent=1) + "\n"
    if not dream_owner.whole(out, stamp):  # last: the stamp says this run finished. Asked under the store's lock: the person may have released the batch
        raise SystemExit("While this was collecting, the batch was released or taken over. Stop here: start the dream again.")
    print(f"{totals['sessions']} sessions and {totals['subagent_runs']} subagent runs → {out} ({skipped} dream files skipped, {kit} that only worked on the memory kit, "
          f"{len(stale)} stale candidates, {len(broken)} integrity problems, {sum(row['files'] for row in notes)} of Claude's own notes copied, "
          f"sealed: {', '.join(sealed) or 'nothing'})")
    print(timing["line"])


if __name__ == "__main__":
    main()

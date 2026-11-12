#!/usr/bin/env python3
"""The last step of a dream: record the metrics, clear the batch, commit, tell the human.

  python3 scripts/dream_finish.py          after a proposal was written
  python3 scripts/dream_finish.py --none   when there was too little signal to dream on

A script rather than prompt steps, so an unattended run can finish with one allowed command.
When a proposal was written, and only then, the time up to which the dream read (the moment
its batch was collected) and the reason it started (AGENT_MEMORY_DREAM_REASON, set by
run-dream.sh) go into the store's state file: dream_due.py counts from that time. A run that
proposed nothing leaves it, so what it didn't read still counts. Either way a dream the daily
check started is marked there with how it ended.
A proposal that holds what the store never keeps (proposal_check.py) is refused before anything
is written: nothing is committed, the batch stays, and the run has not finished. The same when
the state file can't be read or written: that is checked before the commit, so a finish does
everything or nothing. The dream's commit says where and when its batch was collected.
If git can't commit, the dream has not finished: what this wrote goes back, the batch stays, nothing is
recorded, and it exits with a plain message, so run-dream.sh alerts and a second run completes it.
A proposal is finished only by the dream that wrote it: the batch must be this dream's own, and the
proposal is the file that batch names (own_proposal). It is committed as the store's open proposal
(dream_proposal.py), together with the file beside it that holds what the dream read and did not
propose (dream_proposal.side_file), when the dream wrote one. The .finished marker is written only inside an unattended run and holds that
run's name, which is what run-dream.sh looks for.
A dream finishes only the batch it owns (dream_owner.py). One that showed no sign of life for so
long that another dream took the batch over is told who has it now, and nothing is cleared or committed.
Alerting is per store: an executable <store>/notify.sh is called with a title and a body. They
carry only the store name and the counts, never transcript content.
"""
import argparse, json, os, re, shutil, subprocess, sys
from pathlib import Path

import build_index
import dream_due
import dream_owner
import dream_proposal
import memcheck
import needs
import own_notes
import proposal_check

ROOT = Path(__file__).resolve().parent.parent
MEM = Path(os.environ.get("AGENT_MEMORY_DIR", ROOT / "memory")).expanduser().resolve()
DREAMS = MEM / "dreams"
BATCH = DREAMS / "_batch"
HISTORY = DREAMS / "metrics.jsonl"


def taken_words(owner: dict) -> str:
    """For a dream that finds the batch is another dream's: who has it, and, if this dream was the one replaced, exactly what happened."""
    said = f"The batch in {BATCH} belongs to another dream: {dream_owner.who(owner)}, since {dream_due.clock(owner['since'])}. "
    was = owner["replaced"]
    if dream_owner.replaced_me(owner):
        quiet = round((owner["since"] - was["heard"]) / 60)
        return said + (f"This dream collected on {dream_due.clock(was['since'])} and was last heard from on {dream_due.clock(was['heard'])}. "
                 f"The other dream started {quiet} minutes after that and took the store over, because a dream that shows no sign of life for "
                 f"{dream_owner.SILENT // 60} minutes counts as dead: most likely this one was waiting for an answer. Its batch is gone, so it can't finish, "
                 "and what its analysts found has to be found again. Nothing in memory is lost: the other dream reads the same sessions. "
                       "Nothing was committed and nothing was cleared. Stop here: a dream finishes only the batch it collected.")
    return said + ("Nothing was committed and nothing was cleared. Stop here: a dream finishes only the batch it collected. "
                   f"If that dream is dead, its person can give it up with `{dream_owner.RELEASE}`.")


def own_proposal():
    """(stamp, path) of the proposal this dream wrote, or a stop.

    A finish belongs to one dream: the batch must be the one this dream collected and still owns, and the
    proposal must be the file that batch names, which no dream before it could have written. So a dream that
    wrote nothing can't finish on an earlier dream's proposal, move the last dream and mark notes read.
    """
    stamp = dream_due.stamp(BATCH)
    if not dream_owner.mine(BATCH):
        sys.exit(f"The batch in {BATCH} has no owner, so there is no telling which dream collected it. Nothing was committed. "
                 "Start the dream again from its first step.")
    proposal = DREAMS / stamp["proposal"]
    if not proposal.is_file():
        sys.exit(f"This dream's proposal is not there: write it to {proposal} (the name is `proposal` in the batch's _timing.json), or pass --none. The batch was kept.")
    return stamp, proposal


def record_metrics():
    source = BATCH / "_metrics.json"
    if not source.exists():
        return
    row = json.loads(source.read_text())
    old = HISTORY.read_text().splitlines() if HISTORY.exists() else []
    keep = [line for line in old if json.loads(line).get("date") != row.get("date")]
    HISTORY.write_text("\n".join(keep + [json.dumps(row)]) + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--none", action="store_true")
    a = ap.parse_args()
    dream_due.ready()  # before anything is written or committed: a state file that can't be used stops the finish whole
    # From "is this batch mine" to the commit under one hold of the store's lock: a dream that takes the batch over
    # claims it under the same lock, so it can't come between the answer and the clearing.
    with dream_owner.store_locked():
        owner = dream_owner.read(BATCH)
        if owner is not None and not dream_owner.mine(BATCH):
            sys.exit(taken_words(owner))
        older = dream_owner.older_kit(BATCH) if owner is None else None
        if older:  # no owner written, and fresh: a dream an older kit collected. Not this dream's to clear either
            sys.exit(f"{dream_due.running_words(older)} Nothing was committed and nothing was cleared.")
        stamp, proposal = (None, None) if a.none else own_proposal()  # before the batch is cleared
        if proposal:
            stop = proposal_check.refusal(proposal)
            if stop:
                sys.exit(stop)
            waiting = dream_proposal.open_one(MEM)
            if waiting:  # the collector stops on this too; here for a proposal that was opened since
                sys.exit(f"{dream_proposal.waiting_words(waiting)} Nothing was committed. The batch was kept.")
        # What this finish writes besides the proposal itself. If the commit fails they go back to the bytes they held:
        # a dream whose proposal is not committed has not finished, and a second run of this finishes it with nothing doubled.
        records = ["INDEX.md"] + ([str(dream_proposal.LEDGER), str(own_notes.RECORD), str(needs.FILE), "dreams/metrics.jsonl"] if proposal else [])
        held = {path: (MEM / path).read_bytes() if (MEM / path).exists() else None for path in records}
        if proposal:
            record_metrics()
            own_notes.mark_read(MEM, proposal.name)  # last before the commit: a note counts as read only once nothing else can stop this run
            dream_proposal.opened(MEM, proposal.name, stamp["collected"])
            # What the proposal says needs the person is on record from now on, whatever becomes of the proposal (needs.py).
            needs.record(MEM, proposal.name, stamp["collected"], [line for _, line in dream_proposal.section(proposal.read_text(errors="ignore"), needs.HEADING)])
        build_index.write(MEM)
        message = f"dream: {proposal.name}\n\n{dream_due.trailers(stamp)}" if proposal else "dream: no proposal"
        # Only what this finish wrote. Anything else in dreams/ (a proposal some dream left unfinished) is not this dream's to commit.
        own = ([f"dreams/{proposal.name}", f"dreams/{dream_proposal.side_file(proposal.name)}"] if proposal else []) + records
        error = memcheck.commit(MEM, message, [path for path in own if (MEM / path).exists()])
        if error:
            for path, was in held.items():
                if was is None:
                    (MEM / path).unlink(missing_ok=True)
                else:
                    (MEM / path).write_bytes(was)
            memcheck.git(MEM, "reset", "-q", "--", *own)  # what the failed commit may have staged; the files hold what is committed again
            sys.exit((f"The proposal {proposal.name} could not be committed" if proposal else "The end of this dream could not be committed")
                     + f" ({error}), so this dream has not finished: nothing was recorded, no note was marked read, and the batch was kept. "
                     "Run this again once git works. If no git command is running in the store, a git that was killed left "
                     "memory/.git/index.lock behind: delete that file.")
        shutil.rmtree(BATCH, ignore_errors=True)  # only now: the batch is what a second run would need
    state = Path.home() / ".local" / "state" / "agent-memory"
    state.mkdir(parents=True, exist_ok=True)
    # Before the marker: if the state can't be written, the run has not finished and run-dream.sh says so.
    try:
        dream_due.finished(stamp["collected"] if stamp else None)
    except SystemExit as stop:
        if not proposal:
            raise
        # It could be written a moment ago (ready), so this is rare: a full disk, a file changed meanwhile.
        sys.exit(f"The proposal {proposal.name} is committed, but the state file could not be updated: {stop}\n"
                 f"To put it right: fix what stops {dream_due.state_file()} being written, or delete that file. "
                 "The next check then takes the last dream from this proposal's commit. The proposal itself is complete: open it and tick as usual.")
    run = dream_owner.me()["run"]
    if run:  # the marker is for run-dream.sh and names its run, so no other finish can make that run look finished
        (state / f"{MEM.name}.finished").write_text(run)
    if proposal:
        # The first line is agent-written, so only its numbers leave the machine.
        counts = re.search(r"(\d+) sessions?\D+(\d+) proposed", proposal.read_text(errors="ignore").splitlines()[0])
        headline = f"{counts[1]} sessions, {counts[2]} proposed changes" if counts else "Proposal written"
        notify = MEM / "notify.sh"
        if os.access(notify, os.X_OK):
            r = subprocess.run([str(notify), f"Dream ready: {MEM.name}", headline], capture_output=True, text=True)
            print("alert sent" if r.returncode == 0 else f"WARN: alert failed: {r.stderr.strip()[:200]}")
        print(f"finished: {proposal}")
    else:
        print("finished: no proposal")


if __name__ == "__main__":
    main()

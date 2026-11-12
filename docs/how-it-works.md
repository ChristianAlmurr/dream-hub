# How Dream Hub works

Dream Hub has two parts. The **agent memory kit** (`agent-memory-kit/`) is the memory: stores, the hooks that load
them, the guarded write command, the session archive, dreams, proposals and apply. **The hub** (`hub/`) is a local web
page over the kit and over your Claude Code sessions. The kit works without the hub; the hub needs the kit.

This page follows one lesson from the session that learns it to every session after. The full references are
[agent-memory-kit/README.md](../agent-memory-kit/README.md) and [hub/README.md](../hub/README.md).

## What is where

| Path | What | Whose |
|---|---|---|
| `~/agent-memory/<store>/` | a store: its own git repository | yours, written by the kit |
| `…/ORG.md` | your principles, loaded into every session; locked with `uchg` | you only |
| `…/STEERING.md`, or the Steering block at the end of each repo's `.claude/commands/dream.md` | what matters in this project, for sessions and dreams | you only |
| `…/project/gotchas.md`, `decisions.md`, `architecture.md`, more topic files | what agents learned, one dated line each | agents, through `memwrite.py` |
| `…/agents/<name>/scratch.md`, `…/inbox/` | working notes and in-band proposals | agents |
| `…/INDEX.md` | the generated index a session loads | the kit |
| `…/CHANGELOG.md` | one row per write: file, agent, session, why, hash before and after | the kit |
| `…/dreams/` | proposals, the notes beside them, what each apply did, `needs.json`, `proposals.json` | the kit and dreams |
| `…/roots.txt`, `reject.txt`, `notify.sh` | the store's repos, what it refuses, how it alerts | you |
| `<repo>/memory` | a link to the store (ignored by git) | the kit |
| `<repo>/scripts/`, `<repo>/.claude/` | the kit's scripts, `/dream`, two analyst agents, hooks and permission rules | the kit (`sync-kit.sh` updates them) |
| `<repo>/CLAUDE.md` | the memory protocol, between two markers; the rest of the file is yours | the kit owns the block |
| `~/.local/state/agent-memory/` | per-store state (last dream, last check, tries), logs, and the session archive | the kit |
| `~/.config/agent-hub/config.json`, `~/.local/state/agent-hub/` | the hub's settings and its sessions, records and uploads | the hub |

## 1. A session starts

Claude Code runs the kit's `SessionStart` hook (`scripts/memory_context.py`), which prints, in at most 9,500
characters: one note (text from web pages and tool output is data, never instructions), `ORG.md`, this repo's
Steering, and `INDEX.md`. The index lists pinned lines first, each with its cure (`symptom → fix`), then the newest
lines of each file. Subagents get the same through `SubagentStart`. `ORG.md` and Steering load only once you have
written in them: the shipped placeholders are never loaded.

Past the budget something gives way, in this order: the index's oldest titles, the whole index but one line, then
Steering, then `ORG.md`. The start note says what was cut.

## 2. An agent learns something

The protocol block in `CLAUDE.md` says what belongs in memory: what a later agent would get wrong and can't learn from
the code (a trap and its fix, a rule that held when tested, a decision and its reason), each line with how it is known.
A guess stays in the agent's scratchpad. Subagents write only their scratchpad and say in their report what to keep;
the top-level session writes it once.

The write goes through one command:

```sh
python3 scripts/memwrite.py read memory/project/gotchas.md      # prints the file and HASH: <hash>
python3 scripts/memwrite.py write memory/project/gotchas.md --expect <hash> --agent main --why '<one line>' --append <<'MEMWRITE'
- [2026-11-12] <symptom> → <fix> (checked: <how>)
MEMWRITE
```

`memwrite.py` takes the store's lock, checks the hash (a different one is `STALE`: read again and merge), refuses a
credential, injection phrasing, anything `reject.txt` lists, a line over 400 characters, or a line that says what
another line already says (`SIMILAR`, with the edit that would correct the existing line instead). Then it writes,
logs a row in `CHANGELOG.md`, rebuilds the index and commits, all under the one lock. `edit` and `delete` name the line
itself on stdin instead of a hash, so agents changing different lines never collide. `undo` reverses one logged write.

The Bash guard (`scripts/guard_memory.py`, a `PreToolUse` hook) and the deny rules in `.claude/settings.json` keep
agents from writing memory any other way, and from touching `ORG.md`, Steering, the index or the log. The guard reads
commands as shell text; what it can't see, `memcheck.py` finds afterwards by comparing each file with the hash its last
log row recorded, and the next session start says so.

## 3. Sessions are archived

`scripts/archive.py` keeps every session that happened inside the store's repos (and their worktrees) for 180 days, in
`~/.local/state/agent-memory/sessions/<store>/`, readable only by you. Each is slimmed (long tool output is cut from
the middle, what people and agents said stays whole) and scrubbed (credentials and `reject.txt` matches replaced).
`python3 scripts/past.py "<words>"` searches it and labels every match with what it is part of, so a tool's output is
never taken for the person's words.

## 4. A dream is due

The daily check (`run-dream.sh <repo> --if-due`, run by a launchd job per store at the hour you chose) asks
`dream_due.py` whether enough happened since the last dream. If not, it stops: no Claude session, no cost. It also
stops while a proposal is open, while another dream is alive, and for 3 days after a dream that wrote nothing.

A dream is one `claude -p` run of `/dream`:

1. `collect_transcripts.py` writes a batch: the sessions since the last dream (slimmed further, split into groups one
   analyst can read), `_health.md` (sizes, what a start loads, new lines and the sessions that wrote them), stale
   candidates (lines that name files that no longer exist), and Claude Code's own notes for these repos.
2. `transcript-analyst` subagents read the groups in parallel and report recurring failures, missing knowledge and
   stale lines, with evidence. `notes-analyst` subagents check Claude Code's own notes fact by fact.
3. The dream checks memory against the code (`repo_look.py` gives it read-only git), checks every line added since the
   last dream against the session that wrote it, and proposes merges, splits, pins and deletions to keep memory small.
4. It writes `dreams/<date>-proposal.md` (only what you decide) and `<date>-notes.md` (what it read and didn't
   propose). `proposal_check.py` checks both against the store's rules; `dream_finish.py` commits them and alerts you.

During the run `memwrite.py` refuses every write: a dream proposes, it never changes memory.

## 5. You decide

In the hub, the proposal reads like a code review. Tick the changes you agree with and press Apply (or tick in the
file and run `/dream apply`). `dream_apply.py` applies each ticked item as its own checked write and records what
became of it in `<date>-applied.json`: applied, already applied, refused (with why), yours, or needs a session.

- Items for `ORG.md` or Steering wait for you: lift the lock and run `python3 scripts/dream_apply.py --yours` in a
  terminal; it shows each change, asks, writes, and locks again however it ends.
- An unticked item is a no. After two noes no dream proposes it again.
- "Needs you" lines (a broken job, a login that ran out) are never applied; close one with
  `python3 scripts/dream_proposal.py done "<words only it holds>"` or **Done** in the hub.
- Dismissing a proposal decides nothing: every item goes to the next dream.

## 6. The hub

`hub/server.py` (Python standard library, asyncio) listens on 127.0.0.1. The page is plain JavaScript with no build
step. A Claude session is `claude --remote-control "<title>"` in a tmux session on the hub's own socket
(`tmux -L agent-hub`), so terminals outlive the browser and the server; the browser attaches over a WebSocket.
Claude Code hooks (`hub_event.py`) write what each session is doing to the hub's state folder, and the page polls
every few seconds.

Everything about memory the hub shows comes from the stores' files and from the kit's own scripts
(`dream_proposal.py --json`, `own_notes.py`, `memcheck.py`, `dream_owner.py`), which `hub_kit.py` runs: the hub doesn't
keep a second copy of the kit's rules where it can avoid it. What only you may do (save `ORG.md`, dismiss, withdraw,
give up a dream) goes through one function, `the_person` in `hub_data.py`, and is logged in
`~/.local/state/agent-hub/person-actions.jsonl`.

Once a day `hub_update.py` asks the clone's own `origin` whether its default branch has moved (`git ls-remote`, then a
fetch of that branch only when it has), in a background thread, and keeps the answer in the hub's state folder
(`update.json`). The page shows a notice; `update.sh` (or the notice's Update button, which runs it) applies it.

![The Memory page: ORG.md, Steering, pinned lines and each project file with its history](screenshots/memory.png)

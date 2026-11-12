# One kit, nothing in repos: design and migration

The plan to replace the model where the kit's files are copied into every repo and committed there.
It follows a review of the kit as it ran on its author's Mac and a design pass checked against the
Claude Code docs and experiments with Claude Code 2.1.322. Steps 1 and 3 are built (sections below);
the rest is planned. The [roadmap in the top README](../../README.md#roadmap) says where each step stands.

## Why

- Version drift: in a repo with a dozen worktrees, most had no kit scripts and some had old ones; another
  repo's newest kit commit was only on a feature branch.
- Leakage: kit files end up committed in repos that aren't only yours (a shared company monorepo, a
  partner's repository), held back by discipline alone.
- Every kit change needs a sync into every repo and a commit in each.
- Allow rules match command text literally: `python3 scripts/memwrite.py …` is allowed, the same call
  with an absolute path, `./` or an env var in front is refused (or goes to the auto-mode classifier).
- No machine tier, one launchd job per store, the hub re-implementing the kit's state rules.

## Target

- **Packaging.** The kit is one Claude Code plugin named `agent-memory`, loaded in place from
  `~/tools/agent-memory-kit` (local marketplace, source `./`), installed once at user scope.
- **Entry point.** One executable, `bin/amk`, fronts the existing `scripts/*.py`. It finds the kit
  from its own path (`$0`): `CLAUDE_PLUGIN_ROOT` is unset inside the Bash tool. It resolves the
  store, sets `AGENT_MEMORY_DIR`, and runs the script. Agents call `amk <verb>`: one spelling.
- **Finding the store.** `git config agent-memory.store <name>` in the repo's shared git dir. The key
  is also the switch that turns the kit on for a repo. Lookup order in `scripts/store.py`:
  `AGENT_MEMORY_DIR`; `git config` from the working directory; for a folder with no `.git`, walk up to
  `.jj`, follow `.jj/repo` (a folder, or a file holding a relative path to the main repo's `.jj/repo`)
  and read the same key from the git repository jj names in `<that>/store/git_target` (`git`, so
  `<that>/store/git`, when jj keeps the repository itself); else no store. `roots.txt` stays as the reverse map
  (store → repos), read through one function. `amk attach <store> <repo>` writes both; the health
  check compares them. While a repository still has the older layout (a `memory` link, no key) a
  missing key is its normal state and no problem. It becomes one after step 10, when the links are
  gone and the key is the only tie.
- **Hooks** (`hooks/hooks.json`): `SessionStart` and `SubagentStart` each run
  `"${CLAUDE_PLUGIN_ROOT}/bin/amk" hook context --part kit` and `… --part store`; `PreToolUse` on Bash
  runs `… hook guard`. Each hook reads `cwd` from its stdin JSON (never `$CLAUDE_PROJECT_DIR`) and
  exits silently, printing nothing, when no store resolves. The 10,000-character cap is per string, so
  the two parts have separate budgets; their order is not stable, so neither may depend on the other.
  While a repo still carries the old committed hooks (its `.claude/settings.json` names
  `scripts/memory_context.py`), the plugin's context hook stays silent there.
- **Protocol text.** Delivered by the `--part kit` string (from `PROTOCOL.md`, was
  `CLAUDE.md.snippet`), only where a store resolves. No block in any repo's `CLAUDE.md`.
- **Dream command and analysts.** `skills/dream/SKILL.md` (today's `dream.md` without the Steering
  block) runs as `/agent-memory:dream`; `/dream` resolves to it and a two-line alias in
  `~/.claude/commands/dream.md` keeps the short name certain. Analysts are plugin agents
  (`agent-memory:transcript-analyst`, `agent-memory:notes-analyst`).
- **Permissions.** A plugin can't carry permission rules. Deny rules on the stores' protected files
  and allow rules for `amk <verb> *` go in `~/.claude/settings.json`, merged by `amk install` with a
  backup. `amk attach` and `amk detach` get no allow rule, in step 4 or later: which store a
  repository belongs to is the person's to change, and the Bash guard stops an agent that tries
  (those two verbs, `store.py` run directly, and a `git config` that writes the key); the skill's frontmatter carries `allowed-tools` for what a dream runs. Read access to a
  store stays per repo: `additionalDirectories` in the main checkout's untracked
  `.claude/settings.local.json` (worktrees outside the repo read it; confirmed). Rules in
  `settings.local.json` apply without the folder being trusted; rules in a committed
  `.claude/settings.json` don't.
- **Nothing committed in any repo.** One mode for owned and guest repos. Steering moves to
  `~/agent-memory/<store>/STEERING.md`, a human-tier file protected like `ORG.md`, with optional
  `## <repo>` sections; a repo's own protocol lines move there too. A repo is "guest" by a flag in the
  store: any kit path tracked in a guest repo is a health problem and alerts.
- **Machine tier.** A store `~/agent-memory/_machine` (normal layout, empty `roots.txt`). The
  `--part kit` string carries the protocol plus at most 15 machine lines, capped at 2,000 characters.
  Loaded only in projects that have a store (owner's decision). Analysts mark each finding
  `SCOPE: project | machine`; the dream lists machine findings under `## Machine`; `dream_finish.py`
  copies that section into `_machine/inbox/`. Admission needs two stores, five sessions in one store,
  or the owner's word; limit 40 entries; a line nobody hit in 60 days is proposed for deletion; every
  proposal first asks whether the fix can be code instead.
- **One scheduler.** One LaunchAgent `ai.agent-memory.scheduler` running `amk schedule run` hourly
  (`StartCalendarInterval` `{Minute: 7}`; missed intervals coalesce on wake). Each tick: a global lock
  without waiting; for each store not yet checked today and past its hour, the existing `dream_due`
  check; due dreams one after another, at most two per tick, longest-waiting first. Back-off stays in
  each store's state file. Hours live in `~/.config/agent-memory/schedule.json`
  (`{"default_hour": 18, "stores": {"shop": {"hour": 22, "enabled": true}}}`), edited through
  `amk schedule set <store> --hour N | --off`.
- **Status.** `amk status [--json]` only reads files: one record per store (repos, checked, verdict,
  last dream and reason, who is running, the open proposal and its age, tries, next try, schedule,
  health problems, what a session loads against the limit, kit commit). Heavy checks are written by
  the scheduler to `health.json`. The hub uses it and deletes its mirrored rules.
- **Bare mode.** The docs say `--bare` will become the default for `claude -p`; bare skips plugin
  hooks and can't sign in here, and no opt-out flag exists. An unattended dream must verify that its
  start hook ran (a marker per session) and alert if not.

## Migration, each step leaving every store working

| # | Step | Rollback |
|---|---|---|
| 1 | `scripts/store.py`, `bin/amk`, `amk status`; additive | revert the kit commit |
| 2 | Hub reads `amk status` | revert the hub commit |
| 3 | Scripts stop treating their own folder as a repo; `STEERING.md` in each store; the dream runner uses kit scripts with `AGENT_MEMORY_DIR`. Repo copies still work | revert |
| 4 | Plugin files and `amk install`; silent without the key | `claude plugin disable`, `amk uninstall` |
| 5 | Pilot one store: `amk attach`, `amk clean-repo`, one normal commit; two days of use | unset the key, revert the commit |
| 6 | Guest repos (repos that aren't only yours): delete their untracked kit files, links and exclude lines | re-run the old install |
| 7 | The remaining stores, the one with most worktrees first | as step 5 |
| 8 | Scheduler; boot out the per-store jobs after one clean day | re-run the old schedule script per store |
| 9 | Machine tier | remove `_machine` from the kit part |
| 10 | Remove links, post-checkout hooks and dead scripts | re-run the old link script |

Store formats don't change during the migration: old-branch copies keep writing them. History in
repos is left alone; the kit's files leave in one normal commit. Worktrees with no kit files get
memory the moment the key is set; worktrees with an old committed copy keep running it until they
merge the removal commit.

Disappear at the end: `sync-kit.sh`, `kit-files.txt`, `merge_settings.py`, `link-worktrees.sh`,
`exclude-local.sh`, `schedule-dream.sh`, `settings.snippet.json`, `CLAUDE.md.snippet`. Stay: every
`scripts/*.py`, `run-dream.sh` (as `amk dream`), `install-store.sh` (shrunk to create and attach),
`retire-auto-memory.sh`, both test suites.


## Step 1 as built

New files: `scripts/store.py`, `bin/amk`, `scripts/status.py`. Additive: no
repo, store, launchd job or hub file changes, and the scripts copied into repos work as before.

### `scripts/store.py`

**From a folder to its store** (`find`), in this order:

1. `AGENT_MEMORY_DIR`, which has to be a whole path to a store: a folder with `ORG.md` and a git
   history of its own. A missing path, a file, a folder that is no store, a relative path and an
   empty value are refused: the scripts would take an empty one for the working directory.
2. The nearest checkout on the way up (`.git` first, else `.jj`), and its key, read with
   `git -C <top of the checkout> config --local --get agent-memory.store`. `--local` is the
   repository's own configuration, shared by all its worktrees; a key in the person's global git
   configuration is not read, so it can't switch memory on for every repository at once.
3. For a `.jj` alone, `.jj/repo` is followed (a file's content is a path relative to that `.jj`), then
   `store/git_target`, relative to `store`. jj 0.46 writes `git` there for a repository it keeps
   itself and `../../../.git` for one it shares with git, so one rule covers both. Where jj shares the
   repository with git, a workspace made by jj 0.46 has a `.git` of its own and is read as git reads
   it; the fallback serves repositories jj keeps itself and workspaces from before workspaces had one.
4. A checkout with no key that sits inside another (a nested repository, a submodule) belongs to
   what the one around it belongs to: the search goes on outward. A dream counts a session as
   inside a repository by its path (`slim.inside`), so a nested repository's sessions were always
   the outer repository's, and the resolver says the same. Not a linked worktree: a worktree of
   some other repository that was put inside this one shares that repository's key, and that
   repository has none, so it has no store. It is told apart at the cost of one git question, asked
   only of a keyless checkout whose `.git` is a file: its shared git folder is elsewhere.
   A nested repository with a key of its own belongs to the store that key names, and a dream
   agrees: `slim.store_of()` now asks `store.keyed()` for the key
   first and falls back to the `memory` link of the older layout, so `slim.ours()` no longer counts
   what happens in such a repository for the store of the one around it. `attach` therefore takes a
   repository inside another store's repository. A worktree goes where its repository goes,
   wherever it stands: that rule is `store.repository()`, and both `find()` and `slim.store_of()`
   ask it. So a keyless worktree of some other repository, put inside this one, has no store for
   `amk` and is not this store's for a dream either: its sessions are not archived here and not
   counted by `dream_due` (a check runs both with that layout). A worktree of one of the store's
   own repositories stays the store's, with or without a tie of its own.
5. Otherwise no store.

**The folder decides, not the caller's git environment.** git is started in the top folder of the
checkout with `GIT_DIR`, `GIT_WORK_TREE`, `GIT_COMMON_DIR`, `GIT_INDEX_FILE`, `GIT_OBJECT_DIRECTORY`,
`GIT_ALTERNATE_OBJECT_DIRECTORIES`, `GIT_NAMESPACE` and `GIT_CONFIG` taken out: each of them makes
git read another repository, or another file, than the one in that folder, and every git hook sets
the first. `amk` and `store.py` also take them out of their own environment before a script runs, so
a write under `GIT_DIR` is committed in the store and not in the caller's repository. Left in,
because they can't change the answer (a check holds each of the first, third and fourth):
`GIT_CEILING_DIRECTORIES` and `GIT_DISCOVERY_ACROSS_FILESYSTEM` (git never searches upward here; the
walk up is `store.py`'s own and takes no ceiling), `GIT_CONFIG_GLOBAL`, `GIT_CONFIG_SYSTEM`,
`GIT_CONFIG_NOSYSTEM`, `GIT_CONFIG_COUNT` with its keys and values, and `GIT_CONFIG_PARAMETERS`
(`--local` reads none of them, and they are where a person tells git which folders it may trust).
`store.git()` leaves the eight out by itself, whoever calls it; `status.py` run by hand takes them
out of its own environment too, because `memcheck.py` and `dream_proposal.py` start git with the
environment as it is.

**Faults, each one plain line and exit 78, never "no store" and never a traceback:** a key that is
not a store name; a key naming a store that doesn't exist, or differing from its name in letter case
(names are matched against the list of stores, so a disk that takes `ONE` for `one` doesn't decide);
a key naming a store whose `roots.txt` doesn't list the repository (the line ends with the
`amk attach` that repairs it, and no verb runs until then: a script run for a store that leaves the
repository out would bring the session archive in line with that list); a working directory that
was deleted or can't be read; a git that doesn't answer within 10 seconds.

git calls on the lookup: none with `AGENT_MEMORY_DIR` or outside any repository, one in a
repository's main checkout, two in a linked worktree or a submodule (the second asks where the
shared git folder is, to know which line of `roots.txt` to look for), one more for each keyless
checkout around a nested one.

**From a store to its repositories.** `repos(store)` reads `roots.txt`: blank lines are skipped,
space around a path is dropped and `~` is the home folder. `attach(store, repo)` and `detach` read,
decide and write under the store's lock (`memcheck.locked`), and commit `roots.txt` and nothing
else in the store. Both take a repository's main folder only and name it to a subfolder, a linked
worktree or a second jj workspace; `detach` there would unset the key every worktree shares.
`attach` refuses a folder in no repository, a folder under `~/agent-memory`, and a repository
whose key or another store's `roots.txt` says it belongs elsewhere; when `roots.txt` can't be
written the key is taken back. `detach` removes any line by the path status reports it with, so a
line whose folder is gone, a line that is not a whole path and a line that names a subfolder can all
be taken out; it touches the key only in a main folder. A lock that can't be taken stops either
with nothing changed. A commit that fails (`memcheck.commit` returns its error) puts `roots.txt`
back byte for byte and the key back as it was, and says so with exit 78: the key and the line go
together or not at all. Paths in the commands these messages suggest are quoted for the shell.

`linked(store, repo)` says how one listed repository is tied to its store: `link` (the older
layout's `memory` link leads to the store), `key`, `both` or `neither`, and what is wrong, if
anything: a line that is no whole path or leads nowhere, a key naming another store, a repository
tied by neither. A link with no key is the normal state until the repository is moved (step 5
onwards) and is not reported; after step 10 it will be.

`store.py` loads none of the kit's other scripts until a store is written: they take their store
from `AGENT_MEMORY_DIR` as they load, and `amk` asks `store.py` for the store before it sets that.

### `bin/amk`

**Which Python.** `bin/amk` is a few lines of `sh` that choose the interpreter and give the
process over to it (`exec`): there is no process in between, so a signal sent to `amk` reaches the
script and the script's exit code is `amk`'s. In this order:

1. the one on record in `~/.config/agent-memory/interpreter`;
2. `/usr/bin/python3`, the one a launchd job gets on a Mac;
3. the first `python3` on the PATH. On a Mac this is never reached, because `/usr/bin/python3`
   always exists; it stays for a machine that has none.

`/usr/bin/python3` on a Mac is a launcher that belongs to Xcode: while Xcode updates, or its
licence is not accepted, it starts nothing, and every `amk` call and (from step 4) every hook would
fail with it. So what goes on record is the real program an interpreter says it is
(`sys.executable` resolved; for Xcode's, `…/Python3.framework/Versions/3.9/bin/python3.9`), which needs no
launcher. `amk interpreter record [python3]` writes it, after starting that interpreter and having
it load `store.py`; it refuses one that fails, one that is no Python, and one older than 3.9, and it
writes a second line, `tested with Python <version>`. It is the person's command: the guard stops
it for agents, it refuses inside an unattended dream, and `amk install` in step 4 runs it.

Because the launcher gives the process away, it can start only one interpreter and can't try
another after a failure. So it takes the one on record only when that can be told to work without
starting it, and says why in one line on stderr when it goes on with the next:

- it is a program: a file that may be executed (a folder, a missing file and a text file are not);
- `record` tested it: the record's second line says so (a path written into the file by hand
  never ran the kit, so a program that is no Python, or one that exits at once, is not started);
- it has not changed since: it is not newer than the record (a program replaced or updated after
  it was tested is not started; `amk interpreter record` tests it again).

What is left: a program that was tested, is byte for byte unchanged, and fails now (a library it
needs went away). That one is started and its failure is what `amk` ends with. Catching it would
take a second process that waits for the first, on every call, and signals and exit codes would no
longer be the script's own. `amk status` and `amk interpreter` show the Python that ran, the one
on record, and the launcher's own reason when it would not start that one. With nothing on record
nothing is said: that is the state before step 4.

Measured at a load average of 5, median of 40 runs, `amk help`: 36 ms the old way (Python started
directly through `/usr/bin/python3`), 38 ms with the launcher and nothing on record, 33 ms with a
tested program on record. So with a record, which is the normal case from step 4 on, a call is
faster than before, and without one about 3 ms slower.

The suite takes a Python as its argument (`sh tests/test-kit.sh "$(command -v python3)"`): that
one is then first on the PATH, for the scripts still called by hand, and on record, so `amk`,
`store.py` and `status.py` run under it too.

It runs the verb's script in its own process (`runpy`), after it has set `AGENT_MEMORY_DIR`. The
other way, replacing itself with a second interpreter (`os.execv`), was measured: 60 ms for `amk read` on `/usr/bin/python3` against 41 in one process (the script run by hand takes 37), on every verb and later on every Bash call. It would
also put back what a second Python does to a pipe: Python ignores SIGPIPE when it starts, so
`amk past … | head -1` would end with "Exception ignored … BrokenPipeError" again, and that can
only be set right from inside the process the script runs in. What running in one process could
get wrong is held by checks: for each kind of ending (exit 0, an exit with words, argparse's exit
2, STALE 3, DENIED 4, READ-ONLY 6) the exit code, stdout and stderr are byte for byte the script's;
SIGTERM, SIGINT and SIGHUP end `amk` exactly as they end the script run by hand, and nothing is
left running; no kit script is loaded before the store is set. What remains different is cosmetic:
a traceback from a script's own bug has `runpy`'s frames above the script's.

Before the script runs, `amk` sets SIGPIPE to end the process quietly, takes the git variables above
out of the environment, and sets `AGENT_MEMORY_FROM_KIT`, which tells `slim.repos()` and
`check_stale.repos()` that this copy of the script sits in no repository of the store.

Its own refusals exit 64 (no such verb, or a verb it doesn't run yet) and 78 (no store here, or a
fault above); `store.py` uses 78 too when it refuses an attach or detach, and 2 for arguments that
make no sense. No script of the kit uses 64 or 78.

The table itself is `scripts/verbs.py`, which `scripts/amk.py` runs from and `guard_memory.py`
reads, so the guard knows every verb that runs `memwrite.py` (see "The guard"). It is in
`kit-files.txt`, because a repo's copy of the guard needs it.

| Verb | Runs | Needs this folder's store |
|---|---|---|
| `read`, `write`, `edit`, `delete`, `undo` | `memwrite.py` with that command; the same stdin forms as `CLAUDE.md.snippet` gives | yes |
| `past` | `past.py` | yes |
| `check` | `memcheck.py` | yes |
| `due` | `dream_due.py` | yes |
| `proposal-check` | `proposal_check.py` | yes |
| `proposal` | `dream_proposal.py`, the bare read: the open proposal. Takes nothing after it | yes |
| `applied`, `declined` | `dream_proposal.py applied [numbers]`, `declined [file]` | yes |
| `alive` | `dream_owner.py alive`. Takes nothing after it | yes |
| `finish` | `dream_finish.py` | yes |
| `archive` | `archive.py` | yes |
| `own-notes` | `own_notes.py` | yes |
| `status` | `status.py` | no |
| `where` | `store.py where [folder]` | no |
| `attach`, `detach` | `store.py attach`, `detach`: the person's, stopped by the guard for agents | no |
| `interpreter` | `interpreter.py`: the Python amk runs under and the one on record. `record` is the person's | no |
| `help` | prints the table | no |
| `collect` | refused until step 3, in one line, exit 64 | |
| `apply` | refused until step 3, in every form (plain, `--dry-run`, `--done`, `--yours`), exit 64 | |

Not verbs, because they are the person's: `dream_proposal.py dismiss` and `forget`, and
`dream_owner.py release`. `amk proposal` and `amk alive` refuse anything after them (exit 64), so
no verb reaches those commands; the person runs them as the kit documents them. The four verbs
above are the ones `settings.snippet.json` allows agents today.

Also not verbs: `dream_try.py` (the runner's, and deliberately without a permission rule),
`guard_memory.py` and `memory_context.py` (hooks; `amk hook …` in step 4), `build_index.py`,
`check_stale.py` and `slim.py` (called by other scripts only).

**What a script takes from the folder it sits in**, which is the kit when `amk` runs it:

| Where | What | In this step |
|---|---|---|
| every script: `MEM = AGENT_MEMORY_DIR or <its folder>/memory` | the store | `amk` always sets the variable |
| `slim.repos()`, `check_stale.repos()`: the script's folder is a repository of the store | scope of the archive, `past`, `due`, own notes, stale lines | not counted when `amk` says the copy is the kit's (`AGENT_MEMORY_FROM_KIT`) |
| `collect_transcripts.health()`: `memory_context.parts(event, ROOT, MEM)` | Steering of the repository, for `_health.md` | `amk collect` is refused until step 3 |
| `memory_context.parts()` defaults `root=ROOT`, `mem=MEM`; `main()` uses them | what the start hooks load | not a verb; `status.py` passes both |
| `.claude/commands/dream.md`: "this repo's Steering block (the end of this file)", `git -C memory …`, `python3 scripts/…` | the dream's own instructions | the dream stays in the repo until step 4 |
| `dream_apply.py`: a `needs a session` item says "write `ROOT / item.target`" (a new skill under the repository's `.claude/skills/`) | the repository a skill is written into | `amk apply` is refused until step 3 says which repository that is |
| `dream_apply.py`: `DREAM_MD = ROOT / ".claude/commands/dream.md"`, read and written by `--yours` | Steering of the repository | the same refusal; `--yours` is the person's and the guard stops `amk apply --yours` as well |
| `dream_owner.py`, `dream_proposal.py` | the store only (`MEM`) | fine through `amk` |

Measured on the author's Mac at a load average of 5, median of 40 runs, nothing on record: `amk help`
38 ms (the bare interpreter is 21); `amk where` 52 ms in a main checkout and 60 in a keyless
worktree (one git question, two there); `amk read` 58 ms, 7 more than
`python3 scripts/memwrite.py read`; `amk status` 188 ms for seven stores and
135 for one. The guard, which step 4 runs on every Bash call, takes 33 ms.

### The guard

**How memory text reaches memwrite.** Main's rules (a heredoc's delimiter quoted, no backtick or
`$(` where the shell would run it, no line of the text equal to the delimiter) were keyed on the
text `memwrite.py`. They now hold for every spelling that runs it: the script by name, and
`amk <verb>` for each verb whose script is `memwrite.py`, by name, by any path, with an interpreter
in front (`python3 …/amk write`) and as `amk.py`. The verbs come from `scripts/verbs.py`, the table
`amk` runs from, so a verb for memwrite can't be added without the guard holding it to the rules.
Checks run each spelling through an unquoted heredoc, an `echo` in double quotes, a why in double
quotes and a here-string, and the right form through each.

**The person's commands.** One table, `verbs.PERSONS`, names them by script and word: `store.py
attach` and `detach`, `interpreter.py record`, `dream_proposal.py dismiss`, `forget` and `withdraw`,
`dream_owner.py release`, `dream_apply.py --yours`. The guard stops each one run by its script and
through `amk` (by any path to it, with an interpreter in front), also inside a compound command
(`if …; then …; fi`, `while …; do …; done`, `{ …; }`, `!`) and behind `env` with its options. It
also stops `store.attach(` or `store.detach(` called from `python3 -c` or a heredoc, a
`git config` that sets, changes or removes `agent-memory.store` (old and new spelling, `--unset`,
`--remove-section`, any letter case), and `git config -e` in a repository. Reads pass, also with
`--default`, `--type` or `--file`.

Each of those commands also refuses by itself inside an unattended dream, by the kit's one rule
for that (`dream_proposal.read_only()`, which `store.py` and `interpreter.py` now ask too), whatever
name it was called by. `--yours` needs a terminal as well.

**A guard that has lost its table fails closed.** `guard_memory.py` reads the verbs from
`verbs.py`. In a repo that has the guard and not that file, every command that could touch memory
(`memwrite.py`, any `dream_*.py`, `store.py`, `interpreter.py`, `amk` by any path) is stopped with a
line that says the kit's files are incomplete and to run the sync. The rest of the guard (what a
command writes to) works as before.

**What a determined agent can still do, in an interactive session where the person allows it.**
The guard reads text. It does not see `amk` reached through a link with another name, a command
held in a variable or built by a script, or a file written by a tool it doesn't know. So in a
session that is not an unattended dream, an agent that hides the command that way can run
`amk attach`, `amk detach` or `amk interpreter record`, if the person's permission mode lets the
command through: no allow rule covers them, so in the default mode the person is asked. What it
can't do by writing a key alone is move a repository: the store fails closed. A key only works
when the store it names lists the repository in its own `roots.txt` (otherwise every verb refuses,
exit 78), and `roots.txt` is guarded (appending, replacing, editing in place, rewriting through
git). The record of amk's Python is a file under `~/.config`; the guard does not cover it, and a
path written there by hand is not started, because it lacks the line `record` writes, though an
agent that writes that line too is not stopped by anything but the person's permission mode.

### `scripts/status.py`

`amk status` prints one line per store, `amk status <store> …` each named store in full, `--json`
either as JSON. It reads files only: it waits for no lock, refreshes no archive and writes nothing
(the suite compares its whole HOME before and after every run, also while every lock of the store
is held by another process). The one thing it does to a lock is `dream_owner.holder()`'s: to tell
whether an unattended run or a collector is alive, that asks for the run lock and the collect lock,
shared and without waiting, and lets go at once. Each store's record is made by a process of its own (`status.py --one` with
`AGENT_MEMORY_DIR`), all started at once and waited for together, because every reader it reuses
serves the one store it was started for: `dream_due.load()`, `trusted()` and `waiting()` for the
state file and the back-off; `dream_owner.holder()` for a dream that is alive and whose it is;
`dream_proposal.newest()` for the proposal written last and its state; `memory_context.parts()` and `wanted()` for
what a start loads; `memcheck.drift()` for integrity, worked out once per store; `build_index.entries()`;
`own_notes.py`; `store.repos()` and `store.linked()`. All the stores together get 10 seconds: one
whose reading never ends (a pipe in `project/`) is stopped then and its record says so. The
questions about the kit go to git beside the stores, not before them, so a git that is slow
there takes none of the stores' time.

What the hub reads for proposals is `proposals`, from `dream_proposal.load()`, read once per store.
The split of what is carried is in one function, `status.carried()`, and follows the spec's "For a
reader of the record": by `waiting` alone; `ticked` only says the item was ticked. Whether an
approved item can still be done is asked of `dream_proposal.stuck()`, and what is declined of
`dream_proposal.declined()`.

It exits 0 whenever it could report, whatever the stores' problems. It exits 1, with one line, for a
store name that doesn't exist or a clock that can't be read, and 2 for arguments that make no sense.

**Memory content.** `proposals.carried.*.item` is the text of an item of a proposal, which quotes
a memory line. It is the only field that holds memory content, and it is `null` unless `--text` is
given: the output of a status command gets logged, pasted and piped, so the default carries no
memory text, in the JSON or in the full report (an item is named by its proposal and its number).
The hub, which shows the items to the person, asks with `--text`. Paths, store names, verdicts and
problem lines are in the output either way.

The JSON, with stable keys. Every record has every key. A field that can't be read is `null` and
`problems` names it and says why; the last column lists every other case of `null`.

| Key | Meaning | `null` when |
|---|---|---|
| `at` | the moment of the report, epoch seconds | never |
| `kit.path`, `kit.commit`, `kit.dirty`, `kit.why` | the kit that answered, its short commit, whether its files differ from it | `commit` and `dirty`: the kit's folder is not the top of a git checkout of its own (a plain copy; a copy inside another repository), or git could not be asked. `why` then says which, and is `null` otherwise |
| `kit.python`, `kit.python_version`, `kit.python_recorded`, `kit.problems[]` | the real program `amk` ran under, its version, the one on record, and what is wrong with the kit itself (the one on record can't be started) | `python_recorded`: none is on record |
| `stores[]` | one record per store, by name | |
| `store`, `path` | the store's name and folder | never |
| `repos[]` | each line of `roots.txt`: `path`, `exists`, `linked` (`link`, `key`, `both`, `neither`), `own_notes` (`on`, `notes`, `folder`, by `own_notes.py`) | `repos`: only in a record nothing could be read for. `own_notes`: the repository's settings could not be read |
| `state` | `file`, `status` (`ok`; `none` when no check or dream has written it yet; `refused`), `why` (the kit's own words when refused) | `why`: not refused |
| `check` | `at`, `verdict`: the last daily check | the state is refused. `at` and `verdict`: never checked |
| `last_dream` | `at` (up to when the last dream read), `reason` (why it started) | the state is refused. `at`: no check or dream has recorded it yet. `reason`: none recorded |
| `dream` | `running`; `who` (a session, an unattended run, a collector at work, a dream started by an older kit), `since`, `heard` (its last sign of life); `batch_files`, the sessions in the batch | `who`, `since`: nobody is dreaming. `heard`: nobody is, or the dream holds a lock and needs no sign of life. The whole field: the batch's owner can't be read |
| `proposal` | the one written last, by the store's record (`dreams/proposals.json`): `file`, `date` (the day it was written), `age_days` (calendar days), `state` (`open`, `applied`, `dismissed`), `items` (every tick line), `numbered` (the numbered changes), `ticked` | the store's record knows no proposal, or can't be read. `items`, `numbered`, `ticked`: the proposal's file is gone |
| `proposals` | the store's whole record of proposals: `open` (the open one's file), `all` (every proposal: `file`, `state`, `written`, `closed`), and `carried`, split by `waiting` and nothing else: `approved` (waits for `you` or `a session`, oldest first, each with `stuck`: why nothing can do it any more, in the kit's words), `another_look` (not waiting, goes to the next dream once), `declined`. Each entry has `item` (memory content: only with `--text`), `from`, `id`, `number`, `left`, `ticked`, `waiting`, `at`; `from` and `id` name it | `item`: `--text` was not given. `open`: none is open. `closed`: the proposal is open. `number`: the item had none. `stuck`: it can still be done. `waiting`: nobody has to do it. The whole field: the record can't be read |
| `reminders` | `proposal` (which open proposal the person was last reminded of, and when) and `waiting_said` (when they were last told what waits), as the state file has them | the state is refused; either part when no reminder was sent |
| `tries` | `count`; `last` (`at`, `ended`: `proposal`, `nothing`, `failed`, or `null` for one that did not finish); `waiting` (the kit's words during a back-off); `next_try` (`YYYY-MM-DD`: the first day the kit's own rule, `dream_due.waiting()`, lets the daily check start a dream again) | the state is refused. `last`: no try on record. `waiting`, `next_try`: no back-off |
| `schedule` | `every` (`day` or `week`), `hour`, `weekday`, `cwd` | the store has no job, or its job can't be used (then `problems` says so). `weekday`: a daily job |
| `loads` | `limit`, `session`, `subagent`: characters a start loads before any cut, the largest over the store's repositories | `ORG.md` or `INDEX.md` can't be read |
| `entries` | `total` and `files`: entries per `project/` file | a `project/` file can't be read |
| `problems[]` | plain lines: a refused state file; a field that could not be read; an unusable job; a repository's tie that is a fault; `ORG.md` or a repository's Steering still as shipped, or missing; a start over the limit; each file changed outside memwrite (`integrity: …`) | never: `[]` when there are none |
| `not_checked[]` | what costs more than reading a few files and is left for the scheduler's `health.json` | never |

A store nothing could be read for (its process failed, or was stopped at the limit) has `store`,
`path`, `problems` and `not_checked`, and `null` everywhere else.

```json
{
 "at": 1794472955.05,
 "kit": {"path": "/Users/you/tools/agent-memory-kit", "commit": "1a2b3c4", "dirty": false, "why": null,
         "python": "/Applications/Xcode.app/Contents/Developer/Library/Frameworks/Python3.framework/Versions/3.9/bin/python3.9",
         "python_version": "3.9.6", "python_recorded": null, "problems": []},
 "stores": [
  {
   "store": "shop",
   "path": "/Users/you/agent-memory/shop",
   "repos": [
    {"path": "/Users/you/projects/shop", "exists": true, "linked": "link",
     "own_notes": {"on": false, "notes": 12, "folder": "/Users/you/.claude/projects/-Users-you-projects-shop/memory"}}
   ],
   "state": {"file": "/Users/you/.local/state/agent-memory/shop.json", "status": "ok", "why": null},
   "check": {"at": 1794466154, "verdict": "not due: the last try on 9 Nov failed; next try from 12 Nov"},
   "last_dream": {"at": 1794120704, "reason": "27 sessions since 1 Nov"},
   "dream": {"running": true, "who": "session 5f2c1b7e-0d4a-4c1e-9b1f-2a6d8e3c9f10", "since": 1794470400, "heard": 1794472800, "batch_files": 9},
   "proposal": {"file": "2026-11-08-proposal.md", "date": "2026-11-08", "age_days": 2, "state": "open", "items": 14, "numbered": 12, "ticked": 3},
   "reminders": {"proposal": {"proposal": "2026-11-08-proposal.md", "at": 1794247200}, "waiting_said": 1794247200},
   "proposals": {"open": "2026-11-08-proposal.md",
                 "all": [{"file": "2026-11-01-proposal.md", "state": "applied", "written": 1793556000, "closed": 1793600000},
                         {"file": "2026-11-08-proposal.md", "state": "open", "written": 1794160800, "closed": null}],
                 "carried": {"approved": [{"item": null, "from": "2026-11-01-proposal.md", "id": "5c1d0e9a", "number": 7,
                                           "left": 0, "ticked": true, "waiting": "you", "at": 1793600000, "stuck": null}],
                             "another_look": [], "declined": []}},
   "tries": {"count": 2, "last": {"at": 1794247200, "ended": "failed"}, "waiting": "the last try on 9 Nov failed; next try from 12 Nov", "next_try": "2026-11-12"},
   "schedule": {"every": "day", "hour": 20, "weekday": null, "cwd": "/Users/you/projects/shop"},
   "loads": {"limit": 9500, "session": 5479, "subagent": 5695},
   "entries": {"total": 21, "files": {"project/architecture.md": 6, "project/decisions.md": 6, "project/gotchas.md": 9}},
   "problems": ["Steering in /Users/you/projects/shop is still the kit's default: say what matters in this project"],
   "not_checked": ["uncommitted and untracked files in the store (amk check)", "memory lines that name files no repository has"]
  }
 ]
}
```

Against the hub's own functions (`repos_of`, `schedule_of`, `dream_check_of`, the batch count, the
newest proposal's counts, `entries`, and the `ORG.md` and Steering parts of `setup_of`): no difference. What the hub works out and status does not give:
`dreaming` from a tmux session or the launchd job's PID (status gives the kit's own answer,
`dream_owner.holder()`, which is true for an unattended run for as long as it holds the run lock),
`applied` from the store's git log (status gives the record's `state`), whether Claude Code trusts a
repository (`~/.claude.json`), and whether the kit is committed in a repository, which this design
makes moot.

### Edits to existing files

- `scripts/slim.py` `repos()`: one case before its last line. Main's `repos()` is the lines of
  `roots.txt` and nothing else, and for a store with no `roots.txt` the main checkout of the
  repository the script sits in. That last rule would make the kit a repository when `amk` runs
  the kit's copy, so `amk` sets `AGENT_MEMORY_FROM_KIT` to the kit's folder and a copy that sits
  there returns no repository. A repo's copy that inherits the variable is not the kit and is not
  affected. `check_stale.repos()` calls `slim.repos()` now and is not touched.
- `scripts/slim.py` `store_of()`: the key first (`store.keyed()`), then the `memory` link. `slim.py`
  loads `store.py` for it (as `stores`: `slim.store` is a function), so `scripts/store.py` is in
  `kit-files.txt`.
- `scripts/memory_context.py` `parts()`: one more argument, `broken`, for a caller that already has
  the store's drift, so `status.py` works it out once and not once per start and repository.
- `scripts/guard_memory.py`: the cases under "The guard". `RUNS` and `FEEDS`, main's two patterns
  for a command that runs memwrite, gained the `amk` spellings.
- `scripts/dream_proposal.py` `read_only()`: takes the words for what is the person's, so
  `store.py` and `interpreter.py` refuse inside an unattended dream by the same rule and not a copy.
- `kit-files.txt`: `scripts/verbs.py`, which a repo's copy of the guard loads, and
  `scripts/store.py`, which a repo's copy of `slim.py` loads.
- `tests/test-kit.sh`: at the top, `jj` is found before the PATH is cut down, a Python can be
  given as the argument, and the suite always ends with a line that says how it went: whatever
  kills the shell before its last line prints `--- ABORTED after the check "…"` and exits 1. The
  section "one kit, step 1" is before the last lines. The suite passes in a plain copy of the kit
  as well as in a git checkout.
- `README.md`: `amk status` under "Checking it", a section on `bin/amk`, and the suite's time.

### Known and left for later steps

- `amk collect` is refused until step 3 (see the table of what a script takes from its folder).
- A session in a jj workspace that is not a git worktree is in scope for a repo's own copy of the
  scripts (its folder counts) and not for the kit's copy (only `roots.txt` and git worktrees count).
  Step 3 has to say which is meant.
- `archive.py` on `main` removes archived sessions whose transcripts are gone whenever the list
  of repositories it is run with differs from the last run's, and that list depends on where the
  script is run from. Run from a worktree whose path sorts before the repository's, the store's
  repositories come out as that worktree alone, and every session whose transcript Claude Code has
  already deleted is removed from the archive (reproduced in a throwaway HOME). Reported, not changed in this step.


## Step 3 as built

Scripts stop treating their own folder as a repo; Steering can live in the
store; `run-dream.sh` dreams for a repo that has no copy of the kit. Stores in the older layout keep running
their copies, and nothing they load or collect changes (checked byte for byte, see "Checks").

### Which repo a run is for: one rule

`store.run_for(root, store)`, where `root` is the folder the script's copy sits in:

- a copy inside a repo (anything but the kit amk names in `AGENT_MEMORY_FROM_KIT`): that repo, `root`,
  as before. Nothing is asked; a copy inherits amk's variable and is still its repo's;
- the kit's own copy: the repo the working directory is in, the nearest checkout on the way up whose
  repository (`store.repository()`: a worktree's is the one it was made from, a jj workspace's its
  first) is a line of the store's `roots.txt`. None when it is in none of them.

The store's repos stay `roots.txt` (`store.repos()`, `slim.repos()`).

| Binding | Was | Now |
|---|---|---|
| `memory_context.parts()` and `main()` | `root=ROOT` | `root` passed in; `main()` passes `run_for(ROOT, MEM)`, after it has seen there is a store (so a folder with no store still loads nothing more of the kit) |
| `collect_transcripts.health()` | `parts(event, ROOT, MEM)` | `parts(event, run_for(ROOT, MEM), MEM)` |
| `dream_apply.py`: the new skill of a `needs a session` item | `ROOT / target` | `REPO / target`, `REPO = run_for(ROOT, MEM)`; with no repo the item is refused (STALE) |
| `dream_apply.py`: `DREAM_MD`, Steering items and `--yours` | `ROOT/.claude/commands/dream.md` | `STEERING.md` in the store when it has one; else `REPO/.claude/commands/dream.md`; with neither the item is refused (STALE) |
| `slim.repos()` with no `roots.txt` | `[main_checkout(ROOT)]`, `[]` for the kit | `[main_checkout(run_for(ROOT, MEM))]`, `[]` when that is none: the same answers, from the rule |
| `status.py` Steering check | `memory_context.block(repo)` | `block(repo, MEM)`, so `STEERING.md` is read when it is there |

Not changed, because they never meant "the repo": `check_stale.repos()` and `own_notes.py` (both take the
repos from `slim.repos()`), `proposal_check.py` (`ROOT/scripts/<name>.py` asks whether a command is one
of the kit's: the kit's scripts from the kit, the copy's from a copy), `build_index.py`,
`dream_finish.py`, `memwrite.py`, `memcheck.py` (the store only), `status.py` and `interpreter.py`
(`KIT` is meant).

### Steering in the store

`<store>/STEERING.md`: a title line, then the text for every repo of the store, then optional
sections `## <repo>`, named after a repo's main folder, for that repo alone. A repo's Steering is the
top text, then its section (`memory_context.part_for()`), and is loaded exactly as a block was
(`given()`: comments out, a shipped default loads nothing).

- **Source.** A store without `STEERING.md` reads each repo's block at the end of its
  `.claude/commands/dream.md`, as today. Once the file is there it is the only source:
  `memory_context.block()` (and so the start hook, the collector's `_health.md`, `status.py`),
  `dream_apply.py` and the dream's instructions read it, and the blocks in `dream.md` are ignored.
- **Protected like `ORG.md`.** `uchg` (set by `init`, by `memcheck.lock_org()`, which now locks both, and
  again by every apply that finds it off); the deny rules `Edit(/memory/STEERING.md)` and
  `Edit(~/agent-memory/*/STEERING.md)` in `settings.snippet.json`; the guard (`TOP`, `CODE_PATH`);
  `memwrite.py` (not a writable tier); `memcheck.py` reports it unlocked or uncommitted, and `--seal`
  commits a hand edit with a `human` row; the start note warns when it is unlocked.
- **The person's apply.** A Steering item named `Steering` changes the top text, `Steering (<repo>)`
  that repo's section (made at the end when it is not there). `--yours` needs both `ORG.md` and
  `STEERING.md` unlocked (it names the one still locked and gives `chflags nouchg <ORG.md> <STEERING.md>`),
  writes through `memwrite.record()` or `memwrite.edit()`/`delete()` as `human`, logged and committed,
  and locks both again however it ends. An agent's apply records the item as `yours` with that command.
- **The move: `amk steering init`** (`scripts/steering.py`). The person's: in `verbs.PERSONS`, so the
  guard stops it by amk, by path and by script, and it refuses inside an unattended dream. It makes
  `STEERING.md` from the repos' blocks: the same block in every repo becomes the top text; otherwise each
  repo's own block is its section; a shipped default, or no block, is nothing. It refuses when the file
  is there, when two repos of one name would need two sections, and when any repo would then load other
  Steering than its block gives (checked per repo before anything is written). It writes through
  `memwrite.record()` (row `| STEERING.md | human | - | steering moved … | new → <hash> |`, one commit,
  the index rebuilt) and locks the file. A repo with copies has no key, so the person names the store:
  `AGENT_MEMORY_DIR=~/agent-memory/<store> agent-memory-kit/bin/amk steering init`.

`init` names, in one line each, worktrees whose own block differs from their repo's: they load their repo's Steering
from `STEERING.md` once they carry the new kit. Nothing is refused for them.

### The dream for a repo with no copy

`run-dream.sh <repo>`: a repo with `scripts/dream_try.py` runs that copy through its `memory` link,
line for line as before (`/dream`, the repo's settings). Any other repo is found by its key
(`amk where`), and the run exports `AGENT_MEMORY_DIR`, `AGENT_MEMORY_FROM_KIT` (the kit) and the kit's
`bin/` first on the PATH, then gives `claude` what a copy would have, from `scripts/dream_try.py`:

- `prompt`: the propose part of the kit's `.claude/commands/dream.md` (no frontmatter, no Steering
  block, `$ARGUMENTS` empty), with every command spelled as its amk verb (`verbs.spelled()`) and
  `memory/` as the store's path, after a short preamble that says so and lists the verbs. The person's
  commands (`dismiss`, `--yours`, `release`, `reread`, `done`) are left as they are.
- `rules`: the copy's own allow rules from `settings.snippet.json`, spelled the same way
  (`verbs.amk_rules()`): `Bash(python3 scripts/past.py *)` becomes `Bash(amk past *)`,
  `Bash(python3 scripts/memwrite.py *)` one rule per memwrite verb. They go on `--allowedTools` after
  the four `Edit` rules. The mode stays `--permission-mode dontAsk`.
- `agents`: the two analysts from the kit's `.claude/agents/`, for `--agents`.
- `hooks`: the start hook and the Bash guard, run from the kit, for `--settings`.

`--add-dir` gets the store as well. The files are made in the state folder and removed after the run.

So the dream's instructions are one text: the copy reads them as `/dream`, the kit's run reads them
spelled for amk. One map (`verbs.as_amk()`) spells both the instructions and the rules, and the suite
holds every command the instructions ask for to a rule in its exact spelling, in both worlds.

### amk verbs

`collect`, `apply` (plain, `--dry-run`, `--done`; `--yours` is the person's and stopped by the guard, as
in step 1), `machine-check` (takes nothing, like `proposal` and `alive`) and `steering` (the person's)
are verbs. `LATER` is gone.

### Checks (`tests/test-kit.sh`, section "one kit, step 3")

- `run_for()` as a copy (from a subfolder, from elsewhere, from a worktree's copy, with amk's variable
  inherited) and from the kit (subfolder, worktree, outside every repo, a repo not listed).
- The start hook from the kit in a repo with copies is byte for byte the copy's; in a repo with the
  key only it loads the store and no Steering. The collector's `_health.md` from the kit equals the copy's.
- **The older layout** (`tests/live-layout.sh`): two stores laid out as stores made before this step are (copies, a link,
  no key, no `STEERING.md`; two repos sharing a real block and a worktree, one repo with the default).
  It prints a hash for each session and subagent start in each repo, the collector's output and every
  file of a dream's batch. `tests/live-layout.before` is what the kit printed when this layout was released; the suite holds the kit
  to it. Paths are taken out, `_timing.json`'s store commit and the numbers of `_groups.md` (sizes
  that count paths) too.
- **A whole dream cycle** in a repo with copies and in one with the key only: a write, three sessions,
  collect, a proposal from a fixture, `proposal-check`, finish, ticks, apply, `--done`, close; each in
  its world's spelling, in a HOME of its own. The two stores end the same, file for file (HOME, times,
  commit ids taken out); apply names each repo, never the kit, as the skill's place; the key-only repo
  is left clean.
- **`run-dream.sh` for a key-only repo** with a fake `claude`: it ends with the run marker, gets the
  environment, the mode, `--agents`, `--settings`, the store as a folder and the rules; every
  command of the rendered prompt has a rule in its exact spelling, no command of the person's has one,
  and the copy's instructions still each have one of the copy's rules.
- Steering: `init` for identical blocks, for differing blocks and the default, loads unchanged byte for
  byte, a refused second `init`, a refusal inside an unattended dream, the guard; sections by main
  folder in a worktree and from the kit; a `dream.md` block ignored once the file is there; `status` and
  `_health.md` naming a repo it says nothing to. Protection: eight shell routes stopped in both
  spellings, the deny rules, memwrite refused, the lock, `memcheck` and the start note on an unlocked
  file, `--seal` of a hand edit, an agent's apply relocking it. `--yours`: refused while `STEERING.md`
  is locked; a top-text add, a new section and an edit in place, logged as `human`, committed, both
  locks back; and once through `amk apply --yours` run by the person from the kit.

`tests/test-live.sh` gains the loop for a second throwaway repo with only the key (not run here).

### Known and left for later steps

- Messages to the person still spell the copy's commands (`python3 scripts/dream_proposal.py dismiss`,
  `chflags nouchg memory/ORG.md`, …), also when the run is the kit's. The person's commands have no
  amk verb (step 1's choice) and a repo with the key only has no `scripts/`; in that world the person
  runs them with `AGENT_MEMORY_DIR` set, or after step 4.
- A repo with the key only has no `/dream` command, no memory at a session's start and no deny rules
  until the plugin (step 4). Only the unattended dream is complete there in this step; `amk apply` is
  run by hand or by a session that is told to.
- The jj question from step 1: `run_for()` follows a jj workspace to its first workspace. Which sessions
  `slim.py` counts for the kit's copy is unchanged (roots and git worktrees).

### Review findings, fixed

1. The guard reads past a bare `--` before the person's word (`verbs.persons()`), for every command of
   the person's, by script and by amk.
2. A Steering item acts in the part it names: `plan()` sees only that part (`part_of()`), and every step
   carries it (`Part`), so an add is not "already applied" because another section holds the line, and an
   edit or delete never reaches into another part.
3. `run_for()` compares the kit's folder by real path, and `run-dream.sh` names the kit with `pwd -P`.
4. `memory_context.trouble()`: a section that names no repo of the store (with how many lines no session
   gets) and a `STEERING.md` that can't be read are a WARNING in the start note and a problem in
   `amk status`; one that can't be read is no longer called unwritten. A `STEERING.md` the store's git
   tracks and the folder lacks is a problem for `memcheck.py` (`steering_gone()`) and `amk status`.
5. `steering init` turns a refused write into one plain line.
6. `slim.kit_call`: `amk <verb>>file` is kit work, as its script twin.
7. The key-only prompt drops the apply part and says "Without it, this repo has no Steering."
8. `steering init` names worktrees whose own block differs, in one line each, and refuses nothing for them.

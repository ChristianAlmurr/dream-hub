# Dream Hub: the hub

The control-room half of Dream Hub: a private web page on your Mac for projects on the agent memory kit. Create
them, read their memory, run and review dreams, and run several Claude Code sessions in browser terminals. Every
Claude session also starts with Remote Control, so it can be followed from the Claude app on your phone.

This is the hub's full reference. Start with the [README at the top of the repository](../README.md). Open the hub at
the address `install.sh` printed (`http://127.0.0.1:8491/?key=…` the first time, on this Mac only; or your Tailscale
address). The design reasoning is in `docs/brief.md`.

## How it works

| Part | Where |
|---|---|
| Server (Python standard library, loopback only) | `server.py`, launchd job `ai.agent-hub` (optional; `install.sh` offers it) |
| Reads and writes for projects, memory, dreams and sessions | `hub_data.py` |
| Link cards (fetched under guards), file facts for hover cards, image thumbnails | `hub_preview.py` |
| Everything asked of the agent memory kit: the scripts it runs, the kit files it reads | `hub_kit.py`, and nowhere else |
| Which hub this is, and what its commands run in | `hub_base.py` |
| Session state from Claude Code hooks | `hub_event.py`, written to `~/.local/state/agent-hub/events/` |
| What a session is doing now: tool, turn, orb, error, context, changes, ports; Clear, Stop a port, peek | `hub_status.py` |
| A session's changes, read with git: the count, the review, line comments sent back | `hub_diff.py` |
| Terminals | tmux on its own socket (`tmux -L agent-hub`), so they outlive the browser and the server |
| What a test run talks to | `tests/instance.py` |
| Interface (no build step) | `ui/` |
| Install or reinstall | `sh install.sh` at the top of the repository |

It holds almost no state of its own: projects are the stores in `~/agent-memory`, and proposals are the files in
each store's `dreams/`. Session metadata is in `~/.local/state/agent-hub/sessions/`.

## A second instance

Six things make an instance, and each comes from an environment variable.

| Variable | What it sets | The installed hub's value |
|---|---|---|
| `HUB_PORT` | Loopback port the server listens on | `8491`, or the config's `port` |
| `HUB_STATE` | Sessions, hook events, records, uploads, project names and icons | `~/.local/state/agent-hub` |
| `HUB_TMUX` | Name of the tmux socket | `agent-hub` |
| `HUB_CONFIG` | File holding the owner's login and the app's origin | `~/.config/agent-hub/config.json` |
| `HUB_KIT` | The agent memory kit: its template, installer, dream runner and scripts | the config's `kit`, else the `agent-memory-kit` folder beside the hub, else `~/tools/agent-memory-kit` |
| `HUB_JOBS` | Folder the dream jobs are read from | `~/Library/LaunchAgents`, launchd's own |

It is all or nothing. With none of them set, the server is the installed hub. With any of them set it is a second
instance and all six must be: a missing one, an empty one, a relative path or a port outside 1 to 65535 stops the
server with one sentence naming the variable, before it touches anything. So a second server can never run half on the
installed hub's state folder, socket, config or dream jobs. The rule is `read_instance` in `hub_base.py`; the tests use it too. A
session's hook gets its hub's state folder from `HUB_STATE` in the session's own environment, and refuses an empty or
relative one.

The top-level `install.sh` installs the one hub this Mac has and nothing else. It refuses to run while any `HUB_*` variable is set.
A second instance is started by hand and is never installed.

The kit schedules dreams through launchd and nowhere else, so only a hub whose `HUB_JOBS` is launchd's own folder
asks it to. A hub with any other folder reads the dream jobs it finds there, makes new projects without a dream job
(`AGENT_KIT_NO_SCHEDULE=1` to the kit's installer), never asks launchd anything, and refuses the dream switch with a
sentence saying why. That is what lets a second instance run without touching launchd.

What a second instance has to itself: its port, its sessions and terminals, its hook events, records and uploads, its
project names and icons, its config, and the folder it reads dream jobs from. What it shares with the installed hub, because these belong to the Mac:

- the stores in `~/agent-memory` and the folders in your projects folder, so both list the same projects
- the kit's state folder, `~/.local/state/agent-memory`
- `~/.claude`: Claude Code's transcripts, settings and its own notes

So a second instance is for trying the hub's code, not for trying things on a project you care about.

To run one beside the installed hub, with a config whose origin is its own loopback address:

    mkdir -p /tmp/hub2
    echo '{"login": "you@example.com", "origin": "http://127.0.0.1:8492"}' >/tmp/hub2/config.json
    export HUB_PORT=8492 HUB_STATE=/tmp/hub2/state HUB_TMUX=agent-hub-2 HUB_CONFIG=/tmp/hub2/config.json HUB_KIT=$(cd ../agent-memory-kit && pwd) HUB_JOBS=/tmp/hub2/jobs
    /usr/bin/python3 server.py

It answers only requests that carry that login and that Host, as a hub behind Tailscale Serve does (a config with a `key` and no `login` works as on the installed hub too). Stop it by its PID, then end
its terminals with `tmux -L agent-hub-2 kill-server`.

## Home

Home leads with what needs you, as a queue with three tabs, each with its count: **Needs you** (a session asking,
a proposal to review, what dreams found, approved changes only you can apply), **Working** (sessions in a turn and
dreams running) and **Done** (turns that finished, and sessions closed in the last day). The first tab with something
in it is shown; a zero never leads. `1`, `2`, `3` switch tabs, `j`/`k` move, `↵` opens. A Done row is unread until you
open its session or press `e` (**Mark read**, and again to mark it unread). Read is kept in this browser
(`localStorage`, `hub.read`), keyed to what the session last said, so its next finished turn is unread again.

Below the queue, a card per project says one thing: how many sessions are open, else that setup isn't finished,
else when it dreams. The repo line is left out when it only repeats the project's name. Activity folds the rows one
dream wrote into one line, which opens.

A project's pages (its sessions, Memory, Dreams) share tabs in the top bar: Sessions with how many are open, Memory
with how many lessons, Dreams with how many things there wait for you. `m` and `d` still jump to them.

## Projects and sessions

Each project has a homepage (`#/p/<name>`) with its sessions nested under it in the sidebar. A session has its own
page (`#/s/<id>`), and up to four can share one (`#/s/<id>,<id>,…`) in a 2×2 grid with **Split**, the sidebar's split button or ⌘-click. Drag the gutters between panes to size them; double-click one to even them out. The right panel follows the pane you last clicked. ⌥1–4 jumps between panes.

**The shell.** Drag the sidebar's right edge to make it 180 to 420px wide. Drag it past the narrow end, press ⌘B or use
the button at its foot, and it folds to a 48px rail: each project's mark and an orb for every session still running
under it, each named on hover. The right panel of a session resizes the same way from its left edge (240 to 560px), and
dragging it shut closes it. Each session keeps its own panel, open or shut and how wide; one you haven't opened yet takes
the last choice you made. All of this is kept per browser (`localStorage`), so a phone and the Mac each keep their own.

Every session row in the sidebar has its orb and, while it is live, a second line in its state's colour: the tool and
target it is on with the turn's elapsed time (from `activity` and `turn` in `/api/state`, when the server sends them),
the question it asks, or what it said last. A project row shows an orb while any of its sessions works or something
waits on you. The tab's title leads with the counts, `(1 waiting, 2 working)`, and its icon gains a rose diamond or a
jade square.

**Keys.** `?` docks a searchable sheet of every key at the left, and the app stays usable beside it. j and k walk the
sidebar (on a page with its own list, they walk that list, and walk the sidebar once focus is in it); ↵ opens, space
peeks at the session on the row. `g` then `h` goes home, `g d` and `g m` open this project's Dreams and Memory, `g n`
starts a session, `g s` goes to the sidebar. ⌘K opens the jump list, led by actions for the session in hand (the
sidebar row with focus, or the pane you're in): Open, Peek, Rename, Close, Resume, Copy the phone link, Delete…

A session's `⋯` menu in the sidebar renames or deletes it (Delete is also in a running pane's `⋯` menu and on a closed or ended pane). A rename is kept with the session on the server, so every browser sees it. A Claude session already connected to Remote Control keeps the title it started with on the phone, because changing it would mean typing into the session.

A session needs you when Claude asks permission, opens a dialog, or finishes a turn on a question: the server reads the last reply from the transcript (`question_in` in `hub_data.py`), so the hook script stays as it is. It shows in the sidebar and, when the app isn't in front, as a browser notification (turn on from the sidebar). A worker in `ui/clock.js` keeps polling in background tabs.

**Close session** (pane header, or the `⋯` menus) ends a Claude session and keeps its conversation. It first asks Claude
to tidy up and write a four-line handoff (Done, Open, Next, Cleaned) unless you untick that; Claude must be idle with nothing typed for the handoff.
Closing never commits anything, and the dialog says how many uncommitted changes the folder has. A closed pane shows the
conversation as a record, handoff on top, with **Resume**, which runs `claude --resume <id>` in the same folder with the
same mode, model, effort and agent, so Claude continues the same session with its whole history.

The tidy-up rules sent with the prompt: stop only servers and processes this session started, never launchd services
or Docker containers; wait on a PID or `pgrep -x`, never `pgrep -f` (it matches its own loop);
delete only test residue inside the session's temp folder (`/private/tmp/claude-<uid>/`) or its project folder, never
`~/Library/Caches` or the npm, pip and Homebrew caches. The prompt goes in as one bracketed paste through a tmux buffer:
typed keys that long arrive as a paste and Claude keeps only the tail.

Claude's memory isn't relied on. A closed session's record asks the hub, each time it opens, for servers still listening
whose working folder is inside the session's folder, leaving out launchd jobs and anything under a running hub session.
They are listed as "Still running from this folder" with port, pid and uptime. The hub never stops them.

Claude Code deletes its own transcripts after 30 days, so the hub copies each closed or ended session's transcript (and
its subagents) into `~/.local/state/agent-hub/records/<id>/`. The record reads from that copy once Claude's is gone,
and Resume puts it back where Claude looks for it. A close interrupted by a hub restart finishes on the next poll,
without its note.

Plain terminals keep Stop, which ends the process. The ✕ in a grid only closes the pane.

**Delete** (`delete_session` in `hub_data.py`) asks first, in a dialog that says which session it is (project, when it last
ran and started, its size), whether it runs now, that the conversation can't be resumed or read in the hub afterwards,
what goes to the Trash, and whether the memory kit has read it: from the kit's own record of its last dream
(`last_dream` in its state file), all of it, only up to that dream, or none of it, in which case what it taught is lost
to memory. The kit's searchable archive copy (`~/.local/state/agent-memory/sessions/<store>/`) is said to be kept, and
is: the hub never touches it. Keep is the default; Esc and a click outside cancel. Deleting ends a running session
first (its own tmux session, by exact name, and waits for its processes to exit), then moves Claude's transcript, the
folder beside it (subagents), the hub's record and the uploads into one new folder in `~/.Trash`, with the session's
own record and a note of where each item was, and forgets its hook events. Only transcripts the hub itself recorded for
the session are moved, and only when they resolve to a `<uuid>.jsonl` directly inside a folder of
`~/.claude/projects`; nothing from the request is used as a path. A session that is closing is refused. The
`/dismiss` endpoint the test scripts clean up with still removes without the Trash; the page no longer uses it.

The status mark is a pixel cube (`ui/orbs.js`, drawn on the 2px grain of the projects' marks, three faces in three
tones of one colour; every state is on one sheet in `docs/cube-states.html`). Each state has its own shape and at most
one motion, in whole pixels: working builds itself pixel by pixel from the bottom (thinking turns face by face,
compacting and closing settle from the top), jade; needs you lifts its lid, rose, the one that moves to draw the eye;
done is still with its top face lit, blue, and faint once you have read it on Home; idle and a terminal are still and
solid; ended, closed and not connected are an outline; an error has lost a block of its right face to the corner,
rose, and that block blinks while the API retries; a project that is dreaming shimmers across its faces, blue. With
reduced motion each holds still and still differs by shape (two thirds built, the lid up). A cube off screen doesn't
animate. Where a cube can't draw yet (the first moments of a page) the older marks stand in:

| Dot | Meaning |
|---|---|
| Pulsing jade square | Working |
| Rose diamond | Needs you |
| Rose cross | Error: the API stopped the turn, or Claude's process is gone. **Clear** in the pane's strip |
| Blue check | Done |
| Jade tray, pulsing | Closing: writing its handoff |
| Tray | Closed: a record you can read or resume |
| Hollow square | Ended on its own |
| `>` | Plain terminal |

A rename changes only the display name, kept with the icon in `~/.local/state/agent-hub/projects/`. The store, folders
and schedules keep their original names, so nothing that depends on them breaks.

## Dreams

The kit's launchd job checks each store once a day at one hour and dreams only when a dream is due. The switch on a
project's page and on Dreams turns that job on and off through the kit's `schedule-dream.sh`, and says the hour of the
daily check. Under it, both pages say why the last dream ran and what the last check found, in the kit's own words,
read from the kit's state file, `~/.local/state/agent-memory/<store>.json`: that a dream is running, with who and since
when; that an open proposal holds a due dream back, with the day its person was last reminded; that the check is
holding back after a try that wrote no proposal. Any key of the file may be missing or null (the last check is void
once a dream finishes), and until the kit has written the file the pages say nothing. A file the kit itself would refuse gets one
plain line saying so and where it is. The rule is the kit's (`load()` and `trusted()` in its `scripts/dream_due.py`),
mirrored in `dream_check_of` because the kit has no command that only reads the file; `tests/test_hub.py` compares the
two case by case. A job made before the daily check still runs on one weekday and reads that way ("Dreams Sundays
18:00") until you switch it off and on.

**Proposals, by the kit's record.** Which proposals a project has, and whether each is open, applied or dismissed, is
what the kit's `scripts/dream_proposal.py --json` says, in the order it says it. The hub doesn't sort file names (the
second proposal of a day is `<date>-2-proposal.md`), doesn't guess from the store's history, and doesn't list a file
in `dreams/` that the record doesn't know. A project has one open proposal at most:

- Only the open proposal can be ticked and applied. Apply opens a session that runs `/dream apply`, which closes the
  proposal itself. A closed one says when it was applied or dismissed.
- Once a proposal has been applied, each ticked item says what became of it, from the record the kit's
  `dream_apply.py` writes beside the proposal (`<stem>-applied.json`, joined to the items by the kit's item id, so an
  item rewritten after it was applied is another item): applied, with its
  commits; already there; refused, with the reason; yours to apply; waiting for the apply session. A ticked line the
  kit couldn't read as an item is listed too, so nothing ticked vanishes. Nothing about what was applied is guessed.
- **Yours to apply.** ORG.md and Steering are your tier: no agent applies them, and neither does the hub. The proposal
  shows the two commands, each with Copy: `chflags nouchg <store>/ORG.md`, then `python3 scripts/dream_apply.py --yours`
  in the project's folder, which shows each change, asks for a typed yes, and locks the file again. **Open a terminal
  for it** starts that second command in a terminal in the hub; while the lock is on it says so and stays open for
  you to lift it. The hub never lifts the lock for this.
- **Dismiss** closes the open proposal without applying it (`dream_proposal.py dismiss <name>`). It asks first and says
  what it means: nothing is applied, every item goes to the next dream, none counts as declined. What the kit prints
  is shown.
- Under the switch, Dreams says what closed proposals left, in three groups, sorted by the kit's own word for each
  item (`waiting` in the record) and by nothing else:
  - **Approved, not yet done**, oldest first: you approved it and it waits for you (ORG.md, Steering) or for a session
    (a skill, a scratchpad, an inbox item). It stays through later proposals until it is done or you take it back.
    Each says where it came from, how long ago, and what gets it done, in the kit's sentence; one whose proposal or
    record is gone says that nothing can do it any more. **Open a terminal for it** or **Start a session for it**
    (`/dream apply`, which does these with or without an open proposal) does it; **Take back** runs
    `dream_proposal.py withdraw` and shows the kit's answer. They can't be ticked again.
  - **Wait for the next dream**: not ticked, refused by the store, or in a proposal you dismissed.
  - **Declined**: passed over twice when a proposal was applied, so no dream proposes them again.
  An item is known by its proposal and its id, never by its number: scratchpad and inbox items have none. **May be proposed again** on a declined item
  runs `dream_proposal.py forget` for it and shows the kit's answer.
- **Dream now** first asks the kit who is dreaming (`dream_owner.py`) and which proposal is open (`dream_proposal.py`).
  If a dream is alive or a proposal is open, it shows that sentence and starts nothing. A dream that holds the project
  is also said on the Dreams page, with **Give it up** (`dream_owner.py release`), which asks first and shows what the
  kit discarded.
- What the kit's `memcheck.py` finds wrong with the store is listed on Dreams: a record of proposals it can't read, a
  proposal no dream finished, writes around the protocol. The hub had no place for these before.

A proposal reads as a code review. Its totals (proposed, ticked, facts carried) stay at the top while you scroll.
Changes are grouped by the file they change, in the order the dream wrote them, each group with its ticks and
**Viewed**: a viewed file folds, and viewed is kept in this browser for that proposal (`hub.viewed`). Under each
change is one faint line (how often it was seen, what it costs, and once applied what became of it); the dream's why
and its evidence are behind the chevron that starts the line. Apply and Dismiss stay docked at the bottom. What needs
you sits above the proposals: three at a time, then "more", with Done at the row's edge.

A proposal is read in the shape the kit's `/dream` writes it. Under each change:

- an **edit** shows the line it replaces, struck through, under the line that comes. The `replaces:` line is taken whole
- a **merge** shows the one line that stays and the opening words of the lines it stands for
- a **split** shows each new file and the lines that move there
- a decision, or an approach that worked, is an added line like any other

- a **pin** (an edit whose two lines differ only by ` (pin)` at the end) shows the line once, with the pin mark and
  "Pin" or "Unpin" in place of the struck-through line

Two sections sit beside the memory changes. Ticks in **ORG.md and Steering** are text only you change: applying leaves
them for you (see Yours to apply). **Claude's own notes** come from the notes Claude Code keeps by itself for a folder.
There, a carry item moves the checked facts of one note into memory, and each fact has its own switch: off turns its
`+` into a `-` in the proposal, so that fact is left out when the item is applied. That one character is all the hub
writes, on the open proposal only, refused if the file changed since you saw it (as a tick is). The kit reads every
sign as `+` when it names the item (`item_id`, mirrored in `hub_kit.py`), so switching a fact keeps the item the dream
wrote. Once applied, each fact says what the kit did with it (the `parts` of its row): written, already there,
refused, left out. **Unchecked facts from Claude's own notes** are ordinary add items nothing could check: your tick is
the check, and the hub never ticks one for you. Older proposals list the notes left behind as "Not carried over".

**Needs you.** What a dream found that needs you and isn't memory (a bug, a README to fix, a question only you can
answer) is kept by the kit in `dreams/needs.json` until you close it. Dreams lists each open one at the top, with the
proposal that said it and the evidence; the homepage counts them per project. **Done** runs the kit's
`dream_proposal.py done "<its words>"` and shows what the kit said; a dream that sees it again says it again. The hub
never writes the file, and never puts a tick box on these. In a proposal, its "Needs you" section shows only what isn't
listed at the top: lines closed since, and the ones a dream found put right.

**Read and not proposed.** Proposals written since 12 November 2026 have a file beside them, `<stem>-notes.md`, that
the dream writes and the kit commits with the proposal: each of Claude's own notes with its counts, what was stale,
what could not be checked, what was checked on this machine, what was turned away. It is shown, read only, at the
end of the proposal. It is not a proposal and is never listed as one.

**Pins.** A pinned entry is a line of `project/*.md` that ends ` (pin)`, at most 8 per store. Memory lists them under
**Pinned**, by title (no date, no suffix), and marks them in the file. Only a dream's edit that you tick adds or
removes one: the hub's editor refuses an ORG.md save that would change a pin.

While a folder of a project has such notes and Claude Code still keeps them, the project's page lists "Move Claude's own
notes into memory" under Finish setting up. Whether it still keeps them, and where, is the kit's rule and nobody
else's: the hub runs the kit's `scripts/own_notes.py` and reads its answer (managed settings, the folder's local and
shared settings, then your own; the `CLAUDE_CODE_DISABLE_AUTO_MEMORY` switch in an `env` block before
`autoMemoryEnabled`; on by default). The answer is kept until a file it rests on changes, and for a minute at most. An
index file with nothing in it isn't a note. Dreams says what is left to do: dream, then tick and apply, then run the
kit's `retire-auto-memory.sh <store>`, after which the row goes.

A dream job that can't be read shows no timing, and when the kit can't answer about the notes the row is left out. Each
is logged once per change of the file, and the other projects are unaffected.

**The change log.** Where the hub shows a memory write (a file's history in Memory, the activity lists), an edit shows
the line before and after and a removal the line that went. The kit packs those into the log row; its own
`memwrite.change` takes them apart, through `hub_kit.py`, and the hub never splits that text itself.

## Live status and review

Each session in `/api/state` carries what it is doing now. The hook (`hub_event.py`) records the tool that starts
and ends, the turn's start, a compaction and an API failure in the session's event file; the rest is read from the
transcript (from where the last poll stopped), git and lsof. git and lsof run in the background and the poll takes
their last answer, so a slow folder never slows the page. Fields that can't be known are null, never "working".

```
s.activity  {tool, target, since} | null   the tool running now, or the last one this turn (target: a path from the
                                           session's folder, a command, a pattern, a page)
s.turn      {since} | null                 when the current turn started; only while working
s.orb       working | reasoning | searching | using | compacting | waiting | idle | retrying
            reasoning between tools, searching for a read or search tool, using for any other, retrying while the API
            retries and for an error; waiting when it needs you; idle otherwise
s.state     also "error", with s.error: one sentence ("The API stopped this turn: 529 Overloaded.", "Claude stopped
            while it was working, and its process is gone.")
s.context   {used, limit} | null           tokens in the last reply's usage; 1M for current models, 200K for Haiku 4.5 and older
s.changes   {added, removed, files} | null against the last commit, staged or not, new files' lines included
s.ports     [int]                          what processes in the session's pane listen on (not Claude, not its shell)
s.retry     {said, attempt, of}            only while the API is retrying
```

**Is it alive.** A Claude session runs under its pane's `zsh -c "claude …; exec zsh -l"` (tmux runs it in its default shell). Once the shell has taken the
pane over, Claude is gone; if the hooks last said it was working or asking, that is an error, not "working". Esc stops
a turn without the Stop hook, so a "working" the hooks left behind is checked against Claude's own status file
(`~/.claude/sessions/<pid>.json`): idle since after the last event, with nothing on screen still running or asking,
reads as finished. **Clear** marks the error seen; the session reads idle again (or "Claude isn't running").

**The pane strip.** At each Claude pane's foot: orb · tool · target · the turn's time · `esc` Stop (sends Esc). It
says Needs you with the question, Error with its sentence and Clear, Idle otherwise, and keeps its height so the
terminal never resizes as turns start and end. The header's dot stands down while the strip shows.

**The right panel's rail.** Changes (`+42 −7 · 5 files`, opening the review), Context (a meter, its share of the window)
and Ports, each with Open (on this host, in a new tab) and Stop. Stop asks first, then stops only processes in that
session's pane: never Claude, its shell, this hub, or another session's process; anything else is refused.

**The review.** Over the session's panes: the files on the left, every diff in one scroll, **Viewed** per file (kept
in the browser until the file changes again). git is read only: `diff`, `--numstat`, `ls-files` and `rev-parse`, with
no optional locks, no external diff or textconv driver and no fsmonitor. A file is shown only if git lists it as
changed; a path from the page must be one of those, and git internals, keys and dotenv files are listed by name with no
content. Click a line number (or the **+** beside a line) to comment; **Send N comments** pastes them into the
session as one message, `path:line`, the line quoted, your words, the way Close sends its prompt.

**Peek.** `window.hubPeek(id)` (Space on a sidebar row) opens a popover beside the row, or a sheet on a phone: the
last reply, the choice Claude is showing, or the error with Clear. ↵ sends the reply box as one paste; a number key
answers a numbered choice (a permission prompt gets the key, a numbered question in a reply gets the number as the
reply). It refuses to paste while something is typed in the session, and a grey suggestion in Claude's input isn't
taken for typing (`typed_now` reads the screen with its colours).

## What a session shares

Links and file paths on screen are clickable, including ones wrapped across two rows. Paste or drop a file into a
terminal to share it with Claude.

**The viewer.** Everything a session shares opens in one dark, full-screen frame, with the file's name, time and size
at the top left. An image fits the screen; `+` `-` (or the buttons, a pinch, ⌘-scroll) zoom, `0` fits and `1` is actual
size, a drag pans, a double-click zooms where you clicked, and past 200% the pixels stay square so you can check
alignment. ←/→ (or a swipe on a phone) and the filmstrip move through all of the session's images, whether you opened
one from the terminal, the Shared panel, a pasted image in a record, or the picture that pops up when a new one
arrives. Video, audio, PDFs and text (with line numbers) open in the same frame. `esc` closes it.

**Hover cards.** Resting the pointer on a path in a terminal shows what it is: an image's picture, size and pixels; a
text file's lines, size and first lines; when it changed and where it lives. Resting on a link shows its card: title,
description, site and picture, as the page describes itself.

**The Shared panel** lists the chat's images, files, audio and links, read from the transcript, as a timeline, newest
first: one time per turn, images as a row of thumbnails (small copies made once with `sips` and kept under the hub's
state folder), the rest one row each. A link shows its title and icon once its card has been fetched.

Only a session's project, its uploads, the Claude scratch space, Downloads and Desktop can be opened, never keys, dotenv
files or git internals. Local files open sandboxed.

**Remote images and links.** The page never loads anything from the web: its policy is `img-src 'self' data:`, because a
chat could otherwise send data out through an image address. Clicking a link opens it in a new tab. A link's card is
fetched by the hub (`hub_preview.py`), and only when you rest the pointer on that link (350 ms) or focus it: never
because a link appeared in a transcript, and never for the list in the Shared panel, which only asks the hub which
cards it already has (`/api/preview/cached`, which fetches nothing). The card's picture and icon come through the hub
too (`/api/preview/image`), and only pictures a fetched card named. Every fetch, at every hop of a redirect, keeps to:

- http and https only, on ports 80, 443, 8080 and 8443, with no name or password in the address;
- the name is resolved by the hub and every address it gives must be public: nothing on this Mac (loopback), the home
  network (private ranges), link-local (cloud metadata), multicast, reserved space or the tailnet's `100.64.0.0/10`,
  IPv4 hidden in IPv6 included. The connection goes to the address that was checked, so a second lookup can't swap one in;
- two seconds for the whole fetch, at most 512 KB read (a page is read up to that point; a bigger picture is refused),
  web pages only for a card, and PNG, JPEG, GIF, WebP and ICO pictures checked by their first bytes (never SVG);
- no cookies, no referrer, and a User-Agent (`Mozilla/5.0 (compatible; LinkPreview)`) that names no one.

Cards are kept for a week and refusals for ten minutes, under `previews/` in the hub's state folder. A request for a
card from another site in the browser (`Sec-Fetch-Site: cross-site`) is refused. A card does still tell the page's
owner that your Mac looked at that address, once, when you pointed at it.

## Remote Control

Every Claude session starts with `claude --remote-control "<title>"`. The phone link comes from
`bridgeSessionId` in `~/.claude/sessions/<pid>.json`, a Claude Code file with no documented format, so a future
version could move it. If a session isn't connected, its title bar shows **Connect phone**.

Dreams run with `claude -p`, which has no interactive session, so they don't appear on the phone.
**Apply** opens a normal session, which does.

## Access

The terminal is full shell access. The server only listens on 127.0.0.1. `~/.config/agent-hub/config.json` (mode 600,
written by `install.sh`) says how it is reached, in one of two ways:

- **On this Mac only** (`"key"`, no `"login"`): open `<origin>/?key=<key>` once; the hub answers with a cookie that
  holds the key (HttpOnly, SameSite=Strict, this origin only) and from then on every request needs it, the app's Host,
  and for writes and terminals the app's exact Origin. Anyone who can read the config file, or open the key's address
  from this browser's history, can get in: treat the key like a password, and run `sh install.sh --new-key` to change it.
- **Behind Tailscale Serve** (`"login"`): Tailscale Serve is the only way in, from your other devices too. Every
  request needs the owner's `Tailscale-User-Login`, the app's Host, and for writes and terminals the app's exact
  Origin.

Either way, programs already running on this Mac as you can reach loopback and read the config, as they could use your
shell anyway. Other accounts on the same Mac can reach loopback too; the key (or the login) is what keeps them out.

**What the owner check protects against, and what it doesn't.** Every request needs three values: the owner's login
(or the key), the app's Host, and for writes the app's Origin. That keeps out other people on the tailnet and other
sites in your browser. It does not keep out a program running as you on this Mac: the three values sit in
`~/.config/agent-hub/config.json`, which any such program can read, so an agent session here could call the hub with
`curl` and do what the kit keeps agents from doing directly: save `ORG.md`, dismiss a proposal, take back or re-allow
an item, give up a dream. A header can't tell you from a program running as you, and the hub doesn't pretend to with a
secret the same program could read. What it does instead:

- Everything done through the hub that only you may do is written down (`person-actions.jsonl` in the hub's state
  folder) with the time and the store commit it left, and Dreams lists **Done in your name** from the store's own
  history, marking what came through the hub. A line you don't recognise is how you would notice.
- An `ORG.md` save is sealed by the kit (`memcheck.py --seal`: a log row and a commit under the store's lock). If the
  kit can't seal it, the file is put back and you are told. If principles are ever left unlocked, the kit's check
  says so on Dreams.
- One function, `the_person` in `hub_data.py`, is asked before each of these. Today it decides nothing more than the
  three values already did. A real proof that you are there (a password, Touch ID) goes in that one place.

**Repos the hub never runs git or jj in.** `NO_VCS` in `hub_base.py` is the config's `"no_vcs"` list: repos that
aren't only yours, such as a partner's repo where the kit's files stay untracked on purpose, or a shared company
monorepo (each a path from the home folder, or a whole path). Every git call the hub makes goes through `run` in `hub_base.py` or `git` in `hub_diff.py`, and both refuse
one aimed at those folders or anything inside them, so nothing is committed there and no worktree is made there. Their
sessions show no Changes, the project page shows no git log for them, "Commit the kit" is never offered, and a
session of its own (which needs a worktree) is refused with a sentence. Writing a file there, such as Steering, is
still the hub's to do. `tests/guards_check.py` checks this offline.

**Steering in a store with several repos.** The Steering block lives in `.claude/commands/dream.md` of every repo of a
store and must be the same in each. A save writes all of them or none: every file is read first (it must be there,
its block must be the one you opened, and the blocks must agree), the new files are written beside the old ones,
then swapped in, and if a swap fails the ones already swapped go back. Blocks that differ between repos are refused
rather than overwritten.

## What stays manual

- Answering Claude's first-run questions in a new folder (trust, MCP servers). The session shows as needing you.
- Writing `ORG.md` and Steering.
- Ticking a proposal's changes.
- Applying approved `ORG.md` and Steering changes: lifting the lock and running the kit's `dream_apply.py --yours`.
- Running `retire-auto-memory.sh` once a dream has carried Claude's own notes over.
- Removing a worktree you're finished with. Closing a session keeps its worktree and `hub/<name>` branch, so no work is lost.

## Deploying a new version

- Restart the server: `launchctl kickstart -k gui/$(id -u)/ai.agent-hub`.
- Open tabs are told. Each poll carries the build the server runs; a page from an earlier build shows "Dream Hub was
  updated" with **Reload**. Until it is reloaded it may show what the new server no longer accepts, and get a refusal.
- Proposals from before the kit kept a record are not in the kit's record. They are listed on Dreams under **Earlier**, to read; nothing in them can be ticked or applied.
- Deploy the kit first, or together: without the kit's `dream_proposal.py` and `own_notes.py` the hub says so in
  place of the proposals and leaves the "Claude's own notes" row out.

## Tests

`tests/guards_check.py` is offline: no server, no Claude session (`/usr/bin/python3 tests/guards_check.py`, with the
six variables of a second instance set). The others need the app running, and start real, short Claude sessions (which
cost usage and also appear on the phone while they run).

    /usr/bin/python3 tests/test_hub.py
    /usr/bin/python3 tests/browser_check.py <folder-for-screenshots>
    /usr/bin/python3 tests/lifecycle_check.py
    /usr/bin/python3 tests/status_check.py

They run only against a second instance: all six variables, with `HUB_JOBS` a throwaway folder and a port, state
folder and tmux socket that are not the installed hub's (`tests/instance.py` compares the folders as they really are,
and stops with a sentence otherwise). Start one as above and run them with the same variables:

    export HUB_PORT=8492 HUB_STATE=/tmp/hub2/state HUB_TMUX=agent-hub-2 HUB_CONFIG=/tmp/hub2/config.json HUB_KIT=$(cd ../agent-memory-kit && pwd) HUB_JOBS=/tmp/hub2/jobs
    /usr/bin/python3 tests/test_hub.py

Each makes a throwaway project, `hub-test-throwaway-<pid>` (a folder in your projects folder, a store in `~/agent-memory`),
works in it and removes it, along with what the kit and Claude Code kept for it, whether or not the server is still
answering. The store holds a note, `THROWAWAY-TEST-STORE.md`, saying what it is. These can't live anywhere else: the
kit makes every store in `~/agent-memory`, and the Claude sessions the tests start need the real home folder to be
signed in. So don't sync the kit into a store of that name while a test runs. `/usr/bin/python3 tests/sweep.py` removes
the ones whose test run is no longer going, and says which; each test script does the same when it starts, and ends by checking that
launchd's folder holds what it held before: no test makes a dream job there. Dream jobs are written by the tests into
the instance's own folder. The project's alert script is swapped for one that sends nothing, so a test dream never
reaches a phone.

Dreams, proposals and their states are made by the kit's own scripts in the throwaway project: its collector and
`dream_finish.py` (with the proposal's text supplied in place of the model's), `dream_apply.py`, its `--yours` pass in a
hub terminal, `dream_proposal.py applied`, the hub's own Dismiss, and `dream_due.py`. A prompt goes to a session as one
paste, the way the hub's own Close does, because typed keys that long arrive as a paste and Claude keeps only the tail. No test asks a real project or store for anything. Claude Code's
own entry for the throwaway folder in `~/.claude.json` is left, because that file isn't the hub's to edit.

`status_check.py` checks every field above against real sessions (a tool running, a turn finishing, an API failure, a
killed Claude), the review's path guards, Stop's reach, peek's answers and review comments; its first block reads screens
and patches without a session. `browser_check.py` opens the origin in the instance's config in headless Chrome and adds the owner's login header
itself, as Tailscale Serve does for a hub behind it. It needs Google Chrome in `/Applications`.

The link-card guards are tested in `test_hub.py` without the internet: every kind of address the hub refuses, at the
first hop and after a redirect, against a small web server on loopback that the test lets through under one made-up
name only (an oversized page, a non-page, a slow page, a page that trickles, pictures that are too big, SVG or not what
they say).

## Known limits

- One Mac only, and macOS only (launchd, `sips`, `chflags`, `lsof` as macOS ships it).
- Terminal state on a phone browser is workable, but the Claude app through Remote Control is the better phone route.
- Worktrees are made with plain `git worktree add`, because `claude --worktree` didn't get the kit's memory link.

<h1 align="center">Dream Hub</h1>

<p align="center">
  <strong>Shared memory and a control room for Claude Code agents</strong>
</p>

<p align="center">
  One memory for every agent on a project, written through one checked command and approved by you.<br/>
  Shared memory &middot; Daily dreams &middot; Proposals you tick &middot; Browser terminals &middot; Session archive.<br/>
  <strong>100% local. No cloud service, no telemetry. Your memory is markdown in your own git repo.</strong>
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue.svg" alt="MIT License" /></a>
  <img src="https://img.shields.io/badge/macOS-only-lightgrey" alt="macOS" />
  <img src="https://img.shields.io/badge/Python-3.9+-3776ab" alt="Python 3.9+" />
  <img src="https://img.shields.io/badge/dependencies-none-brightgreen" alt="No dependencies" />
  <a href="https://docs.claude.com/en/docs/claude-code"><img src="https://img.shields.io/badge/for-Claude_Code-d97757" alt="For Claude Code" /></a>
</p>

![Dream Hub home: what needs you, the projects, recent memory writes](docs/screenshots/home.png)

---

## What is Dream Hub?

Agents working on one project keep rediscovering the same traps: the test that needs an environment variable, the build that runs out of memory, the decision someone already made and why. **Dream Hub** gives them a memory they share. It lives in a private git repository of plain markdown, is loaded into every session at start, and is written through one guarded command. Once a day, if enough has happened, a "dream" reads the recent sessions and proposes what memory should gain, change or lose. You tick what you agree with, and only that is applied.

A local web page, the hub, runs your Claude Code sessions in browser terminals and puts proposals, memory and "what needs you" in one place.

> **Independent project.** By [Christian Almurr](https://github.com/ChristianAlmurr). Not affiliated with or endorsed by Anthropic. Built for daily use on one Mac, made public. It starts real Claude Code sessions (a dream is one), and those use your Claude plan or API credit like any other.

---

## Features

### Shared Memory
A store is a folder of markdown with its own git history: `ORG.md` (your principles), `project/gotchas.md`, `decisions.md`, `architecture.md`, scratchpads and an inbox. Every session starts with your principles, what matters in this project and an index of the rest, in at most 9,500 characters. Agents grep for the rest.

![Memory](docs/screenshots/memory.png)

### Guarded Writes
One command writes memory. It refuses a line that looks like a credential or an injected instruction, a line that is too long, and a line that says what another already says. A write made from a stale read is refused, not merged, so several agents can write at once. Every write is a git commit with who wrote it and why, and can be undone.

### Daily Dreams
A dream collects the sessions since the last one (slimmed, credentials removed), has analyst subagents read them in parallel, checks memory against the code, and writes one proposal. A day with nothing due costs nothing.

### Proposals You Decide
The proposal reads like a code review: changes grouped by file, each with why, how often it was seen, what it costs and its evidence. Tick what you agree with. Applying writes each ticked change as its own checked commit. Your principles are yours alone: no agent and no dream can change them.

![A dream's proposal](docs/screenshots/dreams.png)

### Sessions in the Browser
Start Claude Code sessions from the hub, pick a mode and a model, and follow them in tmux terminals that keep running when you close the tab. Each shows its state as a small pixel cube. Delete a session safely: it goes to the Trash with a warning that says what a dream has and hasn't read.

![A project page](docs/screenshots/project.png)

### Session Archive
Claude Code deletes transcripts after 30 days. The kit keeps a scrubbed copy of each session for 180 days, searchable with `past.py`. One store's archive never returns another's content.

### And More

| Feature | Description |
|---------|-------------|
| **Needs You** | One list of what only a person can do: proposals to tick, a login that ran out, a failed dream |
| **Steering** | Say per project what dreams should care about, and what they should ignore |
| **`amk status`** | Every store on one line, or one in full as JSON |
| **Phone access** | Reach the hub over Tailscale Serve with your Tailscale login |
| **Update notice** | A daily check against this repo, with an Update button; nothing installs without your click |
| **Alerts** | A failed dream tells you, by macOS notification or your own script |
| **Offline tests** | The kit's rules run in a throwaway HOME, about seven minutes, no cost |

---

## Screenshots

| View | Preview |
|------|---------|
| Home | ![Home](docs/screenshots/home.png) |
| Project | ![Project](docs/screenshots/project.png) |
| Memory | ![Memory](docs/screenshots/memory.png) |
| Dream proposal | ![Dreams](docs/screenshots/dreams.png) |
| Delete a session | ![Delete](docs/screenshots/delete.png) |
| Update notice | ![Update](docs/screenshots/update.png) |

---

## Quick Start

### Prerequisites

- **macOS**
- **[Claude Code](https://docs.claude.com/en/docs/claude-code)**, signed in
- **git** and **tmux** 3.2+
- **Apple's Python 3.9+** at `/usr/bin/python3` (comes with `xcode-select --install`)
- Optional: `jj`, Tailscale (phone access), Google Chrome (only for the hub's browser test)

### Installation

```sh
# Clone the repository
git clone https://github.com/ChristianAlmurr/dream-hub.git
cd dream-hub

# Install, and attach one repo to a memory store
sh install.sh --repo ~/code/my-app
```

`install.sh` asks before each thing it changes outside the clone, never uses `sudo`, and can be run again. At the end it prints one address to open once:

```
http://127.0.0.1:8491/?key=<your key>
```

Your browser keeps the key, and from then on `http://127.0.0.1:8491` is the hub. Then, once per repo:

1. Start Claude there once and accept the trust dialog: `cd ~/code/my-app && claude`.
2. On the project's Memory page, write your principles in `ORG.md` and what matters in this project under Steering.
3. Start a session from the hub, or press **Dream now** once a few sessions have happened.

The install steps, the first dream, unattended dreams (they need `claude setup-token`) and the details are in the [full guide](docs/guide.md#quick-start).

### Available Commands

| Command | What it does |
|---------|--------------|
| `sh install.sh [--repo <path>]` | Install the hub and optionally attach a repo |
| `sh update.sh` | Move to the newest version, re-sync the kit, restart the hub |
| `sh uninstall.sh` | Remove the hub's job, settings and state, step by step; keeps your memory stores |
| `agent-memory-kit/bin/amk status` | Every store on one line |
| `agent-memory-kit/bin/amk help` | All verbs: `read`, `write`, `edit`, `delete`, `undo`, `past`, `check`, `due`, `proposal`, `apply` |
| `/dream`, `/dream apply` | In a Claude session in an attached repo: run a dream, apply a ticked proposal |

---

## Architecture

### Tech Stack

| Layer | Technology |
|-------|------------|
| Hub server | Python standard library, on loopback; no build step |
| Hub UI | Plain JavaScript and CSS, [xterm.js](https://github.com/xtermjs/xterm.js) for terminals |
| Sessions | `claude` in tmux, started with `--remote-control` |
| Memory | Markdown files, one git repository per store |
| Scheduling | launchd, one daily check per store |
| Dependencies | None to install |

### Data Flow

```
  you ── tick, apply, write ORG.md ─────────────┐
                                                 │
  ┌───────────── the hub (localhost web page) ───┼─────────────┐
  │ sessions in tmux · memory · proposals · needs you          │
  └──────┬──────────────────────────────▲────────┼─────────────┘
         │ starts                       │ reads  │
         ▼                              │        ▼
  Claude Code sessions ── memwrite.py ──► store: ~/agent-memory/<project>/  (its own git repo)
  in your repo           (guarded write)   ORG.md · STEERING · INDEX.md · project/*.md · CHANGELOG.md
         ▲                                 │ dreams/<date>-proposal.md
         │ session-start hook loads        │
         └──── ORG.md + Steering + index ◄─┘
                                           ▲
  daily check (launchd) ── run-dream.sh ── a dream: collect sessions → analysts → proposal
```

More in [How it works](docs/how-it-works.md).

### Safety

- **A person-only tier.** `ORG.md` and Steering are locked at the OS level and denied to agents by settings and a Bash hook. A determined agent writing through a script can get past a hook that reads text, so a check reports any change at the next session start.
- **Memory is data, not instructions.** Sessions are told text from web pages and tool output is data; dream analysts treat transcripts the same way.
- **Unattended dreams** run with the narrowest permissions that work: they may write their proposal and nothing else.
- **What leaves your Mac:** only what Claude Code itself sends, plus one daily `git ls-remote` for the update notice (turn it off with `"updates": false`). No telemetry.
- **The hub is a shell.** Its terminals are full shell access as you. It listens on 127.0.0.1 only and needs a key.

The full model is in the [guide](docs/guide.md#safety-model).

---

## Configuration

Settings live in `~/.config/agent-hub/config.json`: port, projects folder, kit location, `path`, `updates`, `no_vcs` (repos the hub never runs git in) and `private_folders`. Alerts, what a store refuses and the token for unattended dreams are per store or per Mac. The whole table is in the [guide](docs/guide.md#configuration).

---

## Testing

```sh
sh agent-memory-kit/tests/test-kit.sh   # every rule, offline, throwaway HOME, no cost
```

`agent-memory-kit/tests/test-live.sh` and the hub's browser and lifecycle checks start real Claude sessions and spend your usage; see [hub/README.md](hub/README.md#tests).

---

## Roadmap and Known Limits

The kit copies its files into each attached repo today. The plan is "one kit, nothing in repos": install once as a Claude Code plugin and tie each repo to its store with a git config key. Also not built: a Linux port, proof of presence (a password or Touch ID) for the person's tier, and stores shared between machines. The [guide](docs/guide.md#roadmap) has the steps and the limits.

---

## Contributing

This is one person's tool made public. Issues with a clear description of what you ran and what happened are welcome. A first install on someone else's Mac has not been tested, so reports from one are the most useful thing you can send. Open an issue before a large pull request.

---

## License

[MIT](LICENSE). Third-party files keep their own licences: see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

Inspired by Anthropic's "The components of agent memory". Uninstall, updates and troubleshooting are in the [guide](docs/guide.md).

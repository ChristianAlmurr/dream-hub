# Dream Hub: the hub's design brief

A private console on your Mac: start projects on the memory kit, read their memory, run and review
dreams, and run several Claude sessions in browser terminals that also appear on the phone.

## What comparable tools taught us

| Source | Lesson | What we do |
|---|---|---|
| [Conductor](https://rustman-blog.pages.dev/wiki/conductor-parallel-agents), [Claude Squad](https://www.codeagentswarm.com/en/guides/best-tools-to-run-multiple-ai-coding-agents) | One git worktree per agent is the shared baseline; tmux keeps sessions alive | A tmux session and, for parallel work, a worktree per agent |
| [Agent comparison](https://nimbalyst-website.pages.dev/blog/best-agent-management-tools-2026/) | Single-repo tools fall short across several projects | Projects are the top level; Now spans all of them |
| [Supervision surfaces](https://blakecrosley.com/blog/agents-need-supervision-surfaces) | Show attention, not activity; few states; approvals as a queue, not modals | Now lists what needs you. Three states: working, needs you, finished |
| [Agent dashboards](https://www.lowcode.agency/blog/how-to-build-an-ai-agent-orchestration-dashboard-for-complex-workflows) | Each review item carries the agent, decision, context and controls; status within seconds | Every proposed change shows the line, why, how often and its cost. State refreshes every 3 seconds |
| [Humans as participants](https://agentensemble.net/blog/human-participation/) | Reserve humans for real authority; send a full snapshot on reconnect | Only `ORG.md` and dream approval are manual. Each poll is a full snapshot |
| [Claude Code agent view](https://code.claude.com/docs/en/agent-view.md) | Group by Needs input / Working / Completed | Same grouping, across projects |
| [Remote Control docs](https://code.claude.com/docs/en/remote-control) | `--remote-control <name>`; the terminal must stay open; run inside tmux | Every session starts with it and shows its phone link |
| [xterm.js notes](https://claudepluginhub.com/skills/ggprompts-xterm-js-plugins-xterm-js/xterm-js) | No zero-size containers; fit to the container; debounce resize | Terminals stay mounted while open; fit is debounced |
| Mobbin: [Stripe Workbench](https://mobbin.com/screens/19e7f5ce-a94b-43a4-addc-dd88fb128182), [Gumloop tasks](https://mobbin.com/screens/f65590b4-be93-42d7-9de6-960f6e5ddcc6), [Plain](https://mobbin.com/screens/9f3d27b2-3693-4517-83f3-98ff87ecb18a), [Browserbase](https://mobbin.com/screens/91fff3d4-82a6-4d59-95bd-0da39f7d3d51) | One calm left rail; list beside detail; filter by outcome | One rail, list and detail in Memory and Dreams |

The sources on agent dashboards are practitioner opinion, not usability studies. Nothing useful turned up on
phone keyboards for web terminals, which is partly why the phone route is Remote Control.

## The redesign (Stageshow craft rules, November 2026)

[Stageshow](https://github.com/benjitaylor/skills) makes illustrations, so only its craft rules apply here. The author chose each row below.

| Decision | Choice |
|---|---|
| Focus | The live agents board fills a project page; the prompt docks beneath it |
| Look | Sharp shell on midnight ink. Hairline controls fill from the left on hover. Home and each project are dashboards beside the left sidebar, with no right column; only sessions have a right panel |
| Colour | Cobalt acts and selects, jade is work in progress, rose needs you, each project has its own identity colour. Chart colours pass the colour-blind checks in their fixed order |
| Type | Schibsted Grotesk for labels; JetBrains Mono for paths, models, numbers and the terminal |
| Identity | A pixel glyph generated from the project name, or an uploaded image |
| Input | Keyboard first, with this page's keys on a status line and `?` for all of them |

From Stageshow: one solid primary per screen, real words only where the eye lands, one number gets to be big, every list on one column grid, motion only as a response to something.

## Principles for this app

1. Now answers one question: what needs me? When nothing does, it stays empty.
2. Three session states, and no feed of activity.
3. Every Claude session is on the phone. If one isn't, the app says so and offers to connect it.
4. Code does what a rule can decide; the human tier stays human.
5. One name per place, one nav, nothing internal opens a new tab, no eyebrows, no labels that state the obvious.

## Safety model

The terminal is full shell access, so:

- The server binds to 127.0.0.1 and is reached only through Tailscale Serve.
- Every request and terminal connection must carry the owner's `Tailscale-User-Login`. Being on the tailnet isn't enough: another person's device on it is a different user. (Without Tailscale, the hub runs on this Mac only and a key in a cookie takes the login's place.)
- Writes and WebSocket upgrades must carry the exact app Origin, which stops other sites driving it.
- Memory and proposals contain text from transcripts and the web, so the UI escapes all of it.
- Separate from any other app on the Mac that other people share.

## Acceptance checks

- A request without the owner's identity or with the wrong Origin is refused, including WebSocket upgrades.
- A session started here shows a working claude.ai/code link.
- Closing the browser leaves sessions running; reopening reattaches.
- Ticking a change writes the proposal file; a stale tick is refused.
- Paths outside the store can't be read or written.

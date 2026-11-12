#!/usr/bin/env python3
"""Turn one Claude Code transcript into slimmed text plus counts. Not run by itself.

The dream batch (collect_transcripts.py) and the session archive (archive.py) both read
transcripts through here, so they agree on what a session is and on what is in scope:

- Entries are matched by the `cwd` recorded on each one. Only entries inside this store's
  repos or their git worktrees are kept, so nothing private from elsewhere is carried along,
  even when one session touched both.
- A /dream session and the analysts it started are left out, so memory doesn't learn from itself.
- A transcript is read a line at a time, and one line that isn't an entry is skipped, not fatal.

What comes out is a list of entries, each labelled as what it is (LABELS below). Only what the
person typed is a `**user**:` entry: the transcript says where a message came from, and a tool
result, a summary Claude Code wrote, a hook's note, another agent's message or a script's prompt
is never filed under the person. The transcript's bookkeeping (snapshots, API requests, tool
lists) is left out.

As text, an entry is its label and what it holds, and entries are a blank line apart. Inside an
entry every kind of line end is a plain newline and no line is empty, so a label is one only at
the start of the text or after a blank line: `**user**:` printed by a tool, behind a carriage
return or a form feed, is that tool's output.

Slimming shortens what is bulky by kind (LIMITS): a tool result keeps its first and last lines,
what the person and the assistant said stays whole. Every cut says how much it left out.

A session is only cut further when it is over the size it is given (`fit`), and then the least
valuable text goes first, in the order of LADDER, until it fits. The result says what went.
`parts` splits a session into pieces of a readable size, never between a call and its result.

A dream reads less than the archive keeps (`dream`): a subagent's run as a digest, results of
tools that only read or write files as a size, a call repeated in a row once. `files` writes a
session for the dream batch that way and `groups` says which files one analyst gets. `working`
is the one definition of a session in which work was done.
"""
import collections, datetime, functools, json, os, re, subprocess
from pathlib import Path

import verbs  # the amk verbs, so a verb is kit work exactly when its script is
import store as stores  # store.py: which store a repo's key names. `store` here is the function that writes a session's files

ROOT = Path(__file__).resolve().parent.parent
MEM = Path(os.environ.get("AGENT_MEMORY_DIR", ROOT / "memory")).expanduser().resolve()
MEMORY_FILE = re.compile(r"(memory|agent-memory/[^/]+)/(project|agents)/")
# A line of a transcript ends at a newline only: the JSON of one entry can hold U+2028 and the like, which must not split it.
LINES = dict(encoding="utf-8", errors="ignore", newline="\n")
PART = 40_000  # characters in one part of a session: what an analyst reads in one go
DREAM_CHARS = 240_000  # the most one session takes up in a dream batch, six parts
GROUP = 250_000  # characters of batch files one analyst is given
TALK = 4_000  # a session in which no tool ran is still one in which work was done when it is this long
BLOCK = 4_000  # characters of a session that are left out together when it has to be thinned
CUT = " … [{:,} characters cut] … "
DROPPED = "[{:,} characters dropped]"
LEFT = "[… {} left out here: {:,} characters …]"
SPARE = 40  # a cut that saves less than this is not worth its marker
ASIDE = "said-in-short-sessions.md"  # in a dream batch: what the person typed in sessions where no work was done

USER, ANSWER, ASSISTANT, CALL, RESULT, ERROR = "user", "user answer", "assistant", "tool call", "tool result", "tool error"
REPORT, TASK, AGENT, PROMPT = "subagent report", "background task", "agent message", "headless prompt"
SUMMARY, NOTE, FAULT, GAP = "compaction summary", "system note", "system error", "left out"
LABELS = {
    USER: "what the person typed, and nothing else",
    ANSWER: "what the person chose when the agent asked them a question",
    ASSISTANT: "what the agent said",
    CALL: "a tool the agent ran, with its input",
    RESULT: "what the tool gave back",
    ERROR: "a tool call that failed, with the error",
    REPORT: "the final report of a subagent the agent started",
    TASK: "a background task (a subagent or a command) telling the agent it finished",
    AGENT: "a message from another agent: the task a subagent was given, or a message from the session that started it",
    PROMPT: "the prompt of a run nobody typed into a terminal: given by a script or a schedule, or by an app passing on what a person typed into it",
    SUMMARY: "Claude Code's own summary of the conversation so far, written when it ran out of room",
    NOTE: "something Claude Code or a hook put into the conversation: a reminder, a hook's context, a command's output",
    FAULT: "Claude Code itself failing: the API could not be reached, a stop hook broke",
}
# How the line that says what was trimmed names one entry of a kind, and several.
NAMES = {USER: ("user message", "user messages"), ANSWER: ("user answer", "user answers"), ASSISTANT: ("assistant message", "assistant messages"),
         CALL: ("tool call", "tool calls"), RESULT: ("tool result", "tool results"), ERROR: ("tool error", "tool errors"),
         REPORT: ("subagent report", "subagent reports"), TASK: ("background task message", "background task messages"),
         AGENT: ("agent message", "agent messages"), PROMPT: ("headless prompt", "headless prompts"),
         SUMMARY: ("compaction summary", "compaction summaries"), NOTE: ("system note", "system notes"), FAULT: ("system error", "system errors")}
# The most characters one entry keeps, by what it is. What is said stays whole unless it is enormous; what tools print is bulk.
LIMITS = {USER: 20_000, ANSWER: 20_000, ASSISTANT: 10_000, AGENT: 10_000, PROMPT: 6_000, CALL: 400, RESULT: 300, ERROR: 1_000,
          REPORT: 2_000, TASK: 1_500, SUMMARY: 1_500, NOTE: 300, FAULT: 1_000}
# What a dream keeps of the kinds that are mostly one agent telling another what it did.
DREAM_LIMITS = {AGENT: 2_000, TASK: 400}
DIGEST_SAID = 400  # what a subagent said along the way, in the digest of its run
# Notes a dream is not given: the same hook context and reminders in every session, and the size of a picture. `tag` says which a note is.
HOOK, REMINDER, IMAGE = "hook", "reminder", "image"
# Tools whose result a dream gets as a size only: what they read or wrote is in the repo. `tag` holds a call's or a result's tool.
FILE_TOOLS = ("Read", "Grep", "Glob", "Edit", "Write", "TodoWrite", "TaskCreate", "TaskUpdate")
# What comes back from a call, and so belongs with it.
BACK = (RESULT, ERROR, REPORT, ANSWER)
# Where the transcript says a message came from (`origin.kind`), and what that makes it.
SAID_BY = {"human": USER, "task-notification": TASK, "peer": AGENT, "coordinator": AGENT}
# `promptSource` values that say who wrote a message, so its text is taken whole as theirs.
MARKED = ("typed", "queued", "sdk")
# What Claude Code itself writes into a message of the person's: a reminder, and the notice that they pressed Esc.
HARNESS = re.compile(r"<system-reminder>.*?</system-reminder>|\[Request interrupted by user[^\]\n]*\]", re.S)
# Everything a reader or Python may take for the end of a line: carriage return, vertical tab, form feed, the
# information separators, next line, line separator, paragraph separator. Inside an entry each becomes a newline.
LINE_ENDS = re.compile("\r\n?|[\x0b\x0c\x1c\x1d\x1e\x85  ]")
# Characters that show nothing: whitespace, controls, format characters (zero-width space and joiners, direction marks,
# BOM, soft hyphen, variation selectors, tags) and the letters that are blank by design (Hangul fillers, braille blank).
NOTHING = ("\x00-\x09\x0b-\x20\x7f-\xa0\xad\u034f\u061c\u115f\u1160\u1680\u180e\u2000-\u200f\u2028-\u202f\u205f-\u206f\u2800\u3000\u3164"
           "\ufe00-\ufe0f\ufeff\uffa0\ufff9-\ufffb\U0001d173-\U0001d17a\U000e0001\U000e0020-\U000e007f")
# Lines with nothing to see on them. Inside an entry they are taken out, so a blank line always means: a new entry starts here.
BLANK = re.compile(f"\n(?:[{NOTHING}]*\n)+")
EDGES = re.compile(f"\\A[{NOTHING}\n]+|[{NOTHING}\n]+\\Z")
HOLLOW = re.compile(f"[{NOTHING}]*")
COMMAND = re.compile(r"<command-name>(.*?)</command-name>", re.S)
ARGUMENTS = re.compile(r"<command-args>(.*?)</command-args>", re.S)
SHELL = re.compile(r"<bash-input>(.*?)</bash-input>", re.S)
# How Claude Code ends its notice that the person turned a tool call down, when they typed why.
REASON = "the user said:\n"
ASK = "AskUserQuestion"  # the tool through which the agent asks the person


class Entry(collections.namedtuple("Entry", "kind head tail cut tag left", defaults=("", "", 0, "", 0))):
    """One labelled piece of a session.

    When characters were cut out of its middle, `cut` counts them and `tail` is what follows.
    `tag` is the tool of a call or of what came back from it, or which kind of note a note is.
    An entry of kind GAP stands for others: `left` of them, `cut` characters in all.
    """
    __slots__ = ()

    @property
    def text(self) -> str:
        if self.left:
            return LEFT.format(f"{self.left:,} {'entry' if self.left == 1 else 'entries'}", self.cut)
        if self.cut and not self.head and not self.tail:
            return DROPPED.format(self.cut)
        return self.head + (CUT.format(self.cut) if self.cut else "") + self.tail

    @property
    def line(self) -> str:
        return self.text if self.kind == GAP else f"**{self.kind}**: {self.text}"

    @property
    def whole(self) -> int:
        """How long it was before anything was cut."""
        return len(self.head) + len(self.tail) + self.cut

    def short(self, limit: int):
        """At most `limit` characters of it: the first two thirds and the last third, with the count of what is missing in between."""
        kept = len(self.head) + len(self.tail)
        if kept <= limit + SPARE:
            return self
        text = self.head + self.tail
        first = min(len(self.head), limit * 2 // 3) if self.cut else limit * 2 // 3
        last = min(len(self.tail), limit - first) if self.cut else limit - first
        return self._replace(head=text[:first], tail=text[len(text) - last:], cut=self.cut + kept - first - last)


def hollow(line: str) -> bool:
    """Does this line show nothing? The one answer for the slimmer, the archive and the search: a line that shows nothing reads as a blank one."""
    return HOLLOW.fullmatch(line) is not None


def repos() -> list:
    """The repos of this store: the lines of its roots.txt, and nothing else.

    Never the folder this script happens to be run from: run from a git worktree, that would make
    the worktree a repo of the store, and the archive would take the store's repos to have changed.
    A store with no roots.txt is the memory of the one repo it sits in: that repo's main checkout,
    whichever of its worktrees this copy of the script is in. The kit's own copy sits in none, and
    with no roots.txt no repo is listed for it to be run in (store.run_for(), the kit's one rule).
    """
    listed = MEM / "roots.txt"
    if listed.exists():
        return sorted({line.strip() for line in listed.read_text().split("\n") if line.strip()})
    repo = stores.run_for(ROOT, MEM)
    return [main_checkout(repo)] if repo else []


def main_checkout(folder: Path) -> str:
    """The main checkout of the repo this folder is in, or the folder itself when it is in none."""
    r = subprocess.run(["git", "-C", str(folder), "worktree", "list", "--porcelain"], capture_output=True, text=True)
    return next((l.split(" ", 1)[1] for l in r.stdout.splitlines() if l.startswith("worktree ")), str(folder))


def owners() -> dict:
    """Every folder in scope, with the repo it belongs to: each repo and all its git worktrees."""
    found = {}
    for repo in repos():
        r = subprocess.run(["git", "-C", repo, "worktree", "list", "--porcelain"],
                           capture_output=True, text=True)
        for path in [repo] + [l.split(" ", 1)[1] for l in r.stdout.splitlines() if l.startswith("worktree ")]:
            found.setdefault(path, repo)
    return found


def roots() -> list:
    """This repo, any others sharing the store, and all their git worktrees."""
    return sorted(owners())


def inside(cwd, rs) -> bool:
    if not isinstance(cwd, str) or not cwd:
        return False
    cwd = os.path.normpath(cwd)  # <repo>/../other is not in the repo
    return any(cwd == r or cwd.startswith(r.rstrip("/") + "/") for r in rs)


NOWHERE = Path(os.devnull)  # the store of a repo that has none: it is no store's, and so not this one's


def store_of(folder: Path):
    """The store a repo keeps its memory in, or None when the folder is not a repo with one.

    The store its `agent-memory.store` key names (store.py), else where its `memory` link leads: a repo
    is tied to its store by one or the other, and the key is the one that stays.
    """
    if not (folder / ".git").exists():
        return None
    link = folder / "memory"
    found = stores.keyed(folder) or (link.resolve() if link.exists() else None)
    if found is None:
        # A worktree with no tie of its own is its repository's, wherever it stands (store.repository): one of
        # another repo, put inside this store's, goes where that repo goes, and nowhere when that repo has no store.
        main = stores.repository(folder)
        if main != folder:
            return MEM if str(main) in repos() else store_of(main) or NOWHERE
    return found


@functools.lru_cache(maxsize=None)
def elsewhere(cwd: str, root: str) -> bool:
    """Is this folder, inside one of the store's repos, in a repo of its own that keeps its memory in another store?

    A repo nested in another is inside it by its path, but what happens there is that repo's and goes to
    its store. A worktree kept inside the repo leads to this store and stays in.
    """
    folder = Path(cwd)
    while str(folder) != root and folder != folder.parent:
        if store_of(folder) not in (None, MEM):
            return True
        folder = folder.parent
    return False


def ours(cwd, rs) -> bool:
    """Did this happen in one of the store's repos, and not in a repo nested there with a store of its own?"""
    if not inside(cwd, rs):
        return False
    cwd = os.path.normpath(cwd)
    return not elsewhere(cwd, max((r.rstrip("/") for r in rs if cwd == r or cwd.startswith(r.rstrip("/") + "/")), key=len))


def voice(e: dict, origin) -> str:
    """What a message in the person's place in the conversation really is. Only what every marker calls the person's is the person's."""
    if e.get("isCompactSummary"):
        return SUMMARY
    if origin is not None:
        kind = SAID_BY.get(origin.get("kind") if isinstance(origin, dict) else origin, NOTE)
        return NOTE if kind == USER and e.get("isMeta") else kind
    # No origin: an older Claude Code, or a kind of message it gives none.
    if e.get("turnOrigin") == "scheduled":
        return PROMPT  # a scheduled task's prompt, which Claude Code also marks as its own
    if e.get("isMeta"):
        return NOTE
    if e.get("isSidechain"):
        return AGENT  # in a subagent's transcript this place is the agent that started it
    if "sdk" in (e.get("turnOrigin"), e.get("promptSource")):
        return PROMPT
    return USER if e.get("turnOrigin") in (None, "human") and e.get("promptSource") != "system" else NOTE


def typed(text: str):
    """What was typed, when Claude Code wrapped it in tags: a slash command or a shell line. None when the tags hold something else."""
    text = text.strip()
    if not text.startswith("<"):
        return text
    command, arguments, shell = COMMAND.search(text), ARGUMENTS.search(text), SHELL.match(text)
    if command:
        return f"{command[1].strip()} {arguments[1].strip() if arguments else ''}".strip()
    return f"! {shell[1].strip()}" if shell else None


def note(text: str) -> Entry:
    """A system note, tagged when it is one of the notes every session repeats."""
    start = text.lstrip()
    return Entry(NOTE, text, tag=REMINDER if start.startswith("<system-reminder>") else IMAGE if start.startswith("[Image") else "")


def spoken(e: dict, origin, text: str) -> list:
    """One text from the person's place in the conversation, as entries."""
    kind = voice(e, origin)
    if kind == NOTE:
        return [note(text)]
    if kind not in (USER, PROMPT):
        return [Entry(kind, text)]
    if origin is not None or e.get("promptSource") in MARKED:
        # The transcript says who wrote this, so every character is theirs: a message may well start with `<`.
        words = typed(text)
        return [Entry(kind, text if words is None else words)]
    # No such mark, as in older transcripts: what Claude Code wrote into the message is a note, and so is a text that is all tags.
    rest = HARNESS.sub("", text)
    words = typed(rest)
    return [note(found) for found in HARNESS.findall(text)] + [note(rest) if words is None else Entry(kind, words)]


def shown(body) -> str:
    """What a tool gave back, as text. A picture or a document is named, not carried."""
    if isinstance(body, str):
        return body
    parts = [part.get("text") if part.get("type") == "text" else f"[{part.get('type')}]"
             for part in (body if isinstance(body, list) else []) if isinstance(part, dict)]
    return "\n".join(part for part in parts if isinstance(part, str))


def result(e: dict, block: dict, names: dict) -> list:
    """What came back from a tool call, as entries. Some of it is the person's: an answer they chose, the reason they turned a call down."""
    done, text, tool = e.get("toolUseResult"), shown(block.get("content")), names.get(block.get("tool_use_id"), "")
    if block.get("is_error"):
        notice, said, reason = text.partition(REASON)
        if said and e.get("toolDenialKind") == "user-rejected":
            return [Entry(ERROR, notice + said.strip(), tag=tool), Entry(USER, reason)]
        return [Entry(ERROR, text, tag=tool)]
    if tool == ASK and isinstance(done, dict) and "answers" in done:
        return [Entry(ANSWER, text, tag=tool)]  # the person picked or typed this; another tool's result with answers in it is just a result
    # The Agent tool's result, once the subagent has finished, is that subagent's report.
    finished = isinstance(done, dict) and done.get("agentId") and done.get("status") == "completed"
    return [Entry(REPORT if finished else RESULT, text, tag=tool)]


def said(e: dict, origin, content, names: dict) -> list:
    """A user-role message as entries. Claude Code files tool results, notes and other agents' messages there too."""
    if isinstance(content, str):
        return spoken(e, origin, content)
    found = []
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_result":
            found += result(e, block, names)
            continue
        new = spoken(e, origin, block.get("text") or "") if block.get("type") == "text" else [Entry(voice(e, origin), f"[{block.get('type')}]")]
        for entry in new:
            # A message in several blocks (words and a picture) is one message.
            if found and found[-1].kind == entry.kind and entry.kind in (USER, PROMPT, AGENT):
                found[-1] = Entry(entry.kind, f"{found[-1].head} {entry.head}".strip())
            else:
                found.append(entry)
    return found


def replied(content, names: dict) -> list:
    """An assistant message as entries: what it said and the tools it called. Its thinking is left out."""
    if isinstance(content, str):
        return [Entry(ASSISTANT, content)]
    found = []
    for block in content if isinstance(content, list) else []:
        if isinstance(block, dict) and block.get("type") == "text":
            found.append(Entry(ASSISTANT, block.get("text") or ""))
        elif isinstance(block, dict) and block.get("type") == "tool_use":
            names[block.get("id")] = str(block.get("name"))
            found.append(Entry(CALL, f"{block.get('name')} {json.dumps(block.get('input'), ensure_ascii=False)}", tag=str(block.get("name"))))
    return found


def attached(e: dict, names: dict) -> list:
    """What an attachment adds to the conversation. Most are Claude Code's bookkeeping and add nothing."""
    held = e.get("attachment")
    if not isinstance(held, dict):
        return []
    if held.get("type") == "queued_command":  # a message that arrived while the agent was working: it is nowhere else
        return said(dict(e, isMeta=held.get("isMeta")), held.get("origin"), held.get("prompt"), names)
    if held.get("type") == "hook_additional_context":
        context = held.get("content")
        context = "\n".join(c for c in context if isinstance(c, str)) if isinstance(context, list) else context
        return [Entry(NOTE, f"{held.get('hookEvent')} hook: {context}", tag=HOOK)] if isinstance(context, str) else []
    if held.get("type") == "structured_output":  # what a run started by a script answered, in the shape it was asked for
        return [Entry(ASSISTANT, json.dumps(held.get("data"), ensure_ascii=False))]
    if held.get("type") == "edited_text_file":  # someone else changed a file the agent had read: the person, or another agent
        return [Entry(NOTE, f"changed on disk since the agent read it: {held.get('filename')}")]
    return []


def system(e: dict) -> list:
    """A system entry: Claude Code's own line in the conversation, or its record of something going wrong."""
    error, broken = e.get("error"), e.get("hookErrors")
    if isinstance(e.get("content"), str):
        return [note(e["content"])]
    if isinstance(error, dict):
        return [Entry(FAULT, str(error.get("formatted") or error.get("message") or json.dumps(error, ensure_ascii=False)))]
    return [Entry(FAULT, f"stop hook: {json.dumps(broken, ensure_ascii=False)}")] if broken else []


def told(e: dict, clean=str, names=None) -> list:
    """One line of a transcript as the labelled entries it holds, each cleaned and then cut to the length its kind is given.

    `names` remembers which tool each call ran, from one line to the next, so what comes back can say whose it is.
    """
    names = {} if names is None else names
    kind, message = e.get("type"), e.get("message") or {}
    role = message.get("role") or kind
    if kind == "attachment":
        found = attached(e, names)
    elif kind == "system":
        found = system(e)
    elif role == "assistant":
        found = replied(message.get("content"), names)
    elif role == "user":
        found = said(e, e.get("origin"), message.get("content"), names)
    else:
        found = []
    out = []
    for entry in found:
        # One kind of line end and no empty line, so nothing in an entry can look like the start of another.
        text = BLANK.sub("\n", EDGES.sub("", LINE_ENDS.sub("\n", entry.head)))
        if text or entry.kind in BACK:  # a tool that printed nothing still ran: its empty result stays
            out.append(entry._replace(head=clean(text)).short(LIMITS[entry.kind]))
    return out


def starts_with_dream(path: Path) -> bool:
    """A session is a dream if the person's first message runs the command, not if it mentions it."""
    try:
        with open(path, **LINES) as handle:
            for raw in handle:  # as far as that first message: Claude Code writes any number of its own lines before it
                entry = parsed(raw)
                if entry is None:
                    continue
                message = entry.get("message") or {}
                if message.get("role") != "user" or entry.get("isMeta"):
                    continue
                content = message.get("content")
                if isinstance(content, list):
                    content = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
                text = str(content or "").strip()
                return text.startswith("/dream") or "<command-name>/dream</command-name>" in text
    except OSError:
        pass
    return False


def timestamp(entry):
    try:
        return datetime.datetime.fromisoformat(entry["timestamp"].replace("Z", "+00:00")).timestamp()
    except (KeyError, ValueError, AttributeError):
        return None


def parent_of(path: Path) -> str:
    """The session a subagent's transcript belongs to, or empty for a session's own file."""
    return path.parent.parent.name if path.parent.name == "subagents" else ""


def is_dream(path: Path, memo: dict) -> bool:
    if path.parent.name != "subagents":
        return memo.setdefault(path, starts_with_dream(path))
    parent = path.parent.parent.with_suffix(".jsonl")
    if memo.setdefault(parent, starts_with_dream(parent)):
        return True
    try:
        return json.loads(path.with_name(path.stem + ".meta.json").read_text()).get("agentType") == "transcript-analyst"
    except (OSError, ValueError):
        return False


def parsed(raw: str):
    """One line of a transcript as an entry, or None when it isn't one: not JSON, not an object, or its message isn't one."""
    try:
        entry = json.loads(raw)
    except (json.JSONDecodeError, RecursionError):
        return None
    return entry if isinstance(entry, dict) and isinstance(entry.get("message") or {}, dict) else None


def within(at: float, start: float, end=None) -> bool:
    """Is a moment inside a window? The one comparison for it, so the dream collects exactly what the daily check counted.

    To the second, and from the second of `start` on: a dream stamps the second it collected in, and what
    arrives later in that same second must still be read by the next one. Read twice at worst, never skipped.
    """
    return int(at) >= int(start) and (end is None or int(at) <= int(end))


def entries(path: Path, rs, cutoff=None):
    """The entries of one transcript that are in scope, each with its time. Read a line at a time: a transcript can be 500 MB."""
    with open(path, **LINES) as handle:
        for raw in handle:
            e = parsed(raw)
            # Entries without a cwd (summaries) say nothing about where they came from: left out.
            if e is None or not ours(e.get("cwd"), rs):
                continue
            at = timestamp(e)
            if at is not None and cutoff is not None and not within(at, cutoff):
                continue
            yield at, e


def folders(paths: list) -> list:
    """Every folder a session worked in, in the order it got there, whichever store they belong to."""
    found = []
    for path in paths:
        with open(path, **LINES) as handle:
            for raw in handle:
                e = parsed(raw)
                cwd = os.path.normpath(e["cwd"]) if e is not None and isinstance(e.get("cwd"), str) and e["cwd"] else None
                if cwd is not None and cwd not in found:
                    found.append(cwd)
    return found


def merged(paths: list, rs, cutoff=None):
    """One session whose transcript lies in several files: every entry once, in time order.

    Also the files that hold something no other file has. One that only repeats another can be
    deleted without the session losing anything.
    """
    found, times, last, holders, alone = [], [], float("-inf"), {}, set()
    for path in paths:
        for at, e in entries(path, rs, cutoff):
            uuid = e.get("uuid")
            if not isinstance(uuid, str):
                alone.add(str(path))  # nothing to match it by
            elif len(holders.setdefault(uuid, set()) | {str(path)}) > 1:
                holders[uuid].add(str(path))
                continue  # a transcript copied to another folder repeats what the first one has
            else:
                holders[uuid].add(str(path))
            last = at if at is not None else last  # an entry with no time stays behind the one before it
            found.append((at, e))
            times.append(last)
    alone |= {next(iter(paths_with)) for paths_with in holders.values() if len(paths_with) == 1}
    return [found[i] for i in sorted(range(len(found)), key=times.__getitem__)], sorted(alone)


def read(paths: list, rs, cutoff=None, clean=str) -> dict:
    """One session as labelled entries plus counts, from what happened inside `rs`.

    `paths` is its transcript, or several when the same session was written under more than one folder.
    `cutoff` (epoch seconds) drops older entries; the archive passes none and gets the whole session.
    `clean` is applied to each entry's whole text before any of it is cut, so nothing it removes can be left behind in half.
    `entries` is empty when nothing in it was in scope. `alone` names the files holding entries no other file has.
    """
    found, names = [], {}
    seen_ids, count = set(), dict.fromkeys(("tool_calls", "assistant_turns", "input_tokens", "cache_read_tokens", "output_tokens"), 0)
    used_memory = False
    kinds = collections.Counter()
    started = ended = None
    cwds = []
    stream, alone = (entries(paths[0], rs, cutoff), [str(paths[0])]) if len(paths) == 1 else merged(paths, rs, cutoff)
    for at, e in stream:
        if at is not None:
            started, ended = min(started or at, at), max(ended or at, at)
        if os.path.normpath(e["cwd"]) not in cwds:
            cwds.append(os.path.normpath(e["cwd"]))
        msg = e.get("message") or {}
        content = msg.get("content")
        if isinstance(content, list):
            for c in content:
                if isinstance(c, dict) and c.get("type") == "tool_use":
                    count["tool_calls"] += 1
                    used_memory = used_memory or bool(MEMORY_FILE.search(json.dumps(c.get("input"))))
                    kinds[kit_call(c.get("name", ""), c.get("input"), os.path.normpath(e["cwd"]), rs)] += 1
        # One API message is logged as several entries, so usage counts once per id.
        if (msg.get("role") or e.get("type")) == "assistant" and msg.get("id") not in seen_ids:
            seen_ids.add(msg.get("id"))
            usage = msg.get("usage") or {}
            count["assistant_turns"] += 1
            count["input_tokens"] += usage.get("input_tokens", 0) + usage.get("cache_creation_input_tokens", 0)
            count["cache_read_tokens"] += usage.get("cache_read_input_tokens", 0)
            count["output_tokens"] += usage.get("output_tokens", 0)
        found += told(e, clean, names)
    # What this session was asked: the person's first message, or for a subagent or a script's run the prompt it was given.
    asked = next((entry.text for kinds in ((USER,), (AGENT, PROMPT)) for entry in found if entry.kind in kinds), None)
    return dict(entries=found, humans=[entry.line for entry in found if entry.kind == USER],
                errors=sum(entry.kind == ERROR for entry in found), count=count, used_memory=used_memory,
                started=started, ended=ended, cwds=cwds, asked=asked, alone=alone,
                # Inside the repos it only worked on the memory kit: installed, synced, ran or changed it, and touched nothing of the project.
                kit_only=kinds["kit"] > 0 and kinds["project"] == 0)


def working(s: dict) -> bool:
    """Was work done in this session? The one answer, for the dream batch, its counts and the daily check.

    Yes when the agent ran a tool, or when the session is long enough to have been a conversation.
    A session with neither is someone checking that Claude answers: the Hub's "reply with the single
    word ready", a test of the closing handoff. What the person typed in one still reaches a dream
    (`aside`), because a decision can be one sentence. A session that only kept the kit (`kit_only`) did
    no work on the project either.
    """
    return not s.get("kit_only") and (s["count"]["tool_calls"] > 0 or size(s["entries"]) >= TALK)


# Work on the memory kit itself, not on a project: installing, syncing, scheduling or running it, the kit's
# folder and the kit's state, and the kit's own files in a repo. Reading the project's git (repo_look) is the
# project's. Using memory (memwrite, past, memcheck, the
# files under memory/, by that link or by the store's own folder under ~/agent-memory) is not this: an agent
# that reads or notes memory there is at work on the project, so those are no evidence either way.
KIT_SCRIPTS = ("amk|archive|build_index|check_stale|collect_transcripts|dream_apply|dream_due|dream_finish|dream_owner|dream_proposal|"
               "dream_try|guard_memory|interpreter|machine_check|memory_context|needs|own_notes|proposal_check|redact|slim|status|steering|store|verbs")
# `amk <verb>` is kit work when the verb's script is one of those: the same answer as for `python3 scripts/<its script>`. The verbs of memwrite,
# past, memcheck and repo_look are not, as their scripts are not.
KIT_VERBS = sorted((verb for verb, (script, *_) in verbs.VERBS.items() if script[:-3] in KIT_SCRIPTS.split("|")), key=len, reverse=True)
KIT_WORK = re.compile(r"agent-memory-kit|/\.local/state/agent-memory\b"
                      r"|\b(?:sync-kit|install-store|run-dream|schedule-dream|retire-auto-memory|link-worktrees|exclude-local)\.sh\b"
                      r"|\bamk\s+(?:" + "|".join(KIT_VERBS) + r")(?=[\s;&|)<>]|$)|(?:^|[\s'\"/])scripts/(?:" + KIT_SCRIPTS + r")\.py\b"
                      r"|\.claude/commands/dream\.md|\.claude/agents/(?:transcript|notes)-analyst\.md|\.claude/dream-apply\.md")
ASSIGNED = re.compile(r"(?:^|[\s;&|(])(\w+)=(['\"]?)([^\s'\";|&$`]*)\2(?=[\s;&|)]|$)")
VARIABLE = re.compile(r"\$\{?(\w+)\}?")
PATH_WORD = re.compile(r"(?:^|[\s'\"=])((?:~|\.{1,2})?/[^\s'\";|&<>()]*|[\w.-]+/[^\s'\";|&<>()]*)")


def kit_call(tool: str, given, cwd: str, rs) -> str:
    """What one tool call made inside the store's repos was: `kit` (work on the memory kit), `project`, or `none`
    (it named nothing in the repos: a subagent, a message, the web, files elsewhere, memory itself).

    Only what can be read for certain is `none` or `kit`: a command whose paths can't be resolved, or that
    names no path at all, is `project`, so a doubt keeps a session in.
    """
    given = given if isinstance(given, dict) else {}
    if tool.startswith("mcp__"):
        return "project"  # an app's own tool: what it reads or changes can't be told from here, so it is a doubt
    if tool == "Bash":
        command = str(given.get("command", ""))
        if KIT_WORK.search(command):
            return "kit"
        # A variable the command sets itself (`S=/tmp/x; ls $S`) is read as its value; any other can't be, and is a doubt.
        values = {"HOME": os.path.expanduser("~"), **{name: value for name, _, value in ASSIGNED.findall(command)}}
        command = VARIABLE.sub(lambda m: values.get(m[1], m[0]), command)
        if VARIABLE.search(command):
            return "project"
        words = [os.path.expanduser(w) for w in PATH_WORD.findall(command) if "://" not in w]
        if not words:
            return "project"
        lead = re.match(r"\s*cd\s+(\S+)\s*(?:&&|;)", command)
        base = os.path.normpath(os.path.join(cwd, os.path.expanduser(lead[1].strip("'\"")))) if lead else cwd
        paths = [os.path.normpath(os.path.join(base, w)) for w in words]
        return "project" if any(inside(p, rs) and not under_memory(p, rs) for p in paths) else "none"
    path = given.get("file_path") or given.get("notebook_path") or (given.get("path") if tool in ("Grep", "Glob") else None)
    if tool in ("Grep", "Glob") and not path:
        path = cwd
    if not isinstance(path, str) or not path:
        return "none"
    path = os.path.normpath(os.path.join(cwd, os.path.expanduser(path)))
    if KIT_WORK.search(path):
        return "kit"
    return "project" if inside(path, rs) and not under_memory(path, rs) else "none"


def under_memory(path: str, rs) -> bool:
    """Is this path the `memory` link of one of the repos, or under it? Using memory is neither kit work nor the project's code."""
    return any(path == r.rstrip("/") + "/memory" or path.startswith(r.rstrip("/") + "/memory/") for r in rs)


def text(found: list) -> str:
    """Entries as the text that is stored and read: each under its label, a blank line between them."""
    return "\n\n".join(entry.line for entry in found)


def size(found: list) -> int:
    """The length of text(found), without joining it."""
    return sum(len(entry.line) for entry in found) + 2 * max(len(found) - 1, 0)


def counted(kinds) -> str:
    """`3 tool results and 1 system note`: a count of entries for each kind, in words."""
    words = [f"{n:,} {NAMES[kind][n > 1]}" for kind, n in kinds.items() if n > 0]
    return " and ".join(filter(None, [", ".join(words[:-1]), words[-1]])) if words else ""


def most(least: int, top: int, fits) -> int:
    """The largest number from `least` to `top` that fits, or `least` when none does."""
    while least < top:
        middle = (least + top + 1) // 2
        least, top = (middle, top) if fits(middle) else (least, middle - 1)
    return least


def shorten(found: list, kinds: tuple, least: int, room: int) -> list:
    """Entries of these kinds cut to one length: the longest that makes the session fit, and never shorter than `least`."""
    cut = lambda limit: [entry.short(limit) if entry.kind in kinds else entry for entry in found]
    longest = max((len(entry.head) + len(entry.tail) for entry in found if entry.kind in kinds), default=0)
    return cut(most(least, max(least, longest), lambda limit: size(cut(limit)) <= room))


def units(found: list) -> list:
    """The session as runs that belong together: a call, or several made at once, with what came back from each."""
    out, owed = [], 0  # owed: calls in the last unit that nothing has come back from yet
    for entry in found:
        if entry.kind == CALL and out and out[-1][-1].kind == CALL:
            out[-1].append(entry)
            owed += 1
        elif entry.kind in BACK and owed:
            out[-1].append(entry)
            owed -= 1
        else:
            out.append([entry])
            owed = int(entry.kind == CALL)
    return out


def blocks(found: list, kinds: tuple) -> list:
    """Stretches of about BLOCK characters that can be left out together, as lists of positions.

    A stretch holds only entries of these kinds (and gaps already there), so it never reaches across
    what the person said, and it ends between two units, so a call goes or stays with its result.
    A unit that also holds something else stays whole: the call before an error, the question
    before the person's answer.
    """
    out, at = [], 0
    for unit in units(found):
        fits = all(entry.kind in kinds or entry.kind == GAP for entry in unit)
        if fits and out and out[-1][-1] == at - 1 and sum(len(found[i].line) + 2 for i in out[-1]) < BLOCK:
            out[-1] += range(at, at + len(unit))
        elif fits:
            out.append(list(range(at, at + len(unit))))
        at += len(unit)
    return [block for block in out if any(found[i].kind in kinds for i in block)]


def gapped(found: list, gone: set) -> list:
    """The session without the entries at these positions. Each run of them, and any gap beside it, is one line saying how much it was."""
    out, left, chars = [], 0, 0
    for i, entry in enumerate(found + [None]):
        if entry is not None and (i in gone or (entry.kind == GAP and entry.left)):
            left, chars = left + (entry.left or 1), chars + (entry.cut if entry.left else entry.whole)
            continue
        if left:
            out.append(Entry(GAP, cut=chars, left=left))
            left = chars = 0
        out += [entry] if entry is not None else []
    return out


def thin(found: list, kinds: tuple, least: int, room: int) -> list:
    """Whole stretches of these kinds left out, evenly through the session and as few as makes it fit."""
    spots = blocks(found, kinds)

    def keeping(n: int) -> list:
        # Evenly spread, the first and the last among them; the last alone when only one stays.
        keep = {round(i * (len(spots) - 1) / max(n - 1, 1)) for i in range(n)} if n > 1 else set(range(len(spots) - n, len(spots)))
        return gapped(found, {i for at, block in enumerate(spots) if at not in keep for i in block if found[i].kind in kinds})

    return keeping(most(least, len(spots), lambda n: size(keeping(n)) <= room))


WORK = (ASSISTANT, CALL, RESULT, REPORT, TASK, AGENT, SUMMARY, NOTE)  # what the agent and its tools did, as against what it was told and what went wrong
# What a session over its size loses, in this order, each step only as far as it takes to fit:
# (how, which entries, how far it may go). Bulk first, what the person said last.
LADDER = (
    (shorten, (RESULT, NOTE, SUMMARY), 100),  # what tools printed and what the harness added, down to a line or two
    (shorten, (CALL,), 150),  # the command or the file stays readable
    (shorten, (AGENT, REPORT, TASK), 1_000),  # what other agents wrote: how each begins and what it concludes
    (shorten, (ASSISTANT,), 300),  # how each step began and ended
    (shorten, (RESULT,), 0),  # only then do results go altogether, each leaving its size
    (thin, WORK, 2),  # then whole stretches of work, evenly; the first and the last stay: how it began and what it came to
    (shorten, (ERROR, FAULT), 200),
    (thin, (ERROR, FAULT, CALL, RESULT, REPORT), 0),  # errors, each with its call, go before a word of the person's does
    (shorten, (USER, ANSWER, PROMPT), 200),
    (thin, (USER, ANSWER, PROMPT, CALL), 0),
    (thin, WORK, 0),
)


def went(before: list, after: list, room: int) -> str:
    """One line saying what a session lost to fit: counted at the end, by what became of each entry."""
    whole, kept = {id(entry) for entry in before}, [entry for entry in after if entry.kind != GAP]
    changed = [entry for entry in kept if id(entry) not in whole]
    lost = [(collections.Counter(entry.kind for entry in changed if entry.head or entry.tail), "shortened"),
            (collections.Counter(entry.kind for entry in changed if not (entry.head or entry.tail)), "reduced to their size"),
            # A session can come with gaps of its own (a digest): they are not entries, and are not counted as lost here.
            (collections.Counter(entry.kind for entry in before if entry.kind != GAP) - collections.Counter(entry.kind for entry in kept), "left out")]
    return (f"trimmed to fit {room:,} characters, the least valuable first: "
            + "; ".join(f"{counted(kinds)} {how}" for kinds, how in lost if kinds)
            + f". {size(before) - size(after):,} of {size(before):,} characters are not here.")


def fit(found: list, room: int) -> tuple:
    """The session in at most `room` characters, and one line saying what that cost. A session that fits is returned as it is, with no line.

    The steps of LADDER are taken in order and each goes only as far as it has to, so the room is used.
    A room too small for even the line that says everything was left out gets nothing.
    """
    if size(found) <= room:
        return found, ""
    after = found
    for step, kinds, least in LADDER:
        after = step(after, kinds, least, room)
        if size(after) <= room:
            return after, went(found, after, room)
    return [], went(found, [], room)


def pieces(entry: Entry, room: int) -> list:
    """An entry longer than a whole part, as several that each fit. The later ones say they go on from the one before."""
    most_text = room - len(f"**{entry.kind}**: [continued] ")
    if len(entry.line) <= room or entry.kind == GAP or most_text <= 0:
        return [entry]
    whole = entry.text
    return [entry._replace(head=("[continued] " if at else "") + whole[at:at + most_text], tail="", cut=0) for at in range(0, len(whole), most_text)]


def parts(found: list, room: int) -> list:
    """The session as runs of entries that each fit in `room`.

    Split between units, so a call is never in one part and its result in the next. A unit too long
    for a part is split between its entries, and an entry too long for a part into pieces.
    """
    out, used = [[]], 0
    for unit in units([piece for entry in found for piece in pieces(entry, room)]):
        for run in ([unit] if size(unit) <= room else [[entry] for entry in unit]):
            length = size(run) + 2
            if out[-1] and used + length - 2 > room:
                out.append([])
                used = 0
            out[-1] += run
            used += length
    return out


def repeats(found: list) -> list:
    """An identical call made several times in a row, each time with a plain result, once: the call, its last result, and how often it ran."""
    out, at = [], 0
    while at < len(found):
        end = at
        while end + 1 < len(found) and found[end].kind == CALL and found[end].head == found[at].head and found[end + 1].kind == RESULT:
            end += 2
        if end - at > 2:
            out += [found[at], found[end - 1], Entry(GAP, f"[… the same call ran {(end - at) // 2:,} times in a row: this is its last result …]")]
            at = end
        else:
            out.append(found[at])
            at += 1
    return out


def digest(found: list) -> list:
    """A subagent's run as a dream needs it.

    The task it was given and what its parent or the person told it later, each error with the call
    before it and the step after, its last message whole, and the start and end of everything else
    it said: a lesson is often in a sentence with no failed call beside it (a tool that did nothing,
    a workaround found without an error).
    """
    last = next((i for i in range(len(found) - 1, -1, -1) if found[i].kind == ASSISTANT), None)
    keep = {last}
    for i, entry in enumerate(found):
        keep |= {i - 1, i, i + 1} if entry.kind in (ERROR, FAULT) else {i} if entry.kind in (USER, ANSWER, AGENT, ASSISTANT) else set()
    found = [entry.short(DIGEST_SAID) if entry.kind == ASSISTANT and i != last else entry for i, entry in enumerate(found)]
    return gapped(found, set(range(len(found))) - keep)


def dream(found: list, subagent: bool) -> list:
    """What a dream is given of one session. The archive keeps all of it; this is what an analyst needs to judge it.

    A call repeated in a row appears once. The result of a tool that reads or writes a file is its
    size: the file is in the repo. Hook context, reminders and picture notes go: every session has
    the same ones. What one agent tells another is kept to its start and its end. A subagent's run
    is a digest; its session says what it was for and what came of it.
    """
    found = repeats(found)
    found = [entry._replace(head="", tail="", cut=entry.whole) if entry.kind == RESULT and entry.tag in FILE_TOOLS else entry for entry in found]
    found = [entry for entry in found if not (entry.kind == NOTE and entry.tag in (HOOK, REMINDER, IMAGE))]
    found = [entry.short(DREAM_LIMITS[entry.kind]) if entry.kind in DREAM_LIMITS else entry for entry in found]
    return digest(found) if subagent else found


def files(stem: str, head: str, found: list, subagent=False) -> list:
    """One session for the dream batch, as [(file name, text)]: one file, or several that are read in order.

    `found` is the whole session; what a dream reads of it is `dream(found, subagent)`. A session longer
    than PART is split into parts, so nothing is cut to make it fit one file. Only a session over
    DREAM_CHARS is trimmed (fit), and every one of its files says so. `head` is the lines each file
    starts with, ending in a newline. Every file says where the whole session is.
    """
    session = stem.partition("--")[0]
    found, cost = fit(dream(found, subagent), DREAM_CHARS)
    groups = parts(found, PART)
    names = [f"{stem}.md"] + [f"{stem}.part-{i:02d}-of-{len(groups):02d}.md" for i in range(2, len(groups) + 1)]
    out = []
    for i, (name, group) in enumerate(zip(names, groups), 1):
        told = head
        if len(groups) > 1:
            told += f"part: {i} of {len(groups)}. One session in {len(groups)} files, read in this order: {', '.join(names)}\n"
        if subagent:
            told += "digest: a subagent's run, given as its task, what it said along the way, each error with the call before it and the step after, and its last message.\n"
        if cost:
            told += f"trimmed: {cost}\n"
        told += f"archive: the whole session is kept there. To search it: python3 scripts/past.py --session {session} \"<words>\"\n"
        out.append((name, f"{told}\n{text(group)}"))
    return out


def sessions_of(names: list) -> int:
    """How many sessions a batch holds, from its file names: a later part and a subagent's file belong to a session already counted."""
    return sum(not name.startswith("_") and name != ASIDE and ".part-" not in name and "--" not in name for name in names)


def aside(sessions: list) -> str:
    """What was said to Claude in sessions where no work was done, as one file for the batch: [(session id, entries)] in, text out.

    Such a session gets no file of its own, but a decision can be one sentence, so what the person
    typed in it, or an app passed on for them, is still read. The same text in many sessions (a
    probe) is there once, with how many. Empty when nothing was typed in any of them.
    """
    where = {}
    for session, found in sessions:
        for entry in found:
            if entry.kind in (USER, ANSWER, PROMPT):
                where.setdefault(entry.line, []).append(session)
    told = [f"{line}\n\n[… in {'session' if len(ids) == 1 else f'{len(ids):,} sessions, the first of them'} {ids[0]} …]" for line, ids in where.items()]
    return ("# what was typed in sessions where no tool ran and little was said\n\n" + "\n\n".join(told) + "\n") if told else ""


def groups(batch: list) -> list:
    """Which files each analyst gets: [[file name]] from [(session id, parent id or "", [(file name, characters)])].

    About GROUP characters each. The parts of one session stay together and in order. A subagent's
    files come after its parent's, in the same group while they fit; the rest start the next group.
    Only a single session larger than a group makes a group larger than that.
    """
    known = {session for session, parent, _ in batch if not parent}
    ordered = [row for top in batch if not top[1] for row in [top] + [sub for sub in batch if sub[1] == top[0]]]
    ordered += [row for row in batch if row[1] and row[1] not in known]  # a subagent whose session is not in this batch
    out, used = [], 0
    for _, _, written in ordered:
        chars = sum(n for _, n in written)
        if not out or used + chars > GROUP:
            out.append([])
            used = 0
        out[-1] += [name for name, _ in written]
        used += chars
    return out


def save(path: Path, text: str):
    """A batch file, readable by its owner only: it holds what was said in sessions."""
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8", errors="replace", newline="\n") as handle:
        handle.write(text)
    os.chmod(path, 0o600)


def store(out: Path, stem: str, head: str, found: list, subagent: bool) -> list:
    """Writes one session into the batch folder `out` and returns its files as [(name, characters)]."""
    written = files(stem, head, found, subagent)
    for name, told in written:
        save(out / name, told)
    return [(name, len(told)) for name, told in written]


def finish(out: Path, batch: list, idle: list):
    """Closes a batch: the file of what was typed in sessions where no work was done, and `_groups.md`, which says who reads what.

    `batch` is [(session id, parent id or "", its files from store())] and `idle` is [(session id, entries)].
    """
    told = aside(idle)
    if told:
        save(out / ASIDE, told)
        batch = batch + [(ASIDE, "", [(ASIDE, len(told))])]
    page = ["# Who reads what: one transcript-analyst for each group below, given that group's files in this order", "",
            f"A group is about {GROUP:,} characters. The parts of one session are together and in order, and a subagent's files come after its session's.", ""]
    sizes = {name: chars for _, _, written in batch for name, chars in written}
    for i, names in enumerate(groups(batch), 1):
        page += [f"## group {i}: {len(names)} {'file' if len(names) == 1 else 'files'}, {sum(sizes[name] for name in names):,} characters"]
        page += [f"- {name}" for name in names] + [""]
    save(out / "_groups.md", "\n".join(page))

"""What a session is doing right now, for the 3-second poll: the tool it runs, when its turn started, a finer status
for the orb, an error when Claude is gone or the API gave up, how full its context is, what it changed in its folder,
and the ports its processes listen on. Also what acts on that: Clear, Stop a port, Peek and its answer.

Everything here is cheap on the poll. Hook events are one small file. Transcripts are read from where the last read
stopped. git and lsof run in the background and the poll takes the last answer they gave, so a slow folder never
slows the page. What can't be known is left out (null), and never shown as working.
"""
import calendar, json, os, re, signal, subprocess, threading, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from hub_base import ENV, HOME, Refused, run

SEARCHING = {"Read", "Grep", "Glob", "LS", "WebFetch", "WebSearch", "ToolSearch", "NotebookRead"}
RETRY_FRESH = 120  # an API retry older than this, with nothing after it, is no longer "retrying"
TAIL = 2_000_000  # the first read of a long transcript starts this far from its end: only the latest turn matters here
LAST_TEXT = 6000


# ── hook events ──
def event_path(sid: str) -> Path:
    import hub_data as hub
    return hub.EVENTS / f"{sid}.json"


def read_event(sid: str) -> dict:
    try:
        value = json.loads(event_path(sid).read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def change_event(sid: str, change):
    """The hook's own read-change-write, under its lock, so a Clear never undoes an event that lands at the same time."""
    import fcntl
    path = event_path(sid)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.parent / f".{sid}.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            event = json.loads(path.read_text())
        except (OSError, ValueError):
            event = {}
        change(event)
        spare = path.parent / f".{sid}.clear"
        spare.write_text(json.dumps(event))
        os.replace(spare, path)


# ── transcripts, read from where the last read stopped ──
_tracks, _track_lock = {}, threading.Lock()


def _fresh(path: str) -> dict:
    return {"path": path, "offset": 0, "used": None, "model": "", "retry": None, "said": "", "said_at": "", "prompt_at": "", "last_at": ""}


def _absorb(track: dict, raw: bytes):
    try:
        entry = json.loads(raw)
    except ValueError:
        return
    if not isinstance(entry, dict) or entry.get("isSidechain"):
        return
    kind, at = entry.get("type"), str(entry.get("timestamp", ""))
    if kind == "system":
        if entry.get("subtype") == "api_error":
            error = entry.get("error") if isinstance(entry.get("error"), dict) else {}
            track["retry"] = {"at": at, "said": str(error.get("formatted") or error.get("message") or "")[:200],
                              "attempt": entry.get("retryAttempt"), "of": entry.get("maxRetries")}
        elif entry.get("subtype") == "compact_boundary":
            track["used"] = None  # the next reply says how much the summary takes
        return
    if kind not in ("user", "assistant"):
        return
    track["last_at"], track["retry"] = at, None
    message = entry.get("message") or {}
    if kind == "user":
        content = message.get("content")
        if isinstance(content, str) or (isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "text" for b in content)):
            if not entry.get("isMeta"):
                track["prompt_at"] = at
        return
    usage = message.get("usage") if isinstance(message.get("usage"), dict) else None
    if usage:
        parts = [usage.get(k) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")]
        track["used"] = sum(p for p in parts if isinstance(p, int))
    track["model"] = str(message.get("model") or track["model"])
    content = message.get("content")
    if isinstance(content, list):
        text = "\n".join(str(b.get("text", "")) for b in content if isinstance(b, dict) and b.get("type") == "text").strip()
        if text and not text.startswith("<"):
            track["said"], track["said_at"] = text[-LAST_TEXT:], at


def track_of(sid: str, transcript) -> dict:
    """What the transcript says about the latest turn. Reads only what was added since the last poll."""
    if transcript is None:
        return _fresh("")
    path = str(transcript)
    try:
        size = os.stat(path).st_size
    except OSError:
        return _fresh(path)
    with _track_lock:
        track = _tracks.get(sid)
        if track is None or track["path"] != path or size < track["offset"]:
            track = _tracks[sid] = _fresh(path)
            track["offset"] = max(0, size - TAIL)
            track["skip"] = track["offset"] > 0  # starting mid-file: the first line is a fragment
        if size > track["offset"]:
            with open(path, "rb") as handle:
                handle.seek(track["offset"])
                chunk = handle.read()
            end = chunk.rfind(b"\n")
            if end >= 0:
                lines = chunk[:end].split(b"\n")
                if track.pop("skip", False):
                    lines = lines[1:]
                for raw in lines:
                    _absorb(track, raw)
                track["offset"] += end + 1
        return dict(track)


def forget(sid: str):
    with _track_lock:
        _tracks.pop(sid, None)
    _changes.pop(sid, None)


def context_limit(model: str, used) -> int:
    """The window a model reads: 1M for the current families, 200K for Haiku 4.5 and older models."""
    match = re.search(r"(opus|sonnet|haiku|fable|mythos)-(\d+)(?:-(\d{1,2})(?!\d))?", model or "")
    big = True
    if match:
        family, major, minor = match[1], int(match[2]), int(match[3] or 0)
        big = family in ("fable", "mythos") or major >= 5 or (family in ("opus", "sonnet") and major == 4 and minor >= 6)
    elif re.search(r"claude-3", model or ""):
        big = False
    limit = 1_000_000 if big else 200_000
    return 1_000_000 if isinstance(used, int) and used > limit else limit


# ── processes: one ps per poll ──
def processes() -> dict:
    """Every process's children: one ps for the whole poll."""
    children = {}
    for row in run("ps", "-axo", "pid=,ppid=", timeout=10).stdout.splitlines():
        parts = row.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            children.setdefault(int(parts[1]), []).append(int(parts[0]))
    return children


def commands_of(pids) -> dict:
    """{pid: its command line} for a few processes (the panes), in one ps."""
    pids = [str(p) for p in pids if p]
    if not pids:
        return {}
    out = {}
    for row in run("ps", "-ww", "-o", "pid=,args=", "-p", ",".join(pids), timeout=10).stdout.splitlines():
        parts = row.strip().split(None, 1)
        if parts and parts[0].isdigit():
            out[int(parts[0])] = parts[1] if len(parts) > 1 else ""
    return out


def waiting_on_claude(command: str) -> bool:
    """A pane's shell still running `claude …; exec zsh -l` as given (sh -c or zsh -c), not yet the shell it ends in."""
    words = command.split(None, 2)
    return len(words) >= 2 and words[1] == "-c" and "claude" in command


def tree_of(root: int, children: dict) -> set:
    seen, stack = set(), [root]
    while stack:
        pid = stack.pop()
        if pid not in seen:
            seen.add(pid)
            stack += children.get(pid, [])
    return seen


# ── what runs in the background: git and lsof answer the next poll, never this one ──
_pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="hub-status")
_changes, _pending, _bg_lock = {}, set(), threading.Lock()
_listening = {"at": 0.0, "ports": {}, "busy": False}
CHANGES_TTL, CHANGES_TTL_IDLE, PORTS_TTL = 15, 300, 4


def _refresh_changes(sid: str, folder: str, stamp):
    import hub_diff
    try:
        value = hub_diff.numstat(folder)
    except Exception as error:  # a folder git can't read shows no changes, and says so in the log once
        print(f"error: changes of {sid}: {type(error).__name__}: {error}", flush=True)
        value = None
    with _bg_lock:
        _changes[sid] = (stamp, value, time.time())
        _pending.discard(sid)


def changes_of(sid: str, folder: str, stamp, alive: bool):
    """{added, removed, files} for the session's folder, as of the last background count. None until the first."""
    with _bg_lock:
        known = _changes.get(sid)
        old = known is None or known[0] != stamp or time.time() - known[2] > (CHANGES_TTL if alive else CHANGES_TTL_IDLE)
        if old and sid not in _pending and folder:
            _pending.add(sid)
            _pool.submit(_refresh_changes, sid, folder, stamp)
    return known[1] if known else None


def listening_now() -> dict:
    """{pid: {ports}} for every TCP socket listening on this Mac that lsof lets this user see."""
    found, pid = {}, None
    for line in run("lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-Fpn", timeout=10).stdout.splitlines():
        if line.startswith("p") and line[1:].isdigit():
            pid = int(line[1:])
        elif line.startswith("n") and pid:
            port = line.rsplit(":", 1)[-1]
            if port.isdigit():
                found.setdefault(pid, set()).add(int(port))
    return found


def _refresh_listening():
    try:
        ports = listening_now()
    except (OSError, subprocess.SubprocessError):
        ports = None
    with _bg_lock:
        if ports is not None:
            _listening["ports"] = ports
        _listening["at"], _listening["busy"] = time.time(), False


def listening() -> dict:
    with _bg_lock:
        if time.time() - _listening["at"] > PORTS_TTL and not _listening["busy"]:
            _listening["busy"] = True
            _pool.submit(_refresh_listening)
        return _listening["ports"]


def owned(pane: int, claude: int, children: dict) -> set:
    """What a session may stop: its pane's processes, but never the pane's shell, Claude itself, or this hub."""
    return tree_of(pane, children) - {pane, claude, os.getpid()} if pane else set()


def ports_of(pane: int, claude: int, children: dict) -> list:
    if not pane:
        return []
    table = listening()
    return sorted({port for pid in owned(pane, claude, children) for port in table.get(pid, ())})


# ── the poll: one session's live fields ──
def iso_epoch(text: str):
    """A transcript's timestamp (UTC, ISO 8601) as epoch seconds."""
    try:
        return calendar.timegm(time.strptime(text[:19], "%Y-%m-%dT%H:%M:%S")) if text else None
    except ValueError:
        return None


def orb_of(state: str, event: dict, track: dict) -> str:
    if state == "error":
        return "retrying"
    if state == "needs":
        return "waiting"
    if state != "working":
        return "idle"
    retry = track.get("retry")
    if retry and (iso_epoch(retry["at"]) or 0) > time.time() - RETRY_FRESH:
        return "retrying"
    if event.get("compacting"):
        return "compacting"
    tool = event.get("tool") if isinstance(event.get("tool"), dict) else None
    if not event:
        return "working"
    if tool and not tool.get("done") and (tool.get("since") or 0) >= (event.get("turn") or 0):
        return "searching" if tool.get("tool") in SEARCHING else "using"
    return "reasoning"


def activity_of(event: dict):
    tool = event.get("tool") if isinstance(event.get("tool"), dict) else None
    if not tool or not tool.get("tool") or (tool.get("since") or 0) < (event.get("turn") or 0):
        return None  # a tool from an earlier turn isn't this turn's
    return {"tool": str(tool["tool"]), "target": str(tool.get("target") or ""), "since": tool.get("since")}


GONE = "Claude stopped while it was working, and its process is gone."


def error_of(event: dict, claude_alive: bool, pane_alive: bool) -> str:
    """One plain sentence when the session failed, or nothing. A Clear after the failure hides it."""
    if (event.get("cleared") or 0) >= (event.get("at") or 0):
        return ""
    if event.get("state") == "error":
        failure = event.get("failure") or {}
        said = str(failure.get("details") or failure.get("error") or event.get("message") or "").strip()
        return f"The API stopped this turn: {said.rstrip('.')}." if said else "The API stopped this turn with an error."
    if pane_alive and not claude_alive and event.get("state") in ("working", "needs"):
        return GONE
    return ""


def enrich(row: dict, meta: dict, transcript, pane_pid: int, claude_pid: int, children: dict):
    """Adds the data contract's fields to a session row whose state is already decided."""
    sid = meta["id"]
    event = read_event(sid) if meta.get("kind") == "claude" else {}
    track = track_of(sid, transcript) if meta.get("kind") == "claude" else _fresh("")
    state = row.get("state")
    row["activity"] = activity_of(event) if meta.get("kind") == "claude" else None
    turn = event.get("turn") or iso_epoch(track.get("prompt_at"))
    row["turn"] = {"since": turn} if state == "working" and turn else None
    row["orb"] = orb_of(state, event, track)
    used = track.get("used")
    row["context"] = {"used": used, "limit": context_limit(track.get("model") or "", used)} if isinstance(used, int) and used > 0 else None
    folder = meta.get("folder") or meta.get("repo") or ""
    try:
        stamp = event_path(sid).stat().st_mtime_ns
    except OSError:
        stamp = 0
    row["changes"] = changes_of(sid, folder, stamp, bool(row.get("alive")))
    row["ports"] = ports_of(pane_pid, claude_pid, children) if row.get("alive") else []
    if state == "working" and row["orb"] == "retrying":
        retry = track.get("retry") or {}
        row["retry"] = {"said": retry.get("said", ""), "attempt": retry.get("attempt"), "of": retry.get("of")}


# ── acting on it ──
def clear(sid: str):
    """Clear a failed state: what failed stays in the transcript, the session reads as idle again."""
    import hub_data as hub
    hub.session_meta(sid)
    now = time.time()
    change_event(sid, lambda e: (e.update(cleared=now, at=now, failure=None, compacting=None), e.update(state="finished") if e.get("state") in ("error", "working", "needs") else None))
    return {"ok": True}


def stop_port(sid: str, body: dict):
    """Stop what listens on one port, only when it is a process this session started. Asked first, in the page."""
    import hub_data as hub
    meta = hub.session_meta(sid)
    port = (body or {}).get("port")
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise Refused("Say which port to stop.")
    panes = dict(r.split() for r in hub.tmux("list-panes", "-t", f"hub-{sid}", "-F", "#{session_name} #{pane_pid}").stdout.splitlines() if len(r.split()) == 2)
    pane = int(panes.get(f"hub-{sid}", 0) or 0)
    if not pane:
        raise Refused("That session has ended.")
    children = processes()
    info = hub.claude_info(pane, children) if meta.get("kind") == "claude" else None
    mine = owned(pane, int((info or {}).get("pid") or 0), children)
    table = listening_now()
    targets = sorted(pid for pid in mine if port in table.get(pid, ()))
    if not targets:
        raise Refused(f"Nothing this session started listens on :{port}.")
    for pid in targets:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    end = time.time() + 3
    while time.time() < end and any(_alive(pid) for pid in targets):
        time.sleep(0.1)
    for pid in targets:
        if _alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    with _bg_lock:
        _listening["at"] = 0.0  # the next poll asks again
    return {"stopped": targets}


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:  # a child that exited and wasn't reaped is gone too
        os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        pass
    return run("ps", "-o", "stat=", "-p", str(pid)).stdout.strip()[:1] not in ("", "Z")


# ── peek: the last reply or question, and an answer without opening the session ──
DIALOG_OPTION = re.compile(r"^\s*(?:[❯>›]\s*)?(\d)[.)]\s+(.+?)\s*$")
REPLY_OPTION = re.compile(r"^\s*(\d{1,2})[.)]\s+(.+?)\s*$")


def dialog_in(pane: str):
    """A numbered choice Claude Code is showing (a permission prompt, a question): its question and options.

    Only a choice that is live: its cursor (❯) is on one of the options, and no input line comes after it. A numbered
    list higher up the screen, in a reply or a prompt, is text, not a choice."""
    lines = [line.rstrip() for line in pane.splitlines()][-40:]
    cursor = max((i for i, line in enumerate(lines) if line.lstrip().startswith("❯")), default=None)
    if cursor is None or not DIALOG_OPTION.match(lines[cursor]):
        return None
    start = cursor
    while start > 0 and DIALOG_OPTION.match(lines[start - 1]):
        start -= 1
    options, end = [], start
    while end < len(lines) and (DIALOG_OPTION.match(lines[end]) or (options and lines[end].startswith("     ") and not lines[end].strip().startswith("❯"))):
        match = DIALOG_OPTION.match(lines[end])
        if match:
            if int(match[1]) != len(options) + 1:
                break
            options.append({"n": int(match[1]), "label": re.sub(r"\s+", " ", match[2]).strip()})
        elif options:  # a label the terminal wrapped onto the next line
            options[-1]["label"] += " " + lines[end].strip()
        end += 1
    for option in options:
        option["label"] = option["label"][:160]
    if len(options) < 2 or not options[0]["n"] == 1:
        return None
    question = next((line.strip(" │╭╮╰╯─") for line in reversed(lines[:start]) if line.strip(" │╭╮╰╯─")), "")
    return {"question": question[:240], "options": options[:9]}


def options_in(text: str) -> list:
    """Numbered options at the end of a reply that asks a question: 1., 2., … in order."""
    rows = [line for line in (text or "").splitlines() if line.strip()]
    found = []
    for line in reversed(rows[-12:]):
        match = REPLY_OPTION.match(line)
        if not match:
            if found:
                break
            continue
        found.insert(0, {"n": int(match[1]), "label": re.sub(r"[*_`]+", "", match[2]).strip()[:160]})
    return found if len(found) >= 2 and [o["n"] for o in found] == list(range(1, len(found) + 1)) else []


def peek(sid: str, rows: list) -> dict:
    """`rows` are the sessions as the last poll saw them, so a peek costs no more than the poll did."""
    import hub_data as hub
    meta = hub.session_meta(sid)
    s = next((row for row in rows if row["id"] == sid), None) or {}
    target = f"hub-{sid}"
    alive = target in hub.tmux_sessions()
    track = track_of(sid, hub.transcript_of(sid)) if meta.get("kind") == "claude" else _fresh("")
    out = {"id": sid, "title": meta.get("title", ""), "state": s.get("state", "ended"), "orb": s.get("orb", "idle"), "activity": s.get("activity"), "turn": s.get("turn"),
           "error": s.get("error", ""), "asking": s.get("asking", ""), "text": track.get("said", ""), "at": track.get("said_at", ""), "dialog": None, "options": [],
           "live": alive and meta.get("kind") == "claude" and s.get("state") not in ("closing", "closed", "ended")}
    pane = hub.pane_of(target) if alive else ""
    if out["state"] == "needs" and pane:
        out["dialog"] = dialog_in(pane)
    if out["dialog"]:
        out["options"] = out["dialog"]["options"]
    elif out["state"] in ("needs", "finished"):
        out["options"] = options_in(out["text"])
    out["typed"] = bool(hub.typed_now(target)) if pane and not out["dialog"] else False
    return out


def answer(sid: str, body: dict):
    """A number answers the choice on screen; text goes in as one paste and Enter, the way Close sends its prompt."""
    import hub_data as hub
    meta, target = hub.live_claude(sid)
    body = body or {}
    pane = hub.pane_of(target)
    choice, text = body.get("choice"), body.get("text")
    if choice is not None:
        if not isinstance(choice, int) or isinstance(choice, bool) or not 1 <= choice <= 9:
            raise Refused("Choose one of the numbered options.")
        dialog = dialog_in(pane)
        if dialog:
            if choice > len(dialog["options"]):
                raise Refused("That isn't one of the options on screen.")
            hub.tmux("send-keys", "-t", target, str(choice))
            return {"ok": True, "sent": dialog["options"][choice - 1]["label"]}
        text = str(choice)
    if not isinstance(text, str) or not text.strip():
        raise Refused("Write a reply first.")
    if len(text) > 20000:
        raise Refused("That reply is too long to send from here.")
    if dialog_in(pane):
        raise Refused("Claude is showing a choice. Answer it with its number.")
    if hub.typed_now(target):
        raise Refused("Something is already typed in this session. Send or clear it there first.")
    hub.paste_prompt(target, text.strip(), f"hub-reply-{sid}")
    return {"ok": True}

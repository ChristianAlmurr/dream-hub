#!/usr/bin/env python3
"""Claude Code hook for sessions started by Dream Hub: records one plain state per session, where its transcript is,
and what it is doing: the tool running now (or last this turn), when the turn started, a compaction, an API failure.

It runs on every tool call, so it reads and writes one small file and never fails the session: anything it can't do
it leaves undone."""
import fcntl, json, os, sys, time

STATES = {"UserPromptSubmit": "working", "PreToolUse": "working", "PostToolUse": "working", "PostToolUseFailure": "working",
          "PreCompact": "working", "PostCompact": "working", "Stop": "finished", "StopFailure": "error",
          "SessionEnd": "ended", "SessionStart": "finished", "PermissionRequest": "needs"}


def target_of(tool: str, given: dict, cwd: str) -> str:
    """What a tool works on, in a few words: a path from the session's folder, a command, a pattern, a page."""
    if not isinstance(given, dict):
        return ""
    path = given.get("file_path") or given.get("notebook_path") or given.get("path") or ""
    if tool == "Bash":
        return " ".join(str(given.get("command", "")).split())[:160]
    if tool in ("Grep", "Glob"):
        return str(given.get("pattern", ""))[:120]
    if tool == "WebFetch":
        return str(given.get("url", ""))[:160]
    if tool == "WebSearch":
        return str(given.get("query", ""))[:120]
    if tool in ("Agent", "Task"):
        return str(given.get("description", ""))[:120]
    if tool == "Skill":
        return str(given.get("skill", ""))[:80]
    if path:
        path = str(path)
        if cwd and path.startswith(cwd.rstrip("/") + "/"):
            return path[len(cwd.rstrip("/")) + 1:][:200]
        home = os.path.expanduser("~")
        return ("~" + path[len(home):] if path.startswith(home + "/") else path)[:200]
    return ""


def main():
    sid = os.environ.get("HUB_SESSION", "")
    if not sid.isalnum():
        return
    try:
        event = json.load(sys.stdin)
    except ValueError:
        return
    if not isinstance(event, dict):
        return
    name = event.get("hook_event_name", "")
    state = STATES.get(name)
    if name == "Notification":
        kind = str(event.get("notification_type", ""))
        message = str(event.get("message", ""))
        if kind in ("permission_prompt", "elicitation_dialog") or "permission" in message.lower():
            state = "needs"
    if not state:
        return
    # The hub that started this session says where its state is. A session from before hubs did has no HUB_STATE and
    # belongs to the installed hub. An empty or relative value is a mistake, and guessing a folder would cross two hubs.
    home = os.environ.get("HUB_STATE")
    if home is None:
        home = os.path.expanduser("~/.local/state/agent-hub")
    elif not os.path.isabs(home):
        sys.exit(f"HUB_STATE must be an absolute path, and {home!r} isn't.")
    folder = os.path.join(home, "events")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"{sid}.json")
    now = time.time()
    # Tool calls can run side by side, so the read and the write are one step under a lock.
    with open(os.path.join(folder, f".{sid}.lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            with open(path) as f:
                was = json.load(f)
            was = was if isinstance(was, dict) else {}
        except (OSError, ValueError):
            was = {}
        tool, given, cwd = str(event.get("tool_name", "")), event.get("tool_input") or {}, str(event.get("cwd", ""))
        record = {"state": state, "at": now, "message": str(event.get("message", ""))[:200],
                  "transcript": str(event.get("transcript_path", "")) or was.get("transcript", ""),
                  "turn": was.get("turn"), "tool": was.get("tool"), "compacting": None, "failure": None, "cleared": was.get("cleared")}
        if name == "UserPromptSubmit":
            record.update(turn=now, tool=None)
        elif name == "PreToolUse":
            record["tool"] = {"tool": tool, "target": target_of(tool, given, cwd), "since": now, "done": None, "failed": False}
            record["turn"] = record["turn"] or now
        elif name in ("PostToolUse", "PostToolUseFailure"):
            last = was.get("tool") if isinstance(was.get("tool"), dict) else None
            if not last or last.get("tool") != tool:  # a call that started before the hook knew of it
                last = {"tool": tool, "target": target_of(tool, given, cwd), "since": now}
            record["tool"] = {**last, "done": now, "failed": name == "PostToolUseFailure"}
        elif name == "PreCompact":
            record["compacting"] = now
        elif name == "StopFailure":
            error = event.get("error")
            words = error if isinstance(error, str) else json.dumps(error) if error else ""
            details = str(event.get("error_details") or "")
            record["failure"] = {"at": now, "error": words[:120], "details": details[:300]}
            record["message"] = (details or words)[:200]
        elif name == "PermissionRequest":
            target = target_of(tool, given, cwd)
            record["message"] = f"Allow {tool}{': ' + target if target else ''}?"[:200]
            if isinstance(was.get("tool"), dict) and was["tool"].get("tool") == tool:
                record["tool"] = was["tool"]
        elif name == "Notification" and was.get("state") == "needs" and str(was.get("message", "")).startswith("Allow "):
            record["message"] = was["message"]  # the permission request already said what it asks, in fewer words
        tmp = os.path.join(folder, f".{sid}.{os.getpid()}")
        with open(tmp, "w") as f:
            json.dump(record, f)
        os.replace(tmp, path)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:  # a hook that fails would show in the session; what it records is never worth that
        pass

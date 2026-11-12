"""What Claude is doing and which agents exist, read from its transcripts and agent files.

Transcripts grow by appending, so each one is read from where the last read stopped.
"""
import json, re, threading
from pathlib import Path

HOME = Path.home()
KINDS = {"Read": "r", "Grep": "r", "Glob": "r", "WebFetch": "r", "WebSearch": "r", "ToolSearch": "r",
         "Edit": "e", "Write": "e", "NotebookEdit": "e", "Bash": "x", "Agent": "a", "Task": "a"}
TRACE_LIMIT = 64
AGENT_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_digests, _lock = {}, threading.Lock()


def _name(path) -> str:
    return Path(str(path)).name or str(path)


def phrase(tool: str, given: dict) -> str:
    """One plain line about a tool call."""
    if tool == "Read":
        return f"Reading {_name(given.get('file_path', ''))}"
    if tool in ("Edit", "Write", "NotebookEdit"):
        return f"Editing {_name(given.get('file_path') or given.get('notebook_path', ''))}"
    if tool == "Bash":
        return str(given.get("description") or "Running a command")[:120]
    if tool in ("Grep", "Glob"):
        return f"Searching for {str(given.get('pattern', ''))[:60]}"
    if tool in ("Agent", "Task"):
        return f"Delegating: {str(given.get('description', 'a subagent'))[:100]}"
    if tool == "WebSearch":
        return "Searching the web"
    if tool == "WebFetch":
        return "Reading a web page"
    if tool == "Skill":
        return f"Using the {given.get('skill', '')} skill"
    if tool.startswith("mcp__"):
        return f"Using {tool.split('__')[1].replace('claude_ai_', '').replace('_', ' ')}"
    return tool


def model_id(raw) -> str:
    match = re.search(r"opus|sonnet|haiku|fable", str(raw or ""))
    return match[0] if match else ""


def _fresh(path: Path) -> dict:
    return {"path": str(path), "offset": 0, "trace": [], "tools": {}, "now": "", "said": "", "last": "", "model": "", "launched": {}}


def _first_line(text: str) -> str:
    line = next((row.strip() for row in text.splitlines() if row.strip()), "")
    return re.sub(r"[*`#]+", "", line)[:180]


def _absorb(digest: dict, raw: bytes, is_subagent: bool):
    try:
        entry = json.loads(raw)
    except ValueError:
        return
    if entry.get("isMeta") or (entry.get("isSidechain") and not is_subagent) or entry.get("type") not in ("user", "assistant"):
        return
    message = entry.get("message") or {}
    content = message.get("content")
    if not isinstance(content, list):
        return
    if entry["type"] == "assistant":
        digest["model"] = model_id(message.get("model")) or digest["model"]
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text" and entry["type"] == "assistant":
            text = str(block.get("text", ""))
            line = _first_line(text)
            if line and not line.startswith("<"):
                digest["said"], digest["now"], digest["last"] = line, "", text[-4000:]
        elif kind == "tool_use":
            tool, given = str(block.get("name", "")), block.get("input") or {}
            digest["last"] = ""
            digest["tools"][block.get("id")] = len(digest["trace"])
            digest["trace"].append(KINDS.get(tool, "m"))
            digest["now"] = phrase(tool, given)
            if tool in ("Agent", "Task"):
                digest["launched"][block.get("id")] = {"tool": block.get("id"), "type": str(given.get("subagent_type") or "agent"), "what": str(given.get("description", ""))[:160],
                                                       "prompt": str(given.get("prompt", ""))[:4000], "result": None, "failed": False, "background": False}
        elif kind == "tool_result":
            tool_id = block.get("tool_use_id")
            if block.get("is_error") and tool_id in digest["tools"]:
                digest["trace"][digest["tools"][tool_id]] = "!"
            launched = digest["launched"].get(tool_id)
            if launched is not None:
                outcome = entry.get("toolUseResult")
                status = outcome.get("status") if isinstance(outcome, dict) else ""
                launched["background"] = status in ("async_launched", "launched", "running")
                launched["failed"] = bool(block.get("is_error"))
                if not launched["background"]:
                    body = block.get("content")
                    text = body if isinstance(body, str) else "\n".join(str(x.get("text", "")) for x in body or [] if isinstance(x, dict))
                    text = re.sub(r"\s*<tools-used-before-summary>.*?</tools-used-before-summary>\s*", "", text, flags=re.S)
                    launched["result"] = re.sub(r"^\s*\[[Hh]arness[^\n]*\]\s*$", "", text, flags=re.M).strip()[:6000]


def digest_of(key: str, path, is_subagent: bool = False) -> dict:
    if path is None or not Path(path).exists():
        return _fresh(Path("/nonexistent"))
    path = Path(path)
    with _lock:
        digest = _digests.get(key)
        size = path.stat().st_size
        if digest is None or digest["path"] != str(path) or size < digest["offset"]:
            digest = _digests[key] = _fresh(path)
        if size > digest["offset"]:
            with open(path, "rb") as handle:
                handle.seek(digest["offset"])
                chunk = handle.read()
            end = chunk.rfind(b"\n")  # a line still being written waits for the next read
            if end >= 0:
                for raw in chunk[:end].split(b"\n"):
                    _absorb(digest, raw, is_subagent)
                digest["offset"] += end + 1
        return digest


def forget(sid: str):
    with _lock:
        for key in [k for k in _digests if k == sid or k.startswith(sid + ":")]:
            del _digests[key]


def _metas(transcript: Path) -> dict:
    found = {}
    for path in (transcript.with_suffix("") / "subagents").glob("agent-*.meta.json"):
        try:
            meta = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        found[meta.get("toolUseId")] = {**meta, "agent": path.name[len("agent-"):-len(".meta.json")]}
    return found


def _status(launched: dict, meta: dict) -> str:
    if launched["failed"]:
        return "failed"
    if meta.get("finished") is True or launched["result"] is not None:
        return "done"
    return "running"


def counts(sid: str, transcript, alive: bool) -> dict:
    """How many subagents this session started and how many are still going."""
    if transcript is None:
        return {"running": 0, "total": 0}
    launched = digest_of(sid, transcript)["launched"]
    metas = _metas(transcript) if launched else {}
    doing = [a["what"] for tool, a in launched.items() if _status(a, metas.get(tool, {})) == "running"] if alive else []
    return {"running": len(doing), "total": len(launched), "doing": doing[:4]}


def subagents(sid: str, transcript, alive: bool) -> list:
    """Each subagent, newest first, with what it was asked, what it reported and what it did."""
    if transcript is None:
        return []
    metas, out = _metas(transcript), []
    for tool, launched in digest_of(sid, transcript)["launched"].items():
        meta = metas.get(tool, {})
        status = _status(launched, meta)
        row = {"id": tool, "type": meta.get("agentType") or launched["type"], "what": launched["what"], "prompt": launched["prompt"], "result": launched["result"],
               "status": status if alive or status != "running" else "stopped", "now": "", "said": "", "trace": [], "tools": 0, "model": model_id(meta.get("pickedEffortModel") or meta.get("model"))}
        if meta.get("agent"):
            detail = digest_of(f"{sid}:{meta['agent']}", transcript.with_suffix("") / "subagents" / f"agent-{meta['agent']}.jsonl", True)
            row.update(now=detail["now"], said=detail["said"], trace=detail["trace"][-TRACE_LIMIT:], tools=len(detail["trace"]))
            row["model"] = detail["model"] or row["model"]
        out.append(row)
    out.reverse()
    return out


def _parse_definition(path: Path, source: str):
    try:
        text = path.read_text(errors="ignore")
    except OSError:
        return None
    fields, body = {}, text
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            body = parts[2].strip()
            for line in parts[1].splitlines():
                if ":" in line and not line.startswith((" ", "\t")):
                    key, value = line.split(":", 1)
                    fields[key.strip()] = value.strip().strip("\"'")
    name = fields.get("name") or path.stem
    if not AGENT_NAME.match(name):
        return None
    return {"name": name, "description": fields.get("description", "")[:400], "model": fields.get("model", ""), "tools": fields.get("tools", ""), "source": source, "body": body[:8000]}


def definitions(repos: list) -> list:
    """The agents you can start or delegate to here: the project's, then your personal ones."""
    found = {}
    for folder, source in [*((Path(r) / ".claude" / "agents", "project") for r in repos), (HOME / ".claude" / "agents", "personal")]:
        for path in sorted(folder.glob("*.md")) if folder.exists() else []:
            agent = _parse_definition(path, source)
            if agent and agent["name"] not in found:
                found[agent["name"]] = agent
    return list(found.values())

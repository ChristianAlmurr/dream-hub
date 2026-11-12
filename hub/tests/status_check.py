#!/usr/bin/env python3
"""Live status and review end to end: every field the poll adds to a session (activity, turn, orb, error, context,
changes, ports), the diff and its path guards, Stop on a port and how far it reaches, Clear, peek and its answers,
and review comments sent as one paste. Real Claude sessions (Haiku, low effort) in a throwaway project,
hub-test-throwaway-<n>, made here and removed at the end.

Runs against a second instance (the six HUB_* variables, see tests/instance.py).
Run: /usr/bin/python3 tests/status_check.py
"""
import http.client, json, os, re, signal, socket, subprocess, sys, time, urllib.parse
from pathlib import Path

from instance import HOME, HOST, NAME, OWNER, PORT, STATE, TMUX, WRITE, answer_first_run, launchd_jobs, quiet_alert, remove_project, screen, sweep, tell

base_app = Path(__file__).resolve().parent.parent
ORBS = {"working", "reasoning", "searching", "using", "compacting", "waiting", "idle", "retrying"}
STATES = {"working", "needs", "finished", "shell", "ended", "closing", "closed", "error"}
fails = 0


def check(label, ok, extra=""):
    global fails
    fails += 0 if ok else 1
    print(("PASS  " if ok else "FAIL  ") + label + ("" if ok else f"  [{str(extra)[:400]}]"), flush=True)


def call(method, path, body=None):
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=60)
    c.request(method, path, json.dumps(body) if body is not None else None, WRITE if body is not None else OWNER)
    r = c.getresponse()
    raw = r.read()
    try:
        return r.status, json.loads(raw or b"{}")
    except ValueError:
        return r.status, raw


def rows():
    return call("GET", "/api/state")[1]["sessions"]


def row(sid):
    return next((s for s in rows() if s["id"] == sid), None)


def wait(sid, ok, limit, label=""):
    end, seen = time.time() + limit, None
    while time.time() < end:
        seen = row(sid)
        if seen and ok(seen):
            return seen
        time.sleep(1)
    if label:
        check(label, False, {k: (seen or {}).get(k) for k in ("state", "orb", "activity", "turn", "error", "asking", "said", "ports", "changes")})
    return seen


def contract(s):
    """The shape of each field of the data contract, exactly, or the reason it isn't."""
    number = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)
    whole = lambda v: isinstance(v, int) and not isinstance(v, bool)
    a, t, c, ch, p = s.get("activity"), s.get("turn"), s.get("context"), s.get("changes"), s.get("ports")
    problems = []
    if not (a is None or (isinstance(a, dict) and set(a) == {"tool", "target", "since"} and isinstance(a["tool"], str) and a["tool"] and isinstance(a["target"], str) and number(a["since"]))):
        problems.append(f"activity {a}")
    if not (t is None or (isinstance(t, dict) and set(t) == {"since"} and number(t["since"]))):
        problems.append(f"turn {t}")
    if t is not None and s["state"] != "working":
        problems.append(f"turn while {s['state']}")
    if s.get("orb") not in ORBS:
        problems.append(f"orb {s.get('orb')}")
    if s.get("state") not in STATES:
        problems.append(f"state {s.get('state')}")
    if (s.get("state") == "error") != bool(s.get("error")) or ("error" in s and not (isinstance(s["error"], str) and s["error"].endswith("."))):
        problems.append(f"error {s.get('error')!r} while {s.get('state')}")
    if not (c is None or (isinstance(c, dict) and set(c) == {"used", "limit"} and whole(c["used"]) and whole(c["limit"]) and 0 < c["used"] <= c["limit"])):
        problems.append(f"context {c}")
    if not (ch is None or (isinstance(ch, dict) and set(ch) == {"added", "removed", "files"} and all(whole(v) and v >= 0 for v in ch.values()))):
        problems.append(f"changes {ch}")
    if not (isinstance(p, list) and all(whole(v) and 0 < v < 65536 for v in p) and p == sorted(set(p))):
        problems.append(f"ports {p}")
    return problems


def contract_holds(label):
    every = rows()
    bad = {s["id"]: contract(s) for s in every if contract(s)}
    check(label, not bad, bad)


# ── what is read off a screen and a transcript, without a session ──
import hub_data as hub, hub_diff, hub_status
DIALOG = """● Creating a file
 Do you want to proceed?
 ❯ 1. Yes
   2. Yes, and always allow access to
      /Users/someone/projects/x
      from this project
   3. No

 Esc to cancel · Tab to amend"""
found = hub_status.dialog_in(DIALOG)
check("a choice on screen is read with its question, and a wrapped label whole", found and found["question"] == "Do you want to proceed?" and [o["label"] for o in found["options"]] == ["Yes", "Yes, and always allow access to /Users/someone/projects/x from this project", "No"], found)
check("a numbered list higher up, with Claude's input below it, isn't a choice", hub_status.dialog_in("❯ Review comments:\n  1. a.md:2\n  2. b.md:1\n\n⏺ done\n───\n❯ \n───") is None)
check("options at the end of a reply are read in order", [o["label"] for o in hub_status.options_in("Which one?\n1. **Apple**\n2. Pear")] == ["Apple", "Pear"])
check("a numbered list that doesn't start at 1 isn't options", hub_status.options_in("Steps:\n2. b\n3. c") == [])
screens = {"empty": "\x1b[39m❯\xa0\n", "typed": "\x1b[39m❯ half typed\n", "a grey suggestion": "\x1b[39m❯ \x1b[38;5;246mdelete the file\x1b[39m\n",
           "a dim suggestion": "❯ \x1b[2mrun the tests\x1b[22m\n", "a choice's cursor": " \x1b[38;5;153m❯\x1b[39m \x1b[38;5;246m1. \x1b[38;5;153mYes\x1b[39m\n"}
real_tmux = hub.tmux
try:
    for label, raw in screens.items():
        hub.tmux = lambda *args, raw=raw: type("Done", (), {"stdout": raw})()
        check(f"Claude's input with {label} reads as {'typed' if label == 'typed' else 'nothing typed'}", hub.typed_now("hub-x") == ("half typed" if label == "typed" else ""))
finally:
    hub.tmux = real_tmux
for model, used, limit in (("claude-opus-5-5", 5, 1_000_000), ("claude-haiku-4-5-20251001", 5, 200_000), ("claude-sonnet-4-5", 300_000, 1_000_000), ("claude-sonnet-4-6", 5, 1_000_000), ("claude-fable-5-2", 5, 1_000_000), ("", 5, 1_000_000)):
    check(f"the context window of {model or 'an unknown model'} is {limit:,}", hub_status.context_limit(model, used) == limit)
hunks, cut = hub_diff.parse_patch("diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -3,2 +3,3 @@ def f():\n a\n-b\n+c\n+d\n\\ No newline at end of file\n", 100)
check("a patch is read into numbered rows", not cut and hunks == [{"old": 3, "new": 3, "head": "def f():", "rows": [[" ", 3, 3, "a"], ["-", 4, None, "b"], ["+", None, 4, "c"], ["+", None, 5, "d"], ["\\", None, None, " No newline at end of file"]]}], hunks)
check("a pane still running Claude's command is told from the shell it ends in", hub_status.waiting_on_claude("zsh -c /Users/x/.local/bin/claude 'task' --settings s; exec /bin/zsh -l") and hub_status.waiting_on_claude("sh -c claude --resume x; exec /bin/zsh -l") and not hub_status.waiting_on_claude("/bin/zsh -l") and not hub_status.waiting_on_claude("-zsh"))
check("files the hub never shows are known by their names", all(hub_diff.hidden(p) for p in (".env", "app/.env.local", "keys/id.pem", ".ssh/config", "x/.git/HEAD")) and not hub_diff.hidden("src/environment.ts"))

sweep()
launchd_before = launchd_jobs()
repo = HOME / "projects" / NAME
sids, helpers = [], []
try:
    status, made = call("POST", "/api/projects", {"name": NAME})
    check("a throwaway project is created", status == 200, made)
    quiet_alert(NAME)
    # What Claude may run here without asking, so a turn runs on its own: only in the throwaway project's own settings.
    (repo / ".claude").mkdir(exist_ok=True)
    (repo / ".claude" / "settings.local.json").write_text(json.dumps({"permissions": {"allow": ["Bash(sleep:*)", "Bash(sleep *)", "Bash(ping:*)", "Bash(ping *)"]}}))
    subprocess.run(["git", "-C", str(repo), "add", "-A"], capture_output=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "test settings"], capture_output=True)

    # ── a turn that runs a tool: activity, turn, orb, then the last tool of a finished turn ──
    task = "Use the Bash tool to run exactly this command: sleep 12 . Then reply with the single word done and nothing else."
    status, made = call("POST", "/api/sessions", {"project": NAME, "tasks": [task], "model": "haiku", "effort": "low", "mode": "manual"})
    check("a session starts", status == 200, made)
    sid = made["ids"][0]; sids.append(sid)
    answer_first_run(sid)
    busy = wait(sid, lambda s: s["state"] == "working" and (s.get("activity") or {}).get("tool") == "Bash", 90, "the running tool is said while it runs")
    if busy and busy.get("activity"):
        check("activity names the tool and what it works on", busy["activity"]["tool"] == "Bash" and "sleep 12" in busy["activity"]["target"], busy["activity"])
        check("activity says when it started", abs(time.time() - busy["activity"]["since"]) < 30, busy["activity"])
        check("the orb says a tool is in use", busy["orb"] == "using", busy["orb"])
        check("the turn says when it started, no later than its tool", busy["turn"] and busy["turn"]["since"] <= busy["activity"]["since"] + 0.01, busy["turn"])
    contract_holds("every session's fields have the contract's shape while one works")
    done = wait(sid, lambda s: s["state"] == "finished" and "done" in (s.get("said") or "").lower(), 90, "the turn finishes")
    if done:
        check("a finished turn keeps its last tool and drops its turn", (done.get("activity") or {}).get("tool") == "Bash" and done["turn"] is None and done["orb"] == "idle", {k: done.get(k) for k in ("activity", "turn", "orb")})
        c = done.get("context") or {}
        check("context comes from the transcript's usage", isinstance(c.get("used"), int) and c["used"] > 1000 and c["limit"] in (200_000, 1_000_000), c)
    contract_holds("every session's fields have the contract's shape")

    # ── changes in the session's folder: tracked edits and new files, counted by git ──
    before = wait(sid, lambda s: s.get("changes") is not None, 30, "changes are counted for a git folder")
    base = before["changes"] if before else {"added": 0, "removed": 0, "files": 0}
    with open(repo / "CLAUDE.md", "a") as f:
        f.write("line one added by the status test\nline two added by the status test\n")
    (repo / "notes-status-test.md").write_text("first\nsecond\nthird\n")
    (repo / ".env.status-test").write_text("SECRET=never-shown\n")
    (repo / "outside-link.md").symlink_to(HOME / ".ssh" / "config")
    (repo / "space name.txt").write_text("a file with a space\n")
    time.sleep(1)
    want = {"added": base["added"] + 2 + 3 + 1, "removed": base["removed"], "files": base["files"] + 5}
    after = wait(sid, lambda s: s.get("changes") == want, 40, "changes count tracked edits and new files' lines")
    status, d = call("GET", f"/api/sessions/{sid}/diff")
    files = {f["path"]: f for f in d.get("files", [])} if status == 200 else {}
    check("the diff lists each changed file", status == 200 and {"CLAUDE.md", "notes-status-test.md", ".env.status-test", "outside-link.md", "space name.txt"} <= set(files), list(files))
    md = files.get("CLAUDE.md", {})
    rows_added = [r for h in md.get("hunks") or [] for r in h["rows"] if r[0] == "+"]
    check("a tracked file's hunk has its added lines, numbered", len(rows_added) == 2 and rows_added[0][3] == "line one added by the status test" and isinstance(rows_added[0][2], int), md)
    new = files.get("notes-status-test.md", {})
    check("a new file shows whole, as added", new.get("status") == "added" and [r[3] for h in new.get("hunks") or [] for r in h["rows"]] == ["first", "second", "third"], new)
    env = files.get(".env.status-test", {})
    check("a dotenv file is listed by name and never shown", env.get("hidden") is True and env.get("hunks") is None and "never-shown" not in json.dumps(d), env)
    link = files.get("outside-link.md", {})
    check("a link out of the folder shows no content", link.get("hunks") is None, link)
    status, one = call("GET", f"/api/sessions/{sid}/diff?path={urllib.parse.quote('space name.txt')}")
    check("one changed file can be asked for by its path", status == 200 and one.get("path") == "space name.txt" and one["hunks"][0]["rows"][0][3] == "a file with a space", one)
    for label, bad in (("a traversal", "../../.ssh/config"), ("an absolute path", "/etc/passwd"), ("git internals", ".git/config"), ("a home path", "~/.ssh/config"),
                       ("a file that didn't change", "README.md"), ("a traversal inside a change's name", "notes-status-test.md/../../x")):
        check(f"the diff refuses {label}", call("GET", f"/api/sessions/{sid}/diff?path={urllib.parse.quote(bad)}")[0] == 409)
    status, env_one = call("GET", f"/api/sessions/{sid}/diff?path=.env.status-test")
    check("asked for by name, a dotenv file is still not shown", status == 200 and env_one.get("hidden") and env_one.get("hunks") is None)
    check("the diff of an unknown session is refused", call("GET", "/api/sessions/abcdef12/diff")[0] == 409)
    bare = http.client.HTTPConnection("127.0.0.1", PORT, timeout=30)
    bare.request("GET", f"/api/sessions/{sid}/diff", headers={"Host": HOST})
    check("the diff needs identity", bare.getresponse().status == 403)

    # ── review comments go in as one paste, through the path Close uses ──
    check("no comments is refused", call("POST", f"/api/sessions/{sid}/review", {"comments": []})[0] == 409)
    check("a comment with no words is refused", call("POST", f"/api/sessions/{sid}/review", {"comments": [{"path": "CLAUDE.md", "line": 3, "text": " "}]})[0] == 409)
    comments = [{"path": "notes-status-test.md", "line": 2, "quote": "second", "text": "Reply with the single word reviewed and change nothing."},
                {"path": "CLAUDE.md", "line": 1, "text": "No change needed here either."}]
    status, sent = call("POST", f"/api/sessions/{sid}/review", {"comments": comments})
    check("two comments are sent as one message", status == 200 and sent.get("sent") == 2, sent)
    reviewed = wait(sid, lambda s: s["state"] in ("finished", "needs") and "reviewed" in (s.get("said") or "").lower(), 90, "Claude answers the review")
    transcript = json.loads((STATE / "events" / f"{sid}.json").read_text()).get("transcript", "")
    prompts = [json.loads(l) for l in Path(transcript).read_text().splitlines() if '"type":"user"' in l.replace(" ", "")] if transcript else []
    joined = [json.dumps(p.get("message", {}).get("content")) for p in prompts]
    check("the comments arrived whole, in one prompt", sum("Review comments on your uncommitted changes" in j for j in joined) == 1 and any("notes-status-test.md:2" in j and "CLAUDE.md:1" in j for j in joined), joined[-2:])

    # ── peek: the last reply, an answer typed into it, and a numbered choice ──
    status, p = call("GET", f"/api/sessions/{sid}/peek")
    check("peek shows the last reply", status == 200 and "reviewed" in p.get("text", "").lower() and p["live"] and p["state"] == "finished", p)
    status, res = call("POST", f"/api/sessions/{sid}/answer", {"text": "Reply with these three lines exactly and nothing else:\nWhich fruit?\n1. Apple\n2. Pear"})
    check("a reply from the peek is sent", status == 200, res)
    asked = wait(sid, lambda s: s["state"] == "needs" and "fruit" in (s.get("asking") or "").lower(), 90, "a question with options needs you")
    status, p = call("GET", f"/api/sessions/{sid}/peek")
    check("peek lists the numbered options of the reply", status == 200 and [o["label"] for o in p.get("options", [])] == ["Apple", "Pear"] and p["dialog"] is None, p)
    check("a choice outside 1-9 is refused", call("POST", f"/api/sessions/{sid}/answer", {"choice": 12})[0] == 409)
    status, res = call("POST", f"/api/sessions/{sid}/answer", {"choice": 2})
    check("a number answers it", status == 200, res)
    wait(sid, lambda s: s["state"] in ("finished", "needs") and s.get("orb") in ("idle", "waiting") and "fruit" not in (s.get("asking") or "").lower(), 90, "Claude takes the answer")
    prompts = [l for l in Path(transcript).read_text().splitlines() if '"type":"user"' in l.replace(" ", "")]
    check("the number went in as the reply", any(json.loads(l).get("message", {}).get("content") in ("2", [{"type": "text", "text": "2"}]) for l in prompts[-4:]), prompts[-2:])
    check("an empty reply is refused", call("POST", f"/api/sessions/{sid}/answer", {"text": "  "})[0] == 409)

    # ── a permission prompt on screen: peek reads its options, a number answers it in the terminal ──
    tell(sid, "Use the Bash tool to run exactly: touch peek-permission-test.txt . Then reply with the single word touched.")
    held = wait(sid, lambda s: s["state"] == "needs" and s.get("orb") == "waiting", 90, "a permission prompt needs you")
    status, p = call("GET", f"/api/sessions/{sid}/peek")
    check("peek reads the choice Claude shows", status == 200 and p.get("dialog") and len(p["dialog"]["options"]) >= 2 and p["dialog"]["options"][0]["label"].lower().startswith("yes"), p.get("dialog") or screen(sid)[-600:])
    check("a typed reply is refused while a choice is on screen", call("POST", f"/api/sessions/{sid}/answer", {"text": "hello"})[0] == 409)
    status, res = call("POST", f"/api/sessions/{sid}/answer", {"choice": 1})
    check("its number answers it", status == 200 and res.get("sent", "").lower().startswith("yes"), res)
    wait(sid, lambda s: s["state"] == "finished" and "touched" in (s.get("said") or "").lower(), 90, "the approved command runs")
    check("and the command ran", (repo / "peek-permission-test.txt").exists())

    # ── an API failure (the StopFailure hook) is an error with one sentence, until Clear ──
    hook = lambda event: subprocess.run(["/usr/bin/python3", str(base_app / "hub_event.py")], input=json.dumps(event), text=True, env={**os.environ, "HUB_SESSION": sid, "HUB_STATE": str(STATE)})
    hook({"hook_event_name": "StopFailure", "error": "rate_limit", "error_details": "429 Too many requests"})
    failed = wait(sid, lambda s: s["state"] == "error", 10, "an API failure reads as an error")
    if failed:
        check("the error is one plain sentence, and the orb says so", failed["error"] == "The API stopped this turn: 429 Too many requests." and failed["orb"] == "retrying", failed.get("error"))
    contract_holds("the contract holds with a session in error")
    check("Clear of an unknown session is refused", call("POST", "/api/sessions/abcdef12/clear", {})[0] == 409)
    status, _ = call("POST", f"/api/sessions/{sid}/clear", {})
    cleared = wait(sid, lambda s: s["state"] != "error", 10, "Clear takes the error away")
    check("after Clear the session is idle again, with no error", status == 200 and cleared and cleared["state"] == "finished" and "error" not in cleared, cleared and cleared["state"])

    # ── Claude gone while working: the pane's process tree says so, and it is an error, never "working" ──
    tell(sid, "Use the Bash tool, in the foreground, to run exactly this command: ping -c 60 127.0.0.1 . Then reply with the single word pinged.")
    wait(sid, lambda s: s["state"] == "working" and (s.get("activity") or {}).get("target", "").startswith("ping -c 60"), 60, "a long command runs")
    pane = int(subprocess.run(TMUX + ["list-panes", "-t", f"hub-{sid}", "-F", "#{pane_pid}"], capture_output=True, text=True).stdout.split()[0])
    claude = [int(p) for p in subprocess.run(["pgrep", "-P", str(pane)], capture_output=True, text=True).stdout.split()]
    for pid in claude:
        os.kill(pid, signal.SIGKILL)
    gone = wait(sid, lambda s: s["state"] == "error", 20, "Claude killed mid-turn is an error")
    if gone:
        check("it says the process is gone", "process is gone" in gone.get("error", "") and gone["orb"] == "retrying" and gone["alive"], gone.get("error"))
    status, _ = call("POST", f"/api/sessions/{sid}/clear", {})
    after_clear = wait(sid, lambda s: s["state"] != "error", 10, "Clear takes it away")
    check("cleared, it reads as Claude not running (the terminal stays)", status == 200 and after_clear and after_clear["state"] == "ended" and after_clear["alive"], after_clear and after_clear["state"])

    # ── ports: what a session's own processes listen on; Stop reaches only those ──
    status, made = call("POST", "/api/sessions", {"project": NAME, "kind": "shell"})
    shell = made["ids"][0]; sids.append(shell)
    time.sleep(1.5)
    subprocess.run(TMUX + ["send-keys", "-t", f"hub-{shell}", "-l", "/usr/bin/python3 -m http.server 8631 --bind 127.0.0.1"], check=True)
    subprocess.run(TMUX + ["send-keys", "-t", f"hub-{shell}", "Enter"], check=True)
    stranger = subprocess.Popen(["/usr/bin/python3", "-m", "http.server", "8632", "--bind", "127.0.0.1"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    helpers.append(stranger)
    listed = wait(shell, lambda s: s.get("ports") == [8631], 30, "a session lists the port its process listens on")
    check("another session's list doesn't have it", 8631 not in (row(sid) or {}).get("ports", [8631]))
    check("Stop on a port another session holds is refused", call("POST", f"/api/sessions/{sid}/port-stop", {"port": 8631})[0] == 409)
    check("Stop on a port no session holds is refused", call("POST", f"/api/sessions/{shell}/port-stop", {"port": 8632})[0] == 409 and stranger.poll() is None)
    check("Stop with no port is refused", call("POST", f"/api/sessions/{shell}/port-stop", {"port": "8631"})[0] == 409)
    status, res = call("POST", f"/api/sessions/{shell}/port-stop", {"port": 8631})
    check("Stop on its own port stops that process", status == 200 and res.get("stopped"), res)
    check("the port is free again", wait(shell, lambda s: s.get("ports") == [], 20) is not None and socket.socket().connect_ex(("127.0.0.1", 8631)) != 0)
    check("the shell itself is still running", (row(shell) or {}).get("state") == "shell")
    contract_holds("the contract holds at the end")

    # ── the poll stays cheap: a fresh state with every field ──
    time.sleep(1.6)
    started = time.time()
    call("GET", "/api/state")
    took = time.time() - started
    check("a fresh poll with every field takes under a second", took < 1.0, f"{took:.2f}s")
finally:
    try:
        for helper in helpers:
            if helper.poll() is None:
                helper.terminate(); helper.wait(10)
        for s in sids:
            call("POST", f"/api/sessions/{s}/dismiss", {})
    finally:
        left = remove_project(NAME)
        check("the throwaway project is gone", not left, left)
        check("launchd's folder holds what it held before", launchd_jobs() == launchd_before)
print("--- failures:", fails)
sys.exit(1 if fails else 0)

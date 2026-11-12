#!/usr/bin/env python3
"""Close, record and Resume end to end, with real Claude sessions (Haiku, plan mode, so nothing is edited) in a
throwaway project, hub-test-throwaway-<n>, which is made here and removed at the end.

Runs against a second instance (the six HUB_* variables, see tests/instance.py). HUB_RESTART=1 adds a
check that a close cut short by a server restart still finishes: the script pauses at RESTART_NOW until you restart
that server and press Enter.
Run: /usr/bin/python3 tests/lifecycle_check.py
"""
import http.client, json, os, shutil, subprocess, sys, time
from pathlib import Path

from instance import NAME, OWNER, PORT, STATE, TMUX, WRITE, answer_first_run, launchd_jobs, quiet_alert, remove_project, sweep, tell

SCRATCH = Path(os.environ.get("TMPDIR", "/tmp"))
fails = 0
def check(label, ok, extra=""):
    global fails; fails += 0 if ok else 1
    print(("PASS  " if ok else "FAIL  ") + label + ("" if ok else f"  [{str(extra)[:300]}]"), flush=True)
def call(method, path, body=None):
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=60)
    c.request(method, path, json.dumps(body) if body is not None else None, WRITE if body is not None else OWNER)
    r = c.getresponse(); return r.status, json.loads(r.read() or b"{}")
def row(sid):
    return next((s for s in call("GET", "/api/state")[1]["sessions"] if s["id"] == sid), None)
def wait(sid, ok, limit, label):
    end = time.time() + limit
    while time.time() < end:
        r = row(sid)
        if r and ok(r): return r
        time.sleep(2)
    check(label, False, row(sid)); return row(sid)
say = tell  # as one paste, then Enter once it is seen in Claude's input

sweep()
launchd_before = launchd_jobs()
sid = leftover = empty = hidden = None
try:
    status, made = call("POST", "/api/projects", {"name": NAME})
    check("a throwaway project is created", status == 200, made)
    quiet_alert(NAME)
    status, made = call("POST", "/api/sessions", {"project": NAME, "tasks": ["Reply with the single word ready and nothing else"], "isolate": False, "mode": "plan", "model": "haiku", "effort": "low"})
    check("a session starts", status == 200, made); sid = made["ids"][0]; print("  sid", sid)
    answer_first_run(sid)
    r = wait(sid, lambda r: r["state"] == "finished" and r.get("said"), 120, "it answers")
    check("it answered 'ready'", r and "ready" in (r.get("said") or "").lower(), r and r.get("said"))
    status, chk = call("GET", f"/api/sessions/{sid}/closing")
    check("the close check sees it alive and idle", status == 200 and chk["alive"] and not chk["working"] and not chk["typed"], chk); print("  close check", chk)
    # a server left running from the session's folder: loopback only, serving an empty folder, stopped at the end
    empty = SCRATCH / f"hub-empty-{sid}"; empty.mkdir(exist_ok=True)
    folder = json.loads((STATE / "sessions" / f"{sid}.json").read_text())["folder"]
    leftover = subprocess.Popen(["/usr/bin/python3", "-m", "http.server", "8619", "--bind", "127.0.0.1", "--directory", str(empty)], cwd=folder,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    time.sleep(1.5)
    status, res = call("POST", f"/api/sessions/{sid}/close", {"note": True})
    check("close with a handoff starts", status == 200 and res.get("closing"), res)
    r = row(sid); check("the row says closing", r["state"] == "closing", r["state"])
    status, res = call("POST", f"/api/sessions/{sid}/close", {"note": True})
    check("a second close is refused while closing", status == 409, res)
    r = wait(sid, lambda r: r["state"] == "closed", 400, "it closes")
    check("it is closed and not running", r and r["state"] == "closed" and not r["alive"], r)
    check("the handoff note was kept", r and "done" in (r.get("note") or "").lower(), r and r.get("note")); print("  note:", repr(r and r.get("note")))
    check("the handoff says what was cleaned", r and "cleaned" in (r.get("note") or "").lower(), r and r.get("note"))
    check("it can be resumed", r and r.get("resumable"), r)
    check("the transcript is copied into the hub", (STATE / "records" / sid / "transcript.jsonl").exists())
    check("the tmux session is gone", subprocess.run(TMUX + ["has-session", "-t", f"hub-{sid}"]).returncode != 0)
    status, rec = call("GET", f"/api/sessions/{sid}/record")
    kinds = [i["kind"] for i in rec.get("items", [])]
    check("the record has what you asked and what Claude said", status == 200 and kinds[:1] == ["you"] and "claude" in kinds, rec)
    check("the handoff prompt and reply aren't shown as turns", not any("[Dream Hub]" in i.get("text", "") or i.get("text", "").strip() == rec["note"].strip() for i in rec["items"]), rec["items"])
    check("the record carries the note", rec.get("note") and rec.get("closedAt"), rec)
    check("the record lists the server still running from the folder", any(8619 in x["ports"] for x in rec.get("running", [])), rec.get("running"))
    check("the hub only lists it: the server is still up", leftover.poll() is None)
    leftover.terminate(); leftover.wait(10); shutil.rmtree(empty, ignore_errors=True)
    meta = json.loads((STATE / "sessions" / f"{sid}.json").read_text()); claude_id = meta.get("claude_session"); print("  claude session", claude_id)

    status, res = call("POST", f"/api/sessions/{sid}/resume", {})
    check("resume starts it again", status == 200, res)
    r = wait(sid, lambda r: r["alive"] and r["state"] == "finished", 90, "resumed session is ready")
    check("the resumed row is no longer closed", r and r["state"] != "closed" and r.get("closedAt") is None, r)
    say(sid, "What single word did you reply with at the very start of this conversation? Answer with that word only.")
    time.sleep(4)
    r = wait(sid, lambda r: r["state"] == "finished", 90, "it answers after resuming")
    check("it remembers the conversation", r and "ready" in (r.get("said") or "").lower(), r and r.get("said"))
    ev = json.loads((STATE / "events" / f"{sid}.json").read_text())
    check("it continues the same Claude session", Path(ev["transcript"]).stem == claude_id, ev["transcript"])

    status, res = call("POST", f"/api/sessions/{sid}/close", {"note": False})
    r = row(sid)
    check("close now is immediate and has no note", status == 200 and r["state"] == "closed" and not r.get("note"), r)

    # Claude Code's 30-day cleanup: its own transcript disappears; the hub's copy carries on
    live = Path(ev["transcript"]); hidden = SCRATCH / f"hub-hidden-{sid}.jsonl"; shutil.move(live, hidden)
    status, rec = call("GET", f"/api/sessions/{sid}/record")
    check("the record still reads after Claude cleans up", status == 200 and len(rec["items"]) >= 4, rec)
    r = row(sid); check("it is still resumable", r.get("resumable"), r)
    status, res = call("POST", f"/api/sessions/{sid}/resume", {})
    check("resume puts the transcript back and starts", status == 200 and live.exists(), res)
    r = wait(sid, lambda r: r["alive"] and r["state"] == "finished", 90, "restored session is ready")
    say(sid, "Repeat the single word you replied with at the very start. One word only.")
    time.sleep(4)
    r = wait(sid, lambda r: r["state"] == "finished", 90, "it answers after restore")
    check("the restored conversation is intact", r and "ready" in (r.get("said") or "").lower(), r and r.get("said"))
    hidden.unlink()

    # The hub restarts while waiting for a handoff: the close still finishes
    status, res = call("POST", f"/api/sessions/{sid}/close", {"note": True})
    check("close with handoff starts again", status == 200, res)
    if os.environ.get("HUB_RESTART"):
        print("RESTART_NOW", flush=True)
        input()
    r = wait(sid, lambda r: r["state"] == "closed", 180, "it closes again")
    check("the last close finishes", r and r["state"] == "closed" and not r["alive"], r)

    status, res = call("POST", f"/api/sessions/{sid}/dismiss", {})
    check("delete removes the kept record", status == 200 and not (STATE / "records" / sid).exists(), res)
    sid = None
    status, res = call("POST", "/api/sessions/abcdef12/resume", {})
    check("resuming an unknown session is refused", status == 409, res)
finally:
    try:
        if sid:
            call("POST", f"/api/sessions/{sid}/dismiss", {})
    finally:  # whatever the server did or didn't answer: the helper server, the transcript set aside, then the project
        if leftover and leftover.poll() is None:
            leftover.terminate(); leftover.wait(10)
        if empty:
            shutil.rmtree(empty, ignore_errors=True)
        if hidden:
            hidden.unlink(missing_ok=True)
        left = remove_project(NAME)
        check("the throwaway project is gone", not left, left)
        check("launchd's folder holds what it held before", launchd_jobs() == launchd_before, sorted(set(launchd_jobs()) ^ set(launchd_before)))
print("--- failures:", fails)
sys.exit(1 if fails else 0)

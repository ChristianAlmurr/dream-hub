#!/usr/bin/env python3
"""Tests the running app: access rules, memory, dreams, and real sessions in a throwaway project.

Runs against a second instance on loopback (the six HUB_* variables, see tests/instance.py), with the headers Tailscale
Serve would add, and against the real kit: proposals, dreams and their states are made by the kit's own scripts.
Creates hub-test-throwaway-<n> and removes it. Nothing is written to launchd's folder, and no real project or store
is asked for anything: every request goes to the throwaway project.
Run: /usr/bin/python3 tests/test_hub.py
"""
import base64, contextlib, fcntl, http.client, io, json, os, plistlib, re, shutil, socket, struct, subprocess, sys, tempfile, time, urllib.parse
from pathlib import Path

from instance import CFG, DREAMER, HOME, HOST, KIT, NAME, OWNER, PORT, SMALL, STATE, TMUX, WRITE, answer_first_run, PROPOSAL, base, check_file, collect, hub, in_repo, job_of, kit, launchd_jobs, own_notes, screen as tmux_screen, seen as on_screen, tell, write_dream, quiet_alert, remove_project, set_own_notes, sweep, write_job

fails = 0
sweep()
launchd_before = launchd_jobs()
# The kit's state file, by what the kit does with it (scripts/dream_due.py, load() and trusted()): True for a file it refuses.
LATER = int(time.time()) + 86400
STATE_FILES = (
    ("valid JSON that isn't an object", "[1, 2]", True),
    ("not JSON", "{", True),
    ("a time that isn't a number", json.dumps({"checked": "yesterday", "verdict": "not due: 1 session, 0 memory lines, 1 day"}), True),
    ("a time that is true", json.dumps({"last_dream": True}), True),
    ("a verdict that isn't text", json.dumps({"checked": 1, "verdict": {"due": False}}), True),
    ("a try with no time", json.dumps({"tries": [{"ended": "failed"}]}), True),
    ("a try that ended some other way", json.dumps({"tries": [{"at": 1, "ended": "crashed"}]}), True),
    ("tries that aren't a list", json.dumps({"tries": {"at": 1, "ended": None}}), True),
    ("a try with no ending", json.dumps({"tries": [{"at": 1}]}), True),
    ("a try whose alert time isn't a number", json.dumps({"tries": [{"at": 1, "ended": None, "alerted": "today"}]}), True),
    ("a try later than now", json.dumps({"tries": [{"at": LATER, "ended": None}]}), True),
    ("a last dream later than now", json.dumps({"last_dream": LATER}), True),
    ("a reminder with no proposal named", json.dumps({"reminded": {"at": 5}}), True),
    ("a reminder that isn't a record", json.dumps({"reminded": "2026-11-12-proposal.md"}), True),
    ("a reminder later than now", json.dumps({"reminded": {"proposal": "2026-11-12-proposal.md", "at": LATER}}), True),
    ("a reminder of an open proposal", json.dumps({"last_dream": 3, "checked": 5, "verdict": "not due: the proposal 2026-11-12-proposal.md (of 12 Nov, open for 3 days) has not been applied or dismissed", "tries": [], "reminded": {"proposal": "2026-11-12-proposal.md", "at": 5}}), False),
    ("no check since the last dream finished", json.dumps({"last_dream": 5, "last_reason": "started by hand", "checked": None, "verdict": None, "tries": [], "reminded": None, "waiting_said": None}), False),
    ("a reminder of what waits that isn't a time", json.dumps({"waiting_said": "yesterday"}), True),
    ("a reminder of what waits later than now", json.dumps({"waiting_said": LATER}), True),
    ("a reminder of what waits", json.dumps({"last_dream": 3, "checked": 5, "verdict": "not due: 0 sessions, 0 memory lines, 0 days", "tries": [], "reminded": None, "waiting_said": 5}), False),
    ("a try that was alerted about", json.dumps({"checked": 5, "verdict": "not due: a dream is running", "tries": [{"at": 4, "ended": None, "alerted": 5}]}), False),
    ("nothing in it", "{}", False),
    ("every key null", json.dumps({"last_dream": None, "last_reason": None, "checked": None, "verdict": None, "tries": None}), False),
    ("a try still running", json.dumps({"checked": 5, "verdict": "due: 3 sessions since 8 Nov", "tries": [{"at": 4, "ended": "proposal"}, {"at": 5, "ended": None}]}), False),
    ("a key from a later kit", json.dumps({"checked": 5, "verdict": "not due: 0 sessions, 0 memory lines, 0 days", "later": [1]}), False),
)


def check(label, ok, extra=""):
    global fails
    print(("PASS  " if ok else "FAIL  ") + label + (f"  [{extra}]" if not ok and extra else ""), flush=True)
    fails += 0 if ok else 1


def call(method, path, headers=OWNER, body=None):
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=60)
    c.request(method, path, json.dumps(body) if body is not None else None, headers)
    r = c.getresponse()
    raw = r.read()
    try:
        return r.status, json.loads(raw)
    except ValueError:
        return r.status, raw


def post(path, body):
    return call("POST", path, WRITE, body)


def ws_open(path, headers):
    s = socket.create_connection(("127.0.0.1", PORT), timeout=20)
    key = base64.b64encode(os.urandom(16)).decode()
    lines = [f"GET {path} HTTP/1.1", "Upgrade: websocket", "Connection: Upgrade", f"Sec-WebSocket-Key: {key}", "Sec-WebSocket-Version: 13"]
    lines += [f"{k}: {v}" for k, v in headers.items()]
    s.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
    data = b""
    while b"\r\n\r\n" not in data:
        data += s.recv(4096)
    return s, data.split(b"\r\n")[0].decode(), data.split(b"\r\n\r\n", 1)[1]


def ws_send(s, text):
    payload, mask = text.encode(), os.urandom(4)
    head = bytes([0x81, 0x80 | len(payload)]) if len(payload) < 126 else bytes([0x81, 0x80 | 126]) + struct.pack(">H", len(payload))
    s.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))


def ws_collect(s, seconds):
    s.settimeout(0.4)
    data, end = b"", time.time() + seconds
    while time.time() < end:
        try:
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
        except socket.timeout:
            pass
    return data


def session(sid):
    return next((x for x in call("GET", "/api/state")[1]["sessions"] if x["id"] == sid), None)


def wait(sid, test, seconds=60):
    end = time.time() + seconds
    while time.time() < end:
        s = session(sid)
        if s and test(s):
            return s
        time.sleep(1.5)
    return session(sid)


def project(name=NAME):
    """The project as the next poll shows it. Reads are cached for a moment, so this waits that moment out."""
    time.sleep(1.6)
    return next(x for x in call("GET", "/api/state")[1]["projects"] if x["name"] == name)


# ── access ──
check("no identity is refused", call("GET", "/api/state", {"Host": HOST})[0] == 403)
check("another tailnet user is refused", call("GET", "/api/state", {"Host": HOST, "Tailscale-User-Login": "someone@else.com"})[0] == 403)
check("the wrong Host is refused", call("GET", "/api/state", {**OWNER, "Host": "evil.example"})[0] == 403)
check("the page itself needs identity", call("GET", "/", {"Host": HOST})[0] == 403)
check("a write with no Origin is refused", call("POST", "/api/sessions", {**OWNER, "Content-Type": "application/json"}, {})[0] == 403)
check("a write from another site is refused", call("POST", "/api/sessions", {**WRITE, "Origin": "https://evil.example"}, {})[0] == 403)
check("form-encoded writes are refused", call("POST", "/api/sessions", {**WRITE, "Content-Type": "text/plain"}, {})[0] == 400)
for label, headers in (("no identity", {"Host": HOST, "Origin": CFG["origin"]}), ("another site", {**OWNER, "Origin": "https://evil.example"}), ("no origin", OWNER)):
    s, status, _ = ws_open("/ws/term/deadbeef", headers)
    check(f"terminal upgrade refused: {label}", " 403 " in status, status)
    s.close()
check("static path traversal is refused", call("GET", "/ui/../server.py")[0] == 404 and call("GET", "/ui/%2e%2e/server.py")[0] == 404)
headers = http.client.HTTPConnection("127.0.0.1", PORT)
headers.request("GET", "/", headers=OWNER)
response = headers.getresponse()
check("content security policy is sent", "default-src 'self'" in (response.getheader("Content-Security-Policy") or ""))

# ── which hub this is: none of the six variables, or all of them ──
five = {"HUB_PORT": "8492", "HUB_STATE": "/tmp/hub2/state", "HUB_TMUX": "agent-hub-2", "HUB_CONFIG": "/tmp/hub2/config.json", "HUB_KIT": "/tmp/kit", "HUB_JOBS": "/tmp/hub2/jobs"}


def said(env):
    """The one sentence a set of variables is refused with, or nothing when it is accepted."""
    try:
        hub.read_instance(env)
    except hub.BadInstance as problem:
        return str(problem)
    return ""


installed = hub.read_instance({"PATH": "/usr/bin"})
check("with none of the variables it is the installed hub", (installed["port"], installed["state"], installed["socket"], installed["config"], installed["kit"], installed["jobs"]) == (base.installed_port(), HOME / ".local/state/agent-hub", "agent-hub", HOME / ".config/agent-hub/config.json", HOME / "tools/agent-memory-kit", HOME / "Library/LaunchAgents"), str(installed))
real = lambda path: Path(os.path.realpath(path))
check("all six make a second instance, each path taken as the folder it really is", hub.read_instance(five) == {"port": 8492, "state": real("/tmp/hub2/state"), "socket": "agent-hub-2", "config": real("/tmp/hub2/config.json"), "kit": real("/tmp/kit"), "jobs": real("/tmp/hub2/jobs")})
roundabout = hub.read_instance({**five, "HUB_JOBS": str(HOME / "Library" / ".." / "Library" / "LaunchAgents")})["jobs"]
check("a roundabout spelling of launchd's folder is launchd's folder", roundabout == base.LAUNCHD_JOBS, str(roundabout))
guard = lambda **env: subprocess.run(["/usr/bin/python3", "-c", "import instance"], cwd=hub.APP / "tests", env={**{k: v for k, v in os.environ.items()}, **env}, capture_output=True, text=True)
for label, env, names in (("launchd's folder spelled another way", {"HUB_JOBS": str(HOME / "Library" / ".." / "Library" / "LaunchAgents")}, "HUB_JOBS"),
                          ("the installed hub's port", {"HUB_PORT": "8491"}, "HUB_PORT"), ("the installed hub's state folder", {"HUB_STATE": str(HOME / ".local/state/agent-hub")}, "HUB_STATE"),
                          ("the installed hub's tmux socket", {"HUB_TMUX": "agent-hub"}, "HUB_TMUX")):
    ran = guard(**env)
    check(f"the tests refuse to run on {label}", ran.returncode != 0 and ran.stderr.strip().startswith(names), ran.stderr[-300:])
for name in five:
    alone = said({name: five[name]})
    check(f"{name} by itself is refused", "is not set: a second instance needs all six" in alone and alone.split()[0] in five and alone.split()[0] != name, alone)
    check(f"leaving out only {name} is refused, naming it", said({k: v for k, v in five.items() if k != name}).startswith(f"{name} is not set"))
    check(f"an empty {name} is refused, naming it", said({**five, name: ""}) == f"{name} is set but empty." and said({**five, name: "  "}) == f"{name} is set but empty.")
for port in ("0", "65536", "-1", "80a", "8492.0"):
    check(f"port {port} is refused", said({**five, "HUB_PORT": port}).startswith("HUB_PORT must be a port from 1 to 65535"), said({**five, "HUB_PORT": port}))
for name in ("HUB_STATE", "HUB_CONFIG", "HUB_KIT", "HUB_JOBS"):
    check(f"a relative {name} is refused, naming it", said({**five, name: "hub2/x"}).startswith(f"{name} must be an absolute path"), said({**five, name: "hub2/x"}))
bare = {k: v for k, v in os.environ.items() if not k.startswith("HUB_")}
half = subprocess.run(["/usr/bin/python3", "-c", "import hub_data"], cwd=hub.APP, env={**bare, "HUB_PORT": "8492"}, capture_output=True, text=True)
check("a second instance reads its dream jobs from its own folder and so never asks the kit to schedule", base.JOBS != base.LAUNCHD_JOBS and kit.SCHEDULES is False, str(base.JOBS))
check("a server given only a port stops before it touches anything", half.returncode == 1 and half.stderr.strip() == "HUB_STATE is not set: a second instance needs all six of HUB_PORT, HUB_STATE, HUB_TMUX, HUB_CONFIG, HUB_KIT, HUB_JOBS.", half.stderr)
event = json.dumps({"hook_event_name": "Stop", "transcript_path": ""})
probe = f"probe{os.getpid()}"
hook = lambda state: subprocess.run(["/usr/bin/python3", str(hub.APP / "hub_event.py")], input=event, env={**bare, "HUB_SESSION": probe, **state}, capture_output=True, text=True)
elsewhere = Path(tempfile.mkdtemp(prefix="hub-rules."))
try:
    for label, value in (("empty", ""), ("relative", "state")):
        ran = hook({"HUB_STATE": value})
        check(f"the hook refuses an {label} HUB_STATE and writes nowhere", ran.returncode == 1 and ran.stderr.startswith("HUB_STATE must be an absolute path") and not (HOME / ".local/state/agent-hub/events" / f"{probe}.json").exists() and not list(elsewhere.rglob("*.json")), ran.stderr)
    ran = hook({"HUB_STATE": str(elsewhere / "state")})
    check("the hook writes into the state folder it is given", ran.returncode == 0 and json.loads((elsewhere / "state/events" / f"{probe}.json").read_text())["state"] == "finished", ran.stderr)
    # the installer is for the one installed hub: it refuses before it checks or writes anything.
    for name in five:
        ran = subprocess.run(["/bin/sh", str(hub.APP.parent / "install.sh")], env={"PATH": "/usr/bin:/bin", "HOME": str(elsewhere), name: five[name]}, capture_output=True, text=True)
        check(f"install.sh refuses to run with {name} set", ran.returncode == 2 and ran.stderr.startswith(f"{name} is set") and not any(elsewhere.glob(".*")), ran.stderr)

    # ── Claude's own notes: the hub asks the kit, so in every case the two give one answer ──
    home, folder, shop = elsewhere / "home", elsewhere / "repo", elsewhere / "shop"
    files = {"local": folder / ".claude/settings.local.json", "shared": folder / ".claude/settings.json", "own": home / ".claude/settings.json"}
    for path in files.values():
        path.parent.mkdir(parents=True, exist_ok=True)
    shop.mkdir()
    (shop / "roots.txt").write_text(f"{folder}\n")
    there = {"HOME": str(home)}
    ASK = "import json, sys; sys.path.insert(0, sys.argv[1]); import own_notes; r = sys.argv[2]; print(json.dumps([own_notes.enabled(r), str(own_notes.folder(r)), len(own_notes.notes(r))]))"

    def both():
        """What the hub says and what the kit's own functions say, about the same files."""
        told = kit.own_notes_of(shop, [str(folder)], there)
        theirs = subprocess.run(["python3", "-c", ASK, str(KIT / "scripts"), str(folder)], env={**hub.ENV, **there, "AGENT_MEMORY_DIR": str(shop)}, capture_output=True, text=True)
        return told and [told[str(folder)]["on"], str(told[str(folder)]["folder"]), told[str(folder)]["notes"]], json.loads(theirs.stdout) if theirs.returncode == 0 else theirs.stderr

    def settings(**values):
        for key, path in files.items():
            if path.is_dir():
                path.rmdir()
            path.unlink(missing_ok=True)
            if key in values:
                path.write_text(values[key] if isinstance(values[key], str) else json.dumps(values[key]))
        return both()

    off, on = {"autoMemoryEnabled": False}, {"model": "x", "autoMemoryEnabled": True}
    switch = lambda value: {"env": {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": value}}
    usual = str(home / ".claude/projects" / re.sub(r"[^A-Za-z0-9]", "-", str(folder)) / "memory")
    for label, values, expected in (
            ("on when no settings file says otherwise", {}, True),
            ("your own settings turn it off", {"own": off}, False),
            ("the folder's shared settings outrank your own", {"shared": on, "own": off}, True),
            ("the folder's local settings outrank both", {"local": off, "shared": on, "own": on}, False),
            ("a file that doesn't set it passes to the next", {"local": {"model": "x"}, "shared": off}, False),
            ("a file that isn't a JSON object is skipped", {"local": "[]", "shared": "{", "own": off}, False),
            ("a value that isn't true or false sets nothing", {"local": {"autoMemoryEnabled": "no"}}, True),
            ("the env switch turns it off over autoMemoryEnabled at a higher level", {"local": on, "own": switch("1")}, False),
            ("the env switch turns it on over autoMemoryEnabled", {"shared": off, "own": switch("0")}, True),
            ("the highest level that sets the switch wins", {"local": switch("0"), "own": switch("1")}, True)):
        told, theirs = settings(**values)
        check(f"auto memory, hub and kit agree: {label}", told == theirs and told == [expected, usual, 0], f"hub {told}, kit {theirs}")
    settings(shared=off)
    files["local"].mkdir()
    told, theirs = both()
    check("auto memory, hub and kit agree: a settings file that is a folder is skipped", told == theirs and told == [False, usual, 0], f"hub {told}, kit {theirs}")
    Path(usual).mkdir(parents=True)
    (Path(usual) / "MEMORY.md").write_text("- [Tests](feedback_tests.md)\n")
    (Path(usual) / "feedback_tests.md").write_text("Run the tests.\n")
    (Path(usual) / "folder.md").mkdir()
    told, theirs = settings()
    check("notes of Claude's own, hub and kit agree: files are counted, folders aren't", told == theirs and told == [True, usual, 2], f"hub {told}, kit {theirs}")
    moved = elsewhere / "notes kept elsewhere, with a comma"
    moved.mkdir()
    (moved / "one.md").write_text("One.\n")
    told, theirs = settings(own={"autoMemoryDirectory": str(moved)})
    check("notes of Claude's own, hub and kit agree: a folder the settings name", told == theirs and told == [True, str(moved), 1], f"hub {told}, kit {theirs}")
    (shop / "dreams").mkdir()
    (shop / "dreams/own-notes.json").write_text("[]")
    quiet = io.StringIO()
    with contextlib.redirect_stdout(quiet):
        told, theirs = settings()
    check("when the kit can't say, the hub says nothing and logs why", told is None and isinstance(theirs, list) and "own-notes.json" in quiet.getvalue(), f"hub {told}, log {quiet.getvalue()}")

    # ── an item's id: the hub names an item as the kit does ──
    IDS = "import json, sys; sys.path.insert(0, sys.argv[1]); import dream_proposal; print(json.dumps([[i['item'], i['id']] for i in dream_proposal.items(sys.stdin.read())]))"
    sample = PROPOSAL.replace("{DAY}", "2026-01-01").replace("{REST}", "\n## Scratchpads to condense / clear\n- [x] main — keep: nothing. Clear.\n"
                                                         "\n## Needs you\n- [ ] A tick line here is not an item · evidence: a1\n- Nor is a plain line · evidence: a2\n"
                                                         "\n## Claude's own notes (auto memory)\n- [ ] 9. notes shop/one.md — carry — 2 checked facts\n      why: checked, not in memory · evidence: shop/one.md\n"
                                                         '      + project/gotchas.md "- [2026-01-01] Carried"\n      - project/gotchas.md "- [2026-01-01] Left out (checked: ls, on this machine)" · machine: 2\n')
    theirs = subprocess.run(["python3", "-c", IDS, str(KIT / "scripts")], input=sample, env={**hub.ENV, "AGENT_MEMORY_DIR": str(shop)}, capture_output=True, text=True)
    ours = [i["id"] for sec in hub.parse_proposal(sample)["sections"] for i in sec["items"]]
    check("the hub and the kit give every item of a proposal the same id, numbered or not, a carry item with a fact left out among them", theirs.returncode == 0 and len(ours) == 10 and ours == [row[1] for row in json.loads(theirs.stdout)], f"{ours} {theirs.stdout} {theirs.stderr}")
    carry_item = next(i for sec in hub.parse_proposal(sample)["sections"] for i in sec["items"] if i["file"] == "notes shop/one.md")
    check("a carry item keeps its id whatever the signs of its facts", carry_item["id"] == next(i for sec in hub.parse_proposal(sample.replace('      - project/gotchas.md "- [2026-01-01] Left', '      + project/gotchas.md "- [2026-01-01] Left'))["sections"] for i in sec["items"] if i["file"] == "notes shop/one.md")["id"]
          and [(f["sign"], f["machine"]) for f in carry_item["facts"]] == [("+", None), ("-", "2")] and "+ project/gotchas.md" not in str(carry_item["details"]), str(carry_item))

    # ── the kit's state file: the hub refuses what the kit refuses ──
    def kit_refuses(content):
        """Whether the kit's own load() and trusted() turn this state file away, in a home folder made for the question."""
        place = Path(tempfile.mkdtemp(prefix="state.", dir=elsewhere))
        file = place / ".local/state/agent-memory/a-store.json"
        file.parent.mkdir(parents=True)
        file.write_text(content)
        ran = subprocess.run(["python3", "-c", "import sys; sys.path.insert(0, sys.argv[1]); import dream_due; dream_due.trusted(dream_due.load(), dream_due.now())", str(KIT / "scripts")],
                             env={**hub.ENV, "HOME": str(place), "AGENT_MEMORY_DIR": str(place / "a-store")}, capture_output=True, text=True)
        return ran.returncode != 0

    real_state, real_stores = kit.KIT_STATE, hub.STORES
    try:
        kit.KIT_STATE = elsewhere / "kit-state"
        kit.KIT_STATE.mkdir()
        with contextlib.redirect_stdout(io.StringIO()):
            for label, content, refused in STATE_FILES:
                (kit.KIT_STATE / "a-store.json").write_text(content)
                seen = kit.dream_check_of("a-store")
                check(f"a state file with {label} is {'refused with a plain line' if refused else 'read'}", ("problem" in seen) is refused and (not refused or seen["problem"].startswith("The kit can't read its record")), str(seen))
        for label, content, refused in STATE_FILES:
            check(f"the kit {'refuses' if refused else 'reads'} a state file with {label}, as the hub does", kit_refuses(content) is refused)
        hub.STORES = elsewhere / "stores"
        for name, org in (("good-one", True), ("Bad Name", True), (".hidden", True), ("no-principles", False)):
            (hub.STORES / name).mkdir(parents=True, exist_ok=True)
            if org:
                (hub.STORES / name / "ORG.md").write_text("x\n")
        check("only folders with a project's kind of name are projects", [s.name for s in hub.stores()] == ["good-one"], str(hub.stores()))
    finally:
        kit.KIT_STATE, hub.STORES = real_state, real_stores
finally:
    shutil.rmtree(elsewhere, ignore_errors=True)

# ── link previews: fetched by the hub only for the person, and never from this Mac or its networks ──
import hub_preview as preview
for address, ok in (("8.8.8.8", True), ("93.184.216.34", True), ("2606:4700::1111", True), ("127.0.0.1", False), ("127.8.8.8", False), ("10.0.0.1", False),
                    ("172.16.5.4", False), ("192.168.1.10", False), ("169.254.169.254", False), ("100.64.0.1", False), ("100.100.100.100", False),
                    ("100.127.255.254", False), ("224.0.0.251", False), ("239.255.255.250", False), ("0.0.0.0", False), ("255.255.255.255", False),
                    ("240.0.0.1", False), ("::1", False), ("::", False), ("fe80::1", False), ("fd7a:115c:a1e0::1", False), ("ff02::1", False),
                    ("::ffff:127.0.0.1", False), ("::ffff:10.0.0.1", False), ("64:ff9b::a9fe:a9fe", False), ("2002:c0a8:0101::", False), ("2001:0:4136:e378::1", False), ("nonsense", False)):
    check(f"{address} is {'public' if ok else 'refused'}", preview.public(address) is ok)
for label, url in (("ftp", "ftp://example.com/x"), ("a file address", "file:///etc/passwd"), ("a name and password", "http://me:pw@example.com/"),
                   ("an odd port", "http://example.com:22/"), ("this Mac by name", "http://localhost/"), ("loopback", "http://127.0.0.1/"),
                   ("loopback as one number", "http://2130706433/"), ("loopback in hex", "http://0x7f.1/"), ("IPv6 loopback", "http://[::1]/"),
                   ("IPv4 inside IPv6", "http://[::ffff:127.0.0.1]/"), ("the home network", "http://192.168.1.1/"), ("cloud metadata", "http://169.254.169.254/latest/meta-data/"),
                   ("the tailnet", "http://100.100.100.100/"), ("multicast", "http://224.0.0.1/"), ("no host", "http:///x")):
    try:
        preview.checked_address(url)
        check(f"a preview of {label} is refused", False, url)
    except preview.NotFetched:
        check(f"a preview of {label} is refused", True)
    status, said_ = call("GET", f"/api/preview/link?url={urllib.parse.quote(url, safe='')}")
    check(f"the hub refuses to fetch {label}", status == 200 and said_.get("refused") and not said_.get("title"), str(said_))
check("a link preview asked for by another site is refused", call("GET", "/api/preview/link?url=https%3A%2F%2Fexample.com%2F", {**OWNER, "Sec-Fetch-Site": "cross-site"})[0] == 403)
check("a link preview needs identity", call("GET", "/api/preview/link?url=https%3A%2F%2Fexample.com%2F", {"Host": HOST})[0] == 403)
check("a picture the hub never saw named is refused", call("GET", "/api/preview/image?id=" + "a" * 32)[0] == 409 and call("GET", "/api/preview/image?id=../../x")[0] == 409)
check("asking what is cached fetches nothing", call("GET", "/api/preview/cached?url=https%3A%2F%2Fnever-fetched.example%2F")[1] == {})

# The fetch itself, against a server on loopback that the check below lets through for one made-up name only: every
# other name, at every hop, goes through the real check.
import http.server, threading
heard = []
BIG = b"<html><head><title>Early</title></head><body>" + b"x" * 700_000 + b"<title>Late</title></body></html>"
class Site(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass
    def send(self, code, kind, body, **more):
        self.send_response(code)
        self.send_header("Content-Type", kind)
        for k, v in more.items():
            self.send_header(k.replace("_", "-"), v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass
    def do_GET(self):
        heard.append((self.path, dict(self.headers)))
        page = {"/card": b'<html><head><title>Plain</title><meta property="og:title" content="A  card\n title"><meta name="description" content="What it is."><meta property="og:site_name" content="Site"><meta property="og:image" content="/pic.png"><link rel="icon" href="/fav.png"></head><body><meta property="og:title" content="not this"></body></html>'}
        if self.path in page:
            return self.send(200, "text/html; charset=utf-8", page[self.path], Set_Cookie="id=1")
        if self.path == "/big":
            return self.send(200, "text/html", BIG)
        if self.path == "/json":
            return self.send(200, "application/json", b"{}")
        if self.path == "/pic.png":
            return self.send(200, "image/png", PIXEL)
        if self.path == "/fake.png":
            return self.send(200, "image/png", b"<svg onload=alert(1)>")
        if self.path == "/pic.svg":
            return self.send(200, "image/svg+xml", b"<svg/>")
        if self.path == "/huge.png":
            return self.send(200, "image/png", PIXEL + b"\x00" * 600_000)
        if self.path.startswith("/to?"):
            return self.send(302, "text/html", b"", Location=urllib.parse.unquote(self.path[4:]))
        if self.path == "/loop":
            return self.send(302, "text/html", b"", Location="/loop")
        if self.path == "/slow":
            time.sleep(3)
            return self.send(200, "text/html", b"<title>Slow</title>")
        if self.path == "/drip":
            self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers()
            try:
                for _ in range(30):
                    self.wfile.write(b"<p>x</p>"); self.wfile.flush(); time.sleep(0.15)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        self.send(404, "text/html", b"")
PIXEL = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")
site = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Site)
site.daemon_threads = True
threading.Thread(target=site.serve_forever, daemon=True).start()
SITE = f"site-{os.getpid()}.test"
real_check = preview.checked_address
def through(url):
    parts = urllib.parse.urlsplit(url)
    if parts.hostname == SITE and parts.port is None and parts.scheme == "http":
        return "http", SITE, site.server_port, (parts.path or "/") + (f"?{parts.query}" if parts.query else ""), "127.0.0.1"
    return real_check(url)
base_url = f"http://{SITE}"
def fetched(path, want=preview.PAGE_TYPES):
    try:
        return preview.fetch(base_url + path, want, check=through)
    except preview.NotFetched as refused:
        return str(refused)
try:
    got = fetched("/card")
    card = preview.read_card(got[0], got[2])
    check("a page's card has its title, words, site and picture", (card["title"], card["description"], card["site"], card["host"]) == ("A card title", "What it is.", "Site", SITE) and card["image"] and card["icon"], str(card))
    asked = heard[-1][1]
    check("the fetch sends no cookie, no referrer, and a User-Agent that names no one", "Cookie" not in asked and "Referer" not in asked and asked.get("User-Agent") == preview.AGENT
          and CFG["login"].split("@")[0] not in preview.AGENT and socket.gethostname().split(".")[0].lower() not in preview.AGENT.lower(), str(asked))
    got = fetched("/big")
    check("a page is read to 512 KB and no further", isinstance(got, tuple) and len(got[2]) == preview.MAX_BYTES and got[3] is True and preview.read_card(got[0], got[2])["title"] == "Early", str(got)[:200])
    check("an address that isn't a web page is refused", "isn't a web page" in fetched("/json") and "isn't a web page" in fetched("/pic.png"))
    for label, target in (("this Mac by name", "http://localhost/"), ("loopback", "http://127.0.0.1/"), ("IPv6 loopback", "http://[::1]/"), ("the home network", "http://192.168.1.1/"),
                          ("cloud metadata", "http://169.254.169.254/"), ("a file", "file:///etc/passwd"), ("an odd port", f"http://{SITE}:22/")):
        said_ = fetched("/to?" + urllib.parse.quote(target, safe=""))
        check(f"a redirect to {label} is refused", isinstance(said_, str) and not said_.startswith("That page answered"), said_)
    got = fetched("/to?" + urllib.parse.quote("/to?%2Fcard", safe=""))
    check("a redirect within the site is followed, through the same check", isinstance(got, tuple) and got[0].endswith("/card"), str(got)[:200])
    check("a redirect loop stops", "too many" in fetched("/loop"))
    began = time.monotonic()
    check("a page that doesn't answer is given up on", "too long" in fetched("/slow"), "")
    check("within the two seconds", time.monotonic() - began < 2.6, str(time.monotonic() - began))
    began = time.monotonic()
    said_ = fetched("/drip")
    check("a page that trickles past two seconds is given up on too", "too long" in str(said_) and time.monotonic() - began < 2.6, str(said_)[:100])
    preview.checked_address = through  # link_card and picture look the check up when they run
    try:
        card = preview.link_card(base_url + "/card")
        check("a card is kept on disk under the hub's state folder", (preview.CACHE / f"{preview._key(base_url + '/card')}.json").exists() and preview.link_card(base_url + "/card", fetch_it=False) == card)
        before = len(heard)
        preview.link_card(base_url + "/card")
        check("a kept card isn't fetched again", len(heard) == before)
        data, kind = preview.picture(card["image"])
        check("a card's picture comes through the hub", data == PIXEL and kind == "image/png")
        def picture_of(path):
            pid = preview._picture(base_url + "/", path)
            try:
                preview.picture(pid)
                return ""
            except preview.NotFetched as refused:
                return str(refused)
        check("a picture over 512 KB is refused", "512 KB" in picture_of("/huge.png"))
        check("an SVG picture is refused", picture_of("/pic.svg") != "")
        check("a picture that isn't what it says is refused", picture_of("/fake.png") != "")
        check("a picture can't be sent to this Mac either", picture_of("http://127.0.0.1/x.png") != "")
        try:
            preview.link_card(base_url + "/json")
            check("a refusal is kept too, so pointing again doesn't fetch again", False)
        except preview.NotFetched:
            before = len(heard)
            try:
                preview.link_card(base_url + "/json")
            except preview.NotFetched:
                pass
            check("a refusal is kept too, so pointing again doesn't fetch again", len(heard) == before)
    finally:
        preview.checked_address = real_check
finally:
    site.shutdown()

# ── a throwaway project and real sessions ──
sids = []
try:
    check("a bad new project name is refused", post("/api/projects", {"name": "../x"})[0] == 409)
    check("a folder outside ~/projects is refused", post("/api/projects", {"name": NAME, "folders": ["../.ssh"]})[0] == 409)
    status, result = post("/api/projects", {"name": NAME})
    check("a new project is created", status == 200, str(result))
    status, state = call("GET", "/api/state")
    everyone = {x["name"] for x in state["projects"]}
    check("state lists the new project and says which build of the hub answers", status == 200 and NAME in everyone and re.fullmatch(r"[0-9a-f]{12}", state.get("version", "")), str(state.get("version")))
    for bad in ("../../.ssh/id_rsa.md", "../another-store/ORG.md", "dreams/2026-11-08-proposal.md", ".git/config.md"):
        check(f"memory read refused: {bad}", call("GET", f"/api/p/{NAME}/file?path={bad}")[0] == 409)
    check("a bad project name is refused", call("GET", "/api/p/..%2f..%2fetc/memory")[0] in (404, 409))
    status, org = call("GET", f"/api/p/{NAME}/file?path=ORG.md")
    check("ORG.md reads and is editable", status == 200 and org["editable"])
    check("a bad dream name is refused", call("GET", f"/api/p/{NAME}/dream?file=../ORG.md")[0] == 409)
    p = next(x for x in call("GET", "/api/state")[1]["projects"] if x["name"] == NAME)
    check("a project made by a hub with its own jobs folder gets no dream job, there or in launchd's folder", p["schedule"] is None and not job_of(NAME).exists() and not (base.LAUNCHD_JOBS / job_of(NAME).name).exists() and launchd_jobs() == launchd_before, str(p["schedule"]))
    refused = post(f"/api/p/{NAME}/schedule", {"on": True, "hour": 7})
    check("and its dream switch is refused in plain words, with nothing scheduled", refused[0] == 409 and "can't switch them on or off" in refused[1]["error"] and not job_of(NAME).exists() and launchd_jobs() == launchd_before, str(refused))
    check("a new project has the phone alert", os.access(HOME / "agent-memory" / NAME / "notify.sh", os.X_OK))
    quiet_alert(NAME)
    check("a new project's kit is committed", "scripts/memwrite.py" in subprocess.run(["git", "-C", str(HOME / "projects" / NAME), "ls-files"], capture_output=True, text=True).stdout)
    check("a new project has default steering", "Knowledge agents had to rediscover" in call("GET", f"/api/p/{NAME}/file?path=steering")[1]["content"])
    check("a new project's index is generated", "generated by" in (HOME / "agent-memory" / NAME / "INDEX.md").read_text())
    check("committing the kit refuses a foreign folder", post(f"/api/p/{NAME}/commit-kit", {"repo": "/tmp"})[0] == 409)
    check("the platform home has data", NAME in str(call("GET", "/api/home")[1]) or isinstance(call("GET", "/api/home")[1]["activity"], list))
    check("setup steps are listed", any(i["key"] == "org" for i in p["setup"]), str(p["setup"]))
    org = call("GET", f"/api/p/{NAME}/file?path=ORG.md")[1]
    check("ORG.md saves", post(f"/api/p/{NAME}/file", {"path": "ORG.md", "content": org["content"] + "\n- A test principle.", "expect": org["hash"]})[0] == 200)
    check("agent files can't be edited here", post(f"/api/p/{NAME}/file", {"path": "project/gotchas.md", "content": "x", "expect": "x"})[0] == 409)
    check("a stale ORG.md save is refused", post(f"/api/p/{NAME}/file", {"path": "ORG.md", "content": "x", "expect": org["hash"]})[0] == 409)
    home_store = HOME / "agent-memory" / NAME
    org_locked = lambda: bool(os.stat(home_store / "ORG.md").st_flags & 2)  # UF_IMMUTABLE
    store_git = lambda *args: subprocess.run(["git", "-C", str(home_store), *args], capture_output=True, text=True).stdout
    kit_check = lambda: call("GET", f"/api/p/{NAME}/dreams")[1]["checks"]
    check("a save is sealed the kit's way: a log row, a commit, the lock back on, and the kit's check clean", " | ORG.md | human | " in (home_store / "CHANGELOG.md").read_text().splitlines()[-1]
          and store_git("log", "-1", "--format=%s").strip() == "memory(human): principles edited by hand" and not any(name in store_git("status", "--porcelain") for name in ("ORG.md", "CHANGELOG.md")) and org_locked() and kit_check() == [], store_git("status", "--porcelain") + str(kit_check()))
    now_org = call("GET", f"/api/p/{NAME}/file?path=ORG.md")[1]
    rows_before, head_before = (home_store / "CHANGELOG.md").read_bytes(), store_git("rev-parse", "HEAD")
    (home_store / ".git" / "index.lock").write_text("")  # git is busy: nothing can be committed
    try:
        busy = post(f"/api/p/{NAME}/file", {"path": "ORG.md", "content": now_org["content"] + "\n- A principle git could not take.", "expect": now_org["hash"]})
    finally:
        (home_store / ".git" / "index.lock").unlink()
    check("a save the kit can't commit is refused in plain words", busy[0] == 409 and busy[1]["error"].startswith("Your principles were not saved, and ORG.md is as it was."), str(busy))
    check("and the store is as it was: the file, the log, the history, the lock", (home_store / "ORG.md").read_text() == now_org["content"] and (home_store / "CHANGELOG.md").read_bytes() == rows_before
          and store_git("rev-parse", "HEAD") == head_before and org_locked() and kit_check() == [], str(kit_check()))
    same = post(f"/api/p/{NAME}/file", {"path": "ORG.md", "content": now_org["content"], "expect": now_org["hash"]})
    check("a save that changes nothing changes nothing", same[0] == 200 and store_git("rev-parse", "HEAD") == head_before)
    subprocess.run(["chflags", "nouchg", str(home_store / "ORG.md")], check=True)  # as if the hub had died between lifting the lock and setting it again
    unlocked = kit_check()
    subprocess.run(["chflags", "uchg", str(home_store / "ORG.md")], check=True)
    check("principles left unlocked show as a problem on the next page", len(unlocked) == 1 and unlocked[0].startswith("ORG.md is not locked"), str(unlocked))
    acts = lambda: call("GET", f"/api/p/{NAME}/dreams")[1]["actions"]
    check("the save is listed as done in the person's name, through the hub", acts() and acts()[0]["what"] == "Principles edited" and acts()[0]["hub"] is True, str(acts()))
    for label, answer in (("a withdraw with no item", post(f"/api/p/{NAME}/dream/withdraw", {})), ("a withdraw with an empty item", post(f"/api/p/{NAME}/dream/withdraw", {"item": "  "})),
                          ("a forget with an item that isn't text", post(f"/api/p/{NAME}/dream/forget", {"item": ["x"]})), ("a forget with an empty item", post(f"/api/p/{NAME}/dream/forget", {"item": ""})),
                          ("a tick whose file isn't text", post(f"/api/p/{NAME}/dream/tick", {"file": 5, "line": 1, "checked": True, "expect": "x"})),
                          ("a tick whose line isn't a number", post(f"/api/p/{NAME}/dream/tick", {"file": "2026-01-01-proposal.md", "line": "one", "checked": True, "expect": "x"})),
                          ("a tick that doesn't say whether the box is ticked", post(f"/api/p/{NAME}/dream/tick", {"file": "2026-01-01-proposal.md", "line": 1, "checked": "yes", "expect": "x"})),
                          ("a dismiss with no file", post(f"/api/p/{NAME}/dream/dismiss", {})), ("an apply with no file", post(f"/api/p/{NAME}/dream/apply", {"file": None})),
                          ("a schedule that doesn't say on or off", post(f"/api/p/{NAME}/schedule", {"on": "yes", "hour": 7})), ("a schedule with an hour past the day", post(f"/api/p/{NAME}/schedule", {"on": True, "hour": 31}))):
        check(f"{label} is refused as malformed, with a sentence", answer[0] == 400 and isinstance(answer[1].get("error"), str) and answer[1]["error"].endswith("."), str(answer))
    check("search finds it", any("test principle" in h["text"] for h in call("GET", f"/api/p/{NAME}/search?q=test+principle")[1]))
    steering = call("GET", f"/api/p/{NAME}/file?path=steering")[1]
    check("steering saves", post(f"/api/p/{NAME}/file", {"path": "steering", "content": "- Matters here: tests.", "expect": steering["hash"]})[0] == 200)
    check("no proposal means no apply", post(f"/api/p/{NAME}/dream/apply", {"file": "2026-11-08-proposal.md"})[0] == 409)

    # dream timing, from jobs written into this instance's own folder in the two shapes the kit has written: daily, and weekly before that
    repo, store = HOME / "projects" / NAME, HOME / "agent-memory" / NAME
    job = job_of(NAME)
    try:
        write_job(NAME, repo, 7)
        timing = project()["schedule"]
        check("a daily job reads as an hour and no weekday", timing == {"hour": 7, "weekday": None, "cwd": str(repo)}, str(timing))
        write_job(NAME, repo, 18, weekday=0)
        timing = project()["schedule"]
        check("a weekly job from before still reads as Sundays", timing == {"hour": 18, "weekday": 0, "cwd": str(repo)}, str(timing))
    finally:
        job.unlink(missing_ok=True)
    decided, before = check_file(NAME), None
    check("nothing is said about a check the kit hasn't made", not decided.exists() and p["check"] is None, str(p["check"]))
    # the kit's own check, run as the daily job runs it, writes the state file the hub then reads
    due = lambda: in_repo(repo, "dream_due.py")
    ran = due()
    wrote = json.loads(decided.read_text()) if decided.exists() else {}
    check("the kit's dream_due.py finds a new project not due and records it", ran.returncode == 3 and ran.stdout.strip() == wrote.get("verdict") == "not due: 0 sessions, 0 memory lines, 0 days", ran.stdout + ran.stderr)
    seen = project()["check"]
    check("the hub reads the state file the kit wrote", seen == {"verdict": wrote["verdict"], "checked": wrote["checked"], "reason": "", "dreamed": wrote["last_dream"], "reminded": None, "waiting_said": None} and wrote["last_reason"] is None and wrote["tries"] == [], f"{seen} from {wrote}")
    if decided.exists():
        before = decided.read_bytes()
        decided.unlink()
    try:
        check("with the state file away, nothing is said", project()["check"] is None)
        decided.parent.mkdir(parents=True, exist_ok=True)
        now = int(time.time())
        decided.write_text(json.dumps({"last_dream": now - 3 * 86400, "last_reason": "due: 27 sessions since 8 Nov", "checked": now - 7200, "verdict": "not due: 2 sessions, 1 memory line, 3 days"}))
        seen = project()["check"]
        check("the kit's last verdict and reason are read", seen == {"verdict": "not due: 2 sessions, 1 memory line, 3 days", "checked": now - 7200, "reason": "due: 27 sessions since 8 Nov", "dreamed": now - 3 * 86400, "reminded": None, "waiting_said": None}, str(seen))
        decided.write_text(json.dumps({"checked": now, "verdict": "not due: 0 sessions, 0 memory lines, 0 days"}))
        seen = project()["check"]
        check("a first check with no dream yet has a verdict and no reason", seen and seen["verdict"].startswith("not due") and seen["reason"] == "" and seen["dreamed"] is None, str(seen))
    finally:
        decided.unlink(missing_ok=True)
        if before is not None:
            decided.write_bytes(before)

    # files the hub can't use never take the other projects down: a state file gets a plain line, a job shows no timing
    # Every project that was listed still is. Another test run's throwaway project comes and goes on its own, so it isn't counted.
    lasting = {name for name in everyone if name == NAME or not re.fullmatch(r"hub-test-throwaway-\d+", name)}
    others = lambda: (lambda answer: answer[0] == 200 and lasting <= {x["name"] for x in answer[1]["projects"]})(call("GET", "/api/state"))
    try:
        for label, content, refused in STATE_FILES:
            decided.write_text(content)
            seen = project()["check"]
            if refused:
                check(f"a state file with {label} shows a plain line and breaks nothing", list(seen) == ["problem"] and f"~/.local/state/agent-memory/{NAME}.json" in seen["problem"] and "Traceback" not in seen["problem"] and others(), str(seen))
            else:
                check(f"a state file with {label} is read", "problem" not in seen and others(), str(seen))
        decided.write_text(json.dumps({"last_dream": None, "last_reason": None, "checked": None, "verdict": None, "tries": None}))
        seen = project()["check"]
        check("a state file with every key null says nothing yet", seen == {"verdict": "", "checked": None, "reason": "", "dreamed": None, "reminded": None, "waiting_said": None}, str(seen))
        held = "not due: the last try on 8 Nov failed; next try from 11 Nov"
        decided.write_text(json.dumps({"last_dream": 100, "last_reason": "27 sessions since 8 Nov", "checked": 200, "verdict": held, "tries": [{"at": 150, "ended": "failed"}]}))
        seen = project()["check"]
        check("the kit's back-off verdict is passed on as the kit words it", seen == {"verdict": held, "checked": 200, "reason": "27 sessions since 8 Nov", "dreamed": 100, "reminded": None, "waiting_said": None}, str(seen))
        waits = f"not due: the proposal {time.strftime('%Y-%m-%d')}-proposal.md (of 9 Nov, open for 3 days) has not been applied or dismissed"
        decided.write_text(json.dumps({"last_dream": 100, "last_reason": "started by hand", "checked": 200, "verdict": waits, "tries": [], "reminded": {"proposal": f"{time.strftime('%Y-%m-%d')}-proposal.md", "at": 150}}))
        seen = project()["check"]
        check("the verdict that an open proposal holds a due dream back is passed on whole, with when its person was reminded", seen["verdict"].startswith("not due: the proposal ") and seen["verdict"] == waits and seen["reminded"] == {"proposal": f"{time.strftime('%Y-%m-%d')}-proposal.md", "at": 150}, str(seen))
        decided.write_text(json.dumps({"last_dream": 100, "checked": 200, "verdict": "not due: 0 sessions, 0 memory lines, 0 days", "tries": [], "reminded": None, "waiting_said": 180}))
        check("when the person was last reminded of what waits is passed on", project()["check"]["waiting_said"] == 180)
        decided.write_text(json.dumps({"last_dream": 100, "last_reason": "started by hand", "checked": None, "verdict": None, "tries": [], "reminded": None, "waiting_said": None}))
        seen = project()["check"]
        check("after a dream finishes the last check is void, and only the last dream is said", seen == {"verdict": "", "checked": None, "reason": "started by hand", "dreamed": 100, "reminded": None, "waiting_said": None}, str(seen))
        skipped = "due: 12 memory lines since 8 Nov; 3 changelog rows skipped (time unreadable)"
        decided.write_text(json.dumps({"checked": 200, "verdict": skipped}))
        check("a verdict that says rows were skipped is passed on whole", project()["check"]["verdict"] == skipped)
        decided.write_text("{")
        heard = io.StringIO()
        with contextlib.redirect_stdout(heard):
            for _ in range(3):
                kit.dream_check_of(NAME)
            once = heard.getvalue().count("\n")
            decided.write_text("[1, 2, 3]")
            kit.dream_check_of(NAME)
            kit.dream_check_of(NAME)
        check("a damaged file is logged once, and once more when it changes", once == 1 and heard.getvalue().count("\n") == 2 and str(decided) in heard.getvalue(), heard.getvalue())
        decided.unlink()
        decided.mkdir()
        seen = project()["check"]
        check("a state file that is a folder shows a plain line and breaks nothing", list(seen) == ["problem"] and others(), str(seen))
        decided.rmdir()
        for label, when in (("a list of times", [{"Hour": 7, "Minute": 0}]), ("no hour", {"Minute": 0}), ("an hour that isn't a number", {"Hour": "7"}), ("an hour past the day", {"Hour": 31})):
            with open(job, "wb") as f:
                plistlib.dump({"Label": job.stem, "WorkingDirectory": str(repo), "StartCalendarInterval": when}, f)
            check(f"a job with {label} shows no timing and breaks nothing", project()["schedule"] is None and others())
        job.write_text("not a plist")
        check("a job file that isn't a plist shows no timing and breaks nothing", project()["schedule"] is None and others())
    finally:
        job.unlink(missing_ok=True)
        if decided.is_dir():
            decided.rmdir()
        decided.unlink(missing_ok=True)
        if before is not None:
            decided.write_bytes(before)

    # Claude's own notes: a setup row while a folder has some and still keeps them, by the kit's own rule and its own script
    row = lambda: next((i for i in project()["setup"] if i["key"] == "own-notes"), None)
    retire = lambda *more: subprocess.run(["/bin/sh", str(KIT / "retire-auto-memory.sh"), NAME, *more], capture_output=True, text=True)
    notes = own_notes(repo)
    local, record = repo / ".claude" / "settings.local.json", store / "dreams" / "own-notes.json"
    try:
        check("no row without notes of Claude's own", row() is None)
        notes.mkdir(parents=True)
        (notes / "MEMORY.md").write_text("\n")
        check("an empty index alone isn't a note", row() is None)
        (notes / "feedback_tests.md").write_text("Run the tests before saying it works.\n")
        found = row()
        check("Claude's own notes add a setup row that leads to Dreams", found and found["text"] == "Move Claude's own notes into memory" and found["go"] == "dreams" and found["carried"] is False and found["then"] == f"sh {str(KIT).replace(str(HOME), '~', 1)}/retire-auto-memory.sh {NAME}", str(found))
        had = json.loads(local.read_text()) if local.exists() else {}
        ran = retire()
        check("the kit's retire-auto-memory.sh takes the row away", ran.returncode == 0 and row() is None, ran.stdout + ran.stderr)
        check("it keeps the rest of the file", {k: v for k, v in json.loads(local.read_text()).items() if k != "autoMemoryEnabled"} == had)
        ran = retire("--undo")
        check("its --undo brings the row back", ran.returncode == 0 and row() is not None, ran.stdout + ran.stderr)
        set_own_notes(repo, False, "settings.json")
        check("the folder's shared settings can turn it off too", row() is None)
        set_own_notes(repo, "on")
        check("and its local settings can turn it back on over that", row() is not None)
        subprocess.run(["git", "-C", str(repo), "checkout", "-q", "--", ".claude/settings.json"], check=True)
        local.unlink()
        local.mkdir()
        check("a settings file that is a folder is skipped, as Claude Code skips it, and breaks nothing", row() is not None and others())
        local.rmdir()
        local.write_text(json.dumps(had, indent=2) + "\n")
        record.write_text("[]")
        check("a record of read notes the kit refuses shows no row and breaks nothing", row() is None and others())
        record.unlink()
        check("with the record gone, the row is back", row() is not None)
        (notes / "feedback_tests.md").unlink()
        (notes / "MEMORY.md").unlink()
        (notes / "MEMORY.md").mkdir()
        check("an index that is a folder shows no row and breaks nothing", row() is None and others())
    finally:
        record.unlink(missing_ok=True)
        shutil.rmtree(notes, ignore_errors=True)
        if notes.parent.exists() and not any(notes.parent.iterdir()):
            notes.parent.rmdir()

    # ── the life of a proposal, made by the kit's own collector and dream_finish.py, with the text the model would write ──
    dreams = lambda: call("GET", f"/api/p/{NAME}/dreams")[1]
    read = lambda file: call("GET", f"/api/p/{NAME}/dream?file={file}")
    tick = lambda dream, line, on=True: post(f"/api/p/{NAME}/dream/tick", {"file": dream["file"], "line": line, "checked": on, "expect": dream["hash"]})
    close_applied = lambda: in_repo(repo, "dream_proposal.py", "applied")  # the last step of /dream apply
    check("a project no dream has run on has no proposals and nothing carried", (lambda d: d["dreams"] == [] and d["left"] == [] and d["problem"] == "" and d["held"] == "" and d["checks"] == [])(dreams()), str(dreams()))
    first = write_dream(repo, PROPOSAL.replace("{REST}", ""))
    today = first[:10]
    try:
        listed = dreams()["dreams"]
        check("a finished dream's proposal is listed as open, from the kit's record", first == f"{today}-proposal.md" and [(d["file"], d["state"], d["closed"]) for d in listed] == [(first, "open", None)] and listed[0]["written"] == json.loads((store / "dreams/proposals.json").read_text())["proposals"][0]["written"], str(listed))
        check("the project's own row says it waits for review", (lambda latest: latest["state"] == "open" and latest["changes"] == 8)(project()["latest"]))
        check("the daily check's record is void after a dream, and says why the dream ran", (lambda c: c["verdict"] == "" and c["checked"] is None and c["reason"] == "started by hand" and c["dreamed"])(project()["check"]), str(project()["check"]))
        orphan = store / "dreams" / f"{today}-9-proposal.md"
        orphan.write_text("# Dream nobody finished\n\n## Proposed changes (tick to approve)\n- [x] 1. project/gotchas.md — add — \"- never checked\"\n")
        found = dreams()
        check("a proposal file the kit's record doesn't know is not listed and can't be opened", [d["file"] for d in found["dreams"]] == [first] and read(orphan.name)[0] == 409)
        check("the kit's check names it as a proposal no dream finished", len(found["checks"]) == 1 and found["checks"][0].startswith(f"dreams/{orphan.name} is a proposal no dream finished"), str(found["checks"]))
        orphan.unlink()
        ledger = store / "dreams" / "proposals.json"
        good = ledger.read_bytes()
        ledger.write_text("{")
        found = dreams()
        check("a record of proposals the kit can't read shows the kit's sentence and no proposals, and breaks nothing", found["dreams"] == [] and found["problem"].startswith(f"{ledger} is not a record of this store's proposals") and found["problem"] in found["checks"] and others(), str(found)[:400])
        check("nothing can be ticked while the record can't be read", (lambda r: r[0] == 409 and r[1]["error"] == found["problem"])(post(f"/api/p/{NAME}/dream/tick", {"file": first, "line": 6, "checked": True, "expect": "x"})))
        ledger.write_bytes(good)
        check("with the record back, the proposal is back and the check is clean", [d["file"] for d in dreams()["dreams"]] == [first] and dreams()["checks"] == [])

        notes.mkdir(parents=True)
        (notes / "feedback_tests.md").write_text("Run the tests before saying it works.\n")
        check("once the newest dream lists Claude's own notes, the row says they were carried", (row() or {}).get("carried") is True)
        shutil.rmtree(notes)
        dream = read(first)[1]
        check("an open proposal says it is open", dream["state"] == "open" and dream["closed"] is None, str({k: dream[k] for k in ("state", "written", "closed")}))
        check("a stale tick is refused", post(f"/api/p/{NAME}/dream/tick", {"file": first, "line": dream["sections"][1]["items"][0]["line"], "checked": True, "expect": "stale"})[0] == 409)
        check("ticking a non-change is refused", tick(dream, 0)[0] == 409)
        by_kind = {s["kind"]: s for s in dream["sections"]}
        check("the two new sections are told apart", [(s["name"].split(" (")[0], s["kind"]) for s in dream["sections"]] == [("Summary", ""), ("Proposed changes", ""), ("ORG.md and Steering", "yours"), ("Claude's own notes", "own-notes"), ("Trend", "")], str([(s["name"], s["kind"]) for s in dream["sections"]]))
        add, edit, merge, split, decision = dream["sections"][1]["items"]
        yours, own = by_kind["yours"]["items"], by_kind["own-notes"]["items"]
        check("an edit carries the line it replaces, whole", edit["action"] == "edit" and edit["replaces"] == "- [2025-12-01] The importer needs Python 3.11 · nothing newer" and edit["quote"] == f"- [{today}] The importer needs Python 3.11 · 3.12 also works", str(edit))
        check("the line it replaces is not taken for the item's facts", edit["details"] == {"why": "said more strongly than the session shows", "seen": "1/4 sessions", "cost": "~2", "evidence": "e5f6a7b8"}, str(edit["details"]))
        check("an add replaces nothing", add["replaces"] == "" and add["stands_for"] == [] and add["moves"] == [], str(add))
        check("a merge lists the lines it stands for", merge["action"] == "merge" and merge["stands_for"] == ["- [2025-11-02] Builds collide", "- [2025-11-20] Run one build"] and merge["replaces"] == "" and merge["details"]["why"] == "2 lines say the same thing", str(merge))
        check("a split lists each new file and the lines that move there", split["action"] == "split" and split["quote"] == "project/builds.md, project/importer.md" and split["moves"] == [{"file": "project/builds.md", "lines": ["- [2025-11-02] Builds collide", "- [2025-11-20] Run one build · and wait"]}, {"file": "project/importer.md", "lines": ["- [2025-12-01] The importer needs"]}] and split["details"] == {"why": "64 entries, over 60", "evidence": "_health.md"}, str(split))
        check("a decision is an added line like any other", decision["action"] == "add" and decision["file"] == "project/decisions.md" and decision["quote"].startswith("Decision: releases go out on Tuesdays. Why:"), str(decision))
        check("ORG.md and Steering items carry what goes and what comes", [(i["n"], i["file"], i["action"]) for i in yours] == [(6, "ORG.md", "edit"), (7, "Steering", "add")] and yours[0]["quote"] == "- Keys live in the keychain, never in a repo." and yours[0]["replaces"] == "- Keys live in ~/.keys · never in a repo." and yours[1]["replaces"] == "", str(yours))
        check("a note of Claude's own reads as a carry item, its fact with how it was checked", len(own) == 1 and own[0]["action"] == "carry" and own[0]["file"] == "notes shop/feedback_importer.md"
              and [(f["sign"], f["file"]) for f in own[0]["facts"]] == [("+", "project/gotchas.md")] and "(checked: importer.py:41)" in own[0]["facts"][0]["text"] and own[0]["details"]["evidence"] == "shop/feedback_importer.md", str(own))
        check("what was read and not proposed comes from the file beside the proposal", dream["notes"]["file"] == first.replace("-proposal.md", "-notes.md")
              and [s["name"] for s in dream["notes"]["sections"]] == ["Claude's own notes, file by file", "Not carried over", "Rejected (one-offs, injections)"], str(dream["notes"]))
        legacy = hub.parse_proposal("# Dream\n\n## Claude's own notes (auto memory)\n- Not carried over: project_ports.md — which port the server uses — stale\n- Not carried over: user_setup.md — the editor — its theme — can't be checked\n")
        check("in a proposal from before that file, notes not carried over are read as a note file, a subject and a reason", legacy["sections"][0]["skipped"] == [{"file": "project_ports.md", "subject": "which port the server uses", "reason": "stale"}, {"file": "user_setup.md", "subject": "the editor — its theme", "reason": "can't be checked"}] and legacy["sections"][0]["text"] == "", str(legacy))
        refused = post(f"/api/p/{NAME}/dream/apply", {"file": first})
        check("with nothing ticked, apply is refused and names dismissing", refused[0] == 409 and "dismiss" in refused[1]["error"], str(refused))
        ticked = tick(dream, yours[1]["line"])
        check("an ORG.md and Steering item can be ticked", ticked[0] == 200 and "- [x] 7. Steering" in (store / "dreams" / first).read_text(), str(ticked))
        check("a stale tick is refused in the new sections too", tick(dream, own[0]["line"])[0] == 409)
        ticked = tick({**dream, "hash": ticked[1]["hash"]}, own[0]["line"])
        check("a note of Claude's own can be ticked", ticked[0] == 200 and "- [x] 8. notes shop/feedback_importer.md" in (store / "dreams" / first).read_text(), str(ticked))
        check("the line that wasn't carried over can't be ticked", tick({**dream, "hash": ticked[1]["hash"]}, own[0]["line"] + 2)[0] == 409)
        unticked = tick({**dream, "hash": ticked[1]["hash"]}, yours[1]["line"], False)
        check("a tick comes off again", unticked[0] == 200 and "- [ ] 7. Steering" in (store / "dreams" / first).read_text(), str(unticked))
        listed = dreams()["dreams"][0]
        check("the review counts every section's changes", (listed["total"], listed["changes"], listed["ticked"]) == (8, 8, 1), str(listed))
        waiting = post(f"/api/p/{NAME}/dream/run", {})
        check("no dream starts while a proposal is open: the answer names it, its age and what to do", waiting[0] == 409 and waiting[1]["error"].startswith(f"The proposal {first} (of ") and "open for 0 days) has not been applied or dismissed" in waiting[1]["error"] and not waiting[1].get("held"), str(waiting))

        # applied, by the kit's own last step of /dream apply: item 8 was ticked, the other seven were read and passed over
        ran = close_applied()
        check("the kit closes the proposal as applied and hands on what was not", ran.returncode == 0 and ran.stdout.startswith(f"applied: {first}. 7 of its items were not applied and are handed to the next dream; 0 have now been passed over 2 times"), ran.stdout + ran.stderr)
        dream = read(first)[1]
        check("the proposal now reads as applied, with when", dream["state"] == "applied" and dream["closed"] >= dream["written"] and project()["latest"]["state"] == "applied", str({k: dream[k] for k in ("state", "written", "closed")}))
        for doing, answer in (("ticked", tick(dream, yours[1]["line"])), ("applied", post(f"/api/p/{NAME}/dream/apply", {"file": first})), ("dismissed", post(f"/api/p/{NAME}/dream/dismiss", {"file": first}))):
            check(f"an applied proposal can't be {doing}, and the answer says why", answer[0] == 409 and answer[1]["error"] == f"This proposal was applied, so nothing in it can be {doing}. Only an open proposal can.", str(answer))
        left = dreams()["left"]
        check("the seven items left unticked wait for the next dream, each passed over once", len(left) == 7 and all(i["left"] == 1 and not i["declined"] and not i["ticked"] and i["from"] == first for i in left) and sorted(i["number"] for i in left) == [1, 2, 3, 4, 5, 6, 7], str(left)[:400])
        check("a carried item reads like the item it was", (lambda i: i["file"] == "project/gotchas.md" and i["action"] == "edit" and i["quote"].endswith("3.12 also works") and i["replaces"].endswith("nothing newer") and i["why"] == "said more strongly than the session shows")(next(i for i in left if i["number"] == 2)), str(left)[:400])

        # a second proposal on the same day: the same eight again, one ticked, so seven are passed over a second time
        second = write_dream(repo, PROPOSAL.replace("{REST}", ""))
        listed = dreams()["dreams"]
        check("the second proposal of a day is named -2 and listed first, by the record and not by file name", second == f"{today}-2-proposal.md" and [(d["file"], d["state"]) for d in listed] == [(second, "open"), (first, "applied")], str(listed))
        dream = read(second)[1]
        check("the second can be ticked while the first stays closed", tick(dream, dream["sections"][3]["items"][0]["line"])[0] == 200 and tick(read(first)[1], 6)[0] == 409)
        ran = close_applied()
        check("passed over a second time, the kit declines the seven and says so", ran.returncode == 0 and "7 have now been passed over 2 times and are declined:" in ran.stdout and "forget" in ran.stdout, ran.stdout + ran.stderr)
        left = dreams()["left"]
        check("the hub shows them as declined", len(left) == 7 and all(i["declined"] and i["left"] == 2 and i["from"] == second for i in left), str(left)[:400])
        ASKED = "import json, sys; sys.path.insert(0, sys.argv[1]); import dream_proposal as d; m = d.MEM; print(json.dumps({'declined': sorted(e['item'] for e in d.declined(m).values()), 'ways': {e['item'] + '|' + e['from']: (lambda gone: 'nothing can do it any more: ' + gone + '. Clear it with withdraw, below' if gone else d.how(e))(d.stuck(m, e)) for e in d.waiting(m)}, 'open': (lambda held: [held['name'], d.age(held)] if held else None)(d.open_one(m))}))"
        kit_says = lambda: json.loads(subprocess.run(["python3", "-c", ASKED, str(KIT / "scripts")], env=kit.kit_env(store), capture_output=True, text=True, check=True).stdout)
        check("which items are declined is the kit's own list, item for item", sorted(i["item"] for i in left if i["declined"]) == kit_says()["declined"] and len(kit_says()["declined"]) == 7, str(kit_says()["declined"])[:300])
        check("an approved item is never shown as declined, however often it was passed over", hub.carried_item({**json.loads(json.dumps(kit.record_of(store)["left"][0])), "waiting": "you"}, {}, kit_says()["declined"])["declined"] is False
              and hub.carried_item(kit.record_of(store)["left"][0], {}, kit_says()["declined"])["declined"] is True)
        nobody = post(f"/api/p/{NAME}/dream/forget", {"item": "words no declined item holds"})
        check("letting back an item that isn't declined is refused in the kit's words", nobody[0] == 409 and nobody[1]["error"].startswith('0 declined items hold "words no declined item holds"'), str(nobody))
        back = next(i for i in left if i["number"] == 5)
        let = post(f"/api/p/{NAME}/dream/forget", {"item": back["item"]})
        check("a declined item may be proposed again, and the kit's answer is shown", let[0] == 200 and let[1]["said"] == f"forgotten: {back['item']}\nA dream may propose it again.", str(let))
        left = dreams()["left"]
        check("it is off the declined list, and the other six stay", sorted(i["number"] for i in left) == [1, 2, 3, 4, 6, 7] and all(i["declined"] for i in left), str(left)[:300])

        # a third: dismissed. Nothing in it is applied, and nothing in it counts as a no
        third = write_dream(repo, SMALL.replace("{LINE}", "- [2026-11-08] The hub dismissed this → it comes back"))
        said_open, theirs_open = kit.open_proposal(store), kit_says()["open"]
        check("the sentence about an open proposal names it and its age as the kit does", theirs_open[0] == third and said_open == f"The proposal {third} ({theirs_open[1]}) has not been applied or dismissed. Apply it or dismiss it, and then dream again.", f"{said_open} / {theirs_open}")
        notes.mkdir(parents=True)
        (notes / "feedback_tests.md").write_text("Run the tests before saying it works.\n")
        check("the row follows the newest dream, which lists none of Claude's own notes", (row() or {}).get("carried") is False)
        shutil.rmtree(notes)
        (store / "dreams" / "2026-01-01-proposal.md").write_text(SMALL.replace("{DAY}", "2026-01-01").replace("{LINE}", "- [2026-01-01] From before the kit kept a record"))
        subprocess.run(["git", "-C", str(store), "add", "dreams/2026-01-01-proposal.md"], check=True)
        subprocess.run(["git", "-C", str(store), "commit", "-q", "-m", "dream 2026-01-01: an old proposal", "--", "dreams/2026-01-01-proposal.md"], check=True)
        old = dreams()
        check("a proposal the store holds from before the kit kept a record is listed under Earlier, apart from the record's", old["earlier"] == ["2026-01-01-proposal.md"] and "2026-01-01-proposal.md" not in [d["file"] for d in old["dreams"]], str(old["earlier"]))
        check("it can be read", (lambda r: r[0] == 200 and r[1]["state"] == "earlier" and r[1]["sections"][1]["items"][0]["quote"].endswith("kept a record"))(read("2026-01-01-proposal.md")))
        for doing, answer in (("ticked", post(f"/api/p/{NAME}/dream/tick", {"file": "2026-01-01-proposal.md", "line": 6, "checked": True, "expect": read("2026-01-01-proposal.md")[1]["hash"]})), ("applied", post(f"/api/p/{NAME}/dream/apply", {"file": "2026-01-01-proposal.md"})), ("dismissed", post(f"/api/p/{NAME}/dream/dismiss", {"file": "2026-01-01-proposal.md"}))):
            check(f"and nothing in it can be {doing}", answer[0] == 409 and answer[1]["error"] == f"This proposal is from before the kit kept a record of proposals, so nothing in it can be {doing}. Only an open proposal can.", str(answer))
        gone = post(f"/api/p/{NAME}/dream/dismiss", {"file": third})
        check("the open proposal can be dismissed, and the kit's answer is shown", third == f"{today}-3-proposal.md" and gone[0] == 200 and gone[1]["said"] == f"dismissed: {third}. 1 of its items are handed to the next dream, and none of them counts as declined by this.", str(gone))
        found = dreams()
        check("dismissing and allowing a declined item again are listed as done in the person's name, through the hub", [(a["what"], a["hub"]) for a in acts()[:2]] == [(f"Dismissed {third}", True), ("Allowed a declined item again", True)], str(acts()[:3]))
        check("it reads as dismissed, newest first", [(d["file"], d["state"]) for d in found["dreams"]] == [(third, "dismissed"), (second, "applied"), (first, "applied")] and read(third)[1]["state"] == "dismissed", str(found["dreams"]))
        carried = [i for i in found["left"] if not i["declined"]]
        check("its item waits for the next dream with no no against it", len(carried) == 1 and carried[0]["from"] == third and carried[0]["left"] == 0 and not carried[0]["ticked"] and len(found["left"]) == 7, str(found["left"])[:300])
        again = post(f"/api/p/{NAME}/dream/dismiss", {"file": third})
        check("a dismissed proposal can't be dismissed again", again[0] == 409 and again[1]["error"] == "This proposal was dismissed, so nothing in it can be dismissed. Only an open proposal can.", str(again))

        # a dream that holds the store: the kit's collector ran in a session and its dream is not finished
        ran = collect(repo)
        check("the kit's collector takes the batch for its dream", ran.returncode == 0 and (store / "dreams/_batch/_owner.json").exists(), ran.stdout + ran.stderr)
        held = dreams()["held"]
        check("the hub says who is dreaming, in the kit's words", held.startswith(f"A dream is already running for {NAME}: session {DREAMER}, since ") and "python3 scripts/dream_owner.py release" in held, held)
        ran = due()
        check("the kit's daily check finds the dream running and says who", ran.returncode == 3 and ran.stdout.startswith(f"not due: a dream is running (session {DREAMER}, since "), ran.stdout + ran.stderr)
        check("the hub passes that verdict on", project()["check"]["verdict"].startswith("not due: a dream is running") and project()["check"]["verdict"] == ran.stdout.strip())
        refused = post(f"/api/p/{NAME}/dream/run", {})
        check("no dream starts while one is alive: the answer is the kit's sentence, and it can be given up", refused[0] == 409 and refused[1]["error"] == held and refused[1].get("held") is True, str(refused))
        freed = post(f"/api/p/{NAME}/dream/release", {})
        check("giving a dream up is listed as done in the person's name, through the hub", freed[0] == 200 and acts()[0] == {"at": acts()[0]["at"], "what": "Gave up a dream that was running", "hub": True}, str(acts()[:2]))
        check("giving it up shows what the kit discarded", freed[0] == 200 and freed[1]["said"].startswith(f"Released {NAME} from session {DREAMER}, which collected on ") and "Nothing in memory changed" in freed[1]["said"], str(freed))
        check("nobody is dreaming after that", dreams()["held"] == "" and not (store / "dreams/_batch").exists())
        nothing = post(f"/api/p/{NAME}/dream/release", {})
        check("giving up when nobody dreams changes nothing and says so", nothing[0] == 200 and nothing[1]["said"] == f"No dream holds {NAME}: there is no batch. Nothing was released.", str(nothing))

        # ── applying, by the kit's own dream_apply.py: what became of each ticked item, and the person's own tier ──
        cache, port = f"- [{today}] The cache needs warming → run warm.sh first (checked: ran it)", f"- [{today}] Port 9 is taken → use port 10 (checked: lsof)"
        warmer, principle = cache.replace("warm.sh first", "warm.sh --all first"), "- A principle added by the person's own pass."
        applying = write_dream(repo, f"""# Dream {{DAY}} — 3 sessions, 7 proposed changes

## Summary
A dream written for this test.

## Proposed changes (tick to approve)
- [x] 1. project/gotchas.md — add — "{cache}"
      why: two sessions hit it · seen: 2/3 sessions · cost: ~4 · evidence: a1
- [x] 2. project/gotchas.md — edit — "{warmer}"
      why: the flag was missing · seen: 1/3 sessions · cost: ~1 · evidence: a2
      replaces: "{cache}"
- [x] 3. project/gotchas.md — add — "{port}"
      why: one session lost time to it · seen: 1/3 sessions · cost: ~2 · evidence: a3
- [x] 4. project/gotchas.md — delete — "{port}"
      why: no longer true · seen: checked directly · cost: 0 · evidence: a4
- [ ] 5. project/gotchas.md — add — "- [{today}] Left unticked → it waits (checked: nothing)"
      why: test · evidence: a5
* [x] 6. project/gotchas.md — add — "- written with a star, so it is not an item"

## ORG.md and Steering (yours to change)
- [x] 7. ORG.md — add — "{principle}"
      why: three sessions needed it · evidence: a6
- [x] 8. ORG.md — add — "- A second principle, approved and then taken back."
      why: one session suggested it · evidence: a7

## Scratchpads to condense / clear
- [x] main — keep: nothing. Clear.

## Trend
Nothing to compare.
""")
        check("before anything is applied, no item has a fate", (lambda d: d["fates"] == {} and d["unread"] == [] and d["yours"] is None)(read(applying)[1]))
        ran = in_repo(repo, "dream_apply.py")
        check("the kit's dream_apply.py applies what is ticked and records each item", ran.returncode == 0 and "In all: 4 applied, 0 already applied, 1 refused, 2 yours, 1 need a session." in ran.stdout, ran.stdout + ran.stderr)
        dream = read(applying)[1]
        by_n = {i["n"]: i for sec in dream["sections"] for i in sec["items"] if i["n"]}
        fate = lambda n: dream["fates"].get(by_n[n]["id"])
        check("each memory item the kit applied says so, with its commit", all(fate(n) and fate(n)["result"] == "applied" and fate(n)["kind"] == "memory" and len(fate(n)["commits"]) >= 1 and all(re.fullmatch(r"[0-9a-f]{7,40}", c) for c in fate(n)["commits"]) for n in (1, 2, 3, 4)), str(dream["fates"]))
        check("an item left unticked has no fate", fate(5) is None)
        check("an ORG.md item is the person's: recorded as yours, with nothing written", fate(7) == {"result": "yours", "reason": fate(7)["reason"], "commits": [], "kind": "org", "parts": []} and principle not in (store / "ORG.md").read_text(), str(fate(7)))
        pad = next(i for i in dream["sections"][3]["items"])
        check("a scratchpad to clear waits for the apply session", dream["fates"][pad["id"]]["result"] == "needs a session" and dream["fates"][pad["id"]]["kind"] == "scratchpad", str(dream["fates"].get(pad["id"])))
        check("a ticked line that isn't written as an item is shown as refused, by name, and not lost", len(dream["unread"]) == 1 and dream["unread"][0]["result"] == "refused" and dream["unread"][0]["kind"] == "unreadable" and "written with a star" in dream["unread"][0]["text"] and dream["unread"][0]["reason"], str(dream["unread"]))
        check("the two commands for the person's own pass are given, ready to paste", dream["yours"] == {"lift": f"chflags nouchg ~/agent-memory/{NAME}/ORG.md", "run": f"cd ~/projects/{NAME} && python3 scripts/dream_apply.py --yours", "waiting": 2}, str(dream["yours"]))
        early = post(f"/api/p/{NAME}/dream/yours", {})
        check("the terminal for the person's pass opens while the proposal is still open, as the kit's pass covers it", early[0] == 200 and len(early[1]["ids"]) == 1, str(early))
        sids += early[1]["ids"]
        check("and it runs the kit's pass, which says the lock is on", on_screen(early[1]["ids"][0], "is locked, and this command never lifts the lock", 30), tmux_screen(early[1]["ids"][0]))
        post(f"/api/sessions/{early[1]['ids'][0]}/dismiss", {})
        check("what waits is counted for the project's own row", project()["waits"] == {"you": 0, "a session": 0})  # nothing is carried until the proposal is closed
        history = call("GET", f"/api/p/{NAME}/file?path=project/gotchas.md")[1]["history"]
        check("the change log shows an edit as the line before and after, read by the kit", any(row["was"] == cache and row["now"] == warmer and "was:" not in row["why"] for row in history), str(history)[:500])
        check("and a removal as the line that went", any(row["was"] == port and row["now"] is None and "removed:" not in row["why"] for row in history), str(history)[:500])
        check("an added line has no line before it", any(row["was"] is None and row["now"] is None and row["why"] for row in history), str(history)[:300])
        feed = call("GET", f"/api/p/{NAME}/home")[1]["activity"]
        check("the activity feed carries the same", any(a["kind"] == "memory" and a.get("was") == cache and a.get("now") == warmer for a in feed) and any(a["kind"] == "memory" and a.get("was") == port and a.get("now") is None for a in feed), str(feed)[:400])
        lines = (store / "project" / "gotchas.md").read_text()
        check("memory holds what the record says", warmer in lines and cache not in lines and port not in lines)
        ran = close_applied()
        check("the kit closes the proposal from its record: one unticked item is handed on, and two approved ones stay", ran.returncode == 0 and ran.stdout.startswith(f"applied: {applying}. 1 of its items were not applied and are handed to the next dream; 3 approved and not done yet stay until done or withdrawn"), ran.stdout + ran.stderr)
        mine = lambda: {i["id"]: i for i in dreams()["left"] if i["from"] == applying}
        org, scratch = by_n[7]["id"], pad["id"]
        left = mine()
        check("what the person approved waits for the person or for a session, by the kit's own word, and is not mixed with what goes to the next dream",
              sorted((i["number"] is None, str(i["waiting"]), i["ticked"], i["declined"]) for i in left.values()) == [(False, "None", False, False), (False, "you", True, False), (False, "you", True, False), (True, "a session", True, False)], str(left))
        check("what waits for the person and for a session is counted for the project's own row", project()["waits"] == {"you": 2, "a session": 1}, str(project()["waits"]))
        second_thoughts = left[by_n[8]["id"]]
        taken = post(f"/api/p/{NAME}/dream/withdraw", {"item": second_thoughts["item"]})
        check("one of the two is taken back", taken[0] == 200 and taken[1]["said"].startswith("withdrawn: ORG.md — add — "), str(taken))
        check("its proposal then says one change is the person's, though its apply record still calls two theirs", read(applying)[1]["yours"]["waiting"] == 1 and sum(1 for f in read(applying)[1]["fates"].values() if f["result"] == "yours") == 2, str(read(applying)[1]["yours"]))
        check("taking back is listed as done in the person's name, through the hub", acts()[0]["what"] == f"Took back item 8 of {applying}" and acts()[0]["hub"] is True, str(acts()[:2]))
        left = mine()
        check("an item with no number is known by its id and its first words", left[scratch]["number"] is None and left[scratch]["quote"] == "main" and left[scratch]["item"].startswith("main — keep"), str(left[scratch]))
        check("each waiting item says what gets it done, in the kit's words", left[org]["way"] == "It is yours to make, in a terminal: chflags nouchg memory/ORG.md, then python3 scripts/dream_apply.py --yours." and left[scratch]["way"] == "A session does it: /dream apply in a session." and not left[org]["dead"] and not left[scratch]["dead"], str([left[org]["way"], left[scratch]["way"]]))

        # two later proposals close, one dismissed and one applied: what was approved is still there
        passing = write_dream(repo, SMALL.replace("{LINE}", f"- [{today}] A later proposal, dismissed → nothing of it applied"))
        by_hand = in_repo(repo, "dream_proposal.py", "dismiss")
        check("a later proposal is dismissed, in a terminal and not through the hub", by_hand.returncode == 0 and acts()[0]["what"] == f"Dismissed {passing}" and acts()[0]["hub"] is False, by_hand.stdout + by_hand.stderr + str(acts()[:2]))
        later = write_dream(repo, SMALL.replace("- [ ] 1.", "- [x] 1.").replace("{LINE}", f"- [{today}] A later proposal, applied → its one item ticked"))
        did, ran = in_repo(repo, "dream_apply.py"), close_applied()
        check("another is applied", did.returncode == 0 and ran.returncode == 0, did.stdout + did.stderr + ran.stdout + ran.stderr)
        left = mine()
        check("an ORG.md item approved three proposals ago still waits for the person, and the scratchpad for a session", left[org]["waiting"] == "you" and left[scratch]["waiting"] == "a session" and [d["file"] for d in dreams()["dreams"]][:2] == [later, passing], str(left))
        check("the open-proposal answer still comes from the first line alone, with items waiting", kit.open_proposal(store) == "" and len(kit.said_bare(store)["ways"]) == 2, str(kit.said_bare(store)))

        # an approved item whose proposal file is gone: a dead end, said as the kit says it, and taken back
        scout = write_dream(repo, f"""# Dream {{DAY}} — 3 sessions, 0 proposed changes

## Summary
A dream written for this test.

## Scratchpads to condense / clear
- [x] scout — keep: nothing. Clear.

## Trend
Nothing to compare.
""")
        did, ran = in_repo(repo, "dream_apply.py"), close_applied()
        check("a scratchpad item of another proposal waits for a session too", did.returncode == 0 and ran.returncode == 0 and any(i["waiting"] == "a session" and i["from"] == scout for i in dreams()["left"]), did.stdout + did.stderr + ran.stdout + ran.stderr)
        (store / "dreams" / scout).unlink()
        lost = next(i for i in dreams()["left"] if i["from"] == scout)
        check("with its proposal's file gone, the hub says nothing can do it any more, and names its own way to clear it", lost["dead"] and lost["way"] == f"Nothing can do it any more: its proposal {scout} is gone. Take it back to clear it.", str(lost))
        check("what the hub reads from the kit's printed list is what the kit means, item for item, the dead end among them", {f"{item}|{proposal}": way for (item, proposal), way in kit.said_bare(store)["ways"].items()} == kit_says()["ways"] and len(kit_says()["ways"]) == 3, f"{kit.said_bare(store)['ways']} / {kit_says()['ways']}")
        check("the proposal whose file is gone is listed as gone, and opening it says so", next(d for d in dreams()["dreams"] if d["file"] == scout)["gone"] is True and (lambda r: r[0] == 200 and r[1]["gone"] is True and r[1]["state"] == "applied" and r[1]["sections"] == [])(read(scout)), str(read(scout))[:200])
        check("a proposal whose file is there is not", next(d for d in dreams()["dreams"] if d["file"] == applying)["gone"] is False and read(applying)[1]["gone"] is False)
        nobody = post(f"/api/p/{NAME}/dream/withdraw", {"item": "words no waiting item holds"})
        check("taking back an item that isn't waiting is refused in the kit's words", nobody[0] == 409 and nobody[1]["error"].startswith('0 approved items that wait hold "words no waiting item holds"'), str(nobody))
        taken = post(f"/api/p/{NAME}/dream/withdraw", {"item": lost["item"]})
        check("an approved item can be taken back, and the kit's answer is shown", taken[0] == 200 and taken[1]["said"] == f"withdrawn: {lost['item']}\nIt is no longer waiting to be done. A dream may propose it again.", str(taken))
        check("it no longer waits, and the other two still do", not any(i["from"] == scout for i in dreams()["left"]) and sorted(i["waiting"] for i in mine().values() if i["waiting"]) == ["a session", "you"])
        subprocess.run(["git", "-C", str(store), "checkout", "-q", "--", f"dreams/{scout}"], check=True)

        # ── a carry item's facts, what needs the person, pins, and the file of what was read and not proposed ──
        need = "The deploy script still points at the old host. It needs a fix · evidence: deploy.sh:3"
        needing = write_dream(repo, SMALL.replace("{LINE}", f"- [{today}] A line the proposal needs").replace("\n## Proposed changes", f"\n## Needs you\n- {need}\n\n## Proposed changes"))
        found = dreams()["needs"]
        check("what a dream said needs the person is listed from the kit's needs.json, and counted on the project's row", found["problem"] == "" and [(n["text"], n["from"], n["said"]) for n in found["open"]] == [(need, needing, 1)] and project()["needs"] == 1, str(found))
        shown = read(needing)[1]
        check("in the proposal it is a plain line of its own section, never an item", [(s["kind"], s["needs"], len(s["items"])) for s in shown["sections"] if s["kind"] == "needs"] == [("needs", [{"text": need, "resolved": False}], 0)], str(shown["sections"]))
        original = (store / "dreams" / needing).read_text()
        fact_one, fact_two = f'- [{today}] Fact one (by Sam, from Claude\'s own note one.md)', f"- [{today}] Fact two"
        (store / "dreams" / needing).write_text(original.replace("\n## Trend", "\n## Claude's own notes (auto memory)\n- [ ] 2. notes shop/one.md — carry — 2 checked facts\n      why: checked, not in memory · evidence: shop/one.md\n"
                                                                 f'      + project/gotchas.md "{fact_one}"\n      + project/decisions.md "{fact_two}"\n\n## Trend'))
        side = store / "dreams" / needing.replace("-proposal.md", "-notes.md")
        side.write_text("# Dream: read and not proposed\n## Claude's own notes, file by file\n- shop/one.md — facts: 3 · checked: 2 · unchecked: 0 · already in memory: 0 · stale: 1 · rejected: 0\n"
                        "- shop/MEMORY.md — facts: 0 · checked: 0 · unchecked: 0 · already in memory: 0 · stale: 0 · rejected: 0 · nothing here because: an index\n## Not carried over\n- shop/one.md — the old port — stale: it moved\n")
        try:
            shown = read(needing)[1]
            carry = next(i for s in shown["sections"] for i in s["items"] if i["action"] == "carry")
            check("a carry item's facts are read with their signs and lines", [(f["sign"], f["file"], f["text"]) for f in carry["facts"]] == [("+", "project/gotchas.md", fact_one), ("+", "project/decisions.md", fact_two)], str(carry))
            check("the file beside the proposal is read, note by note, and is not listed as a proposal",
                  shown["notes"] == {"file": side.name, "sections": [{"name": "Claude's own notes, file by file", "lines": [], "notes": [
                      {"note": "shop/one.md", "counts": [["facts", 3], ["checked", 2], ["unchecked", 0], ["already in memory", 0], ["stale", 1], ["rejected", 0]], "because": ""},
                      {"note": "shop/MEMORY.md", "counts": [["facts", 0], ["checked", 0], ["unchecked", 0], ["already in memory", 0], ["stale", 0], ["rejected", 0]], "because": "an index"}]},
                      {"name": "Not carried over", "lines": ["shop/one.md — the old port — stale: it moved"], "notes": []}]}
                  and side.name not in [d["file"] for d in dreams()["dreams"]] + dreams()["earlier"] and read(side.name)[0] == 409, str(shown["notes"]))
            sign = lambda fact, on, expect: post(f"/api/p/{NAME}/dream/fact", {"file": needing, "line": fact["line"], "carry": on, "expect": expect})
            before = (store / "dreams" / needing).read_text()
            left_out = sign(carry["facts"][1], False, shown["hash"])
            after = (store / "dreams" / needing).read_text()
            changed = [(a, b) for a, b in zip(before.split("\n"), after.split("\n")) if a != b]
            check("leaving a fact out turns its + into a - and changes nothing else", left_out[0] == 200 and len(before) == len(after) and changed == [(f'      + project/decisions.md "{fact_two}"', f'      - project/decisions.md "{fact_two}"')], str(left_out) + str(changed))
            again = read(needing)[1]
            kept = next(i for s in again["sections"] for i in s["items"] if i["action"] == "carry")
            check("the item is still the one the dream wrote", kept["id"] == carry["id"] and [f["sign"] for f in kept["facts"]] == ["+", "-"] and again["hash"] == left_out[1]["hash"])
            check("a stale version is refused", sign(kept["facts"][0], False, shown["hash"])[0] == 409)
            check("a line that isn't a fact of a carry item is refused", post(f"/api/p/{NAME}/dream/fact", {"file": needing, "line": kept["line"] + 1, "carry": False, "expect": again["hash"]})[0] == 409
                  and post(f"/api/p/{NAME}/dream/fact", {"file": needing, "line": kept["line"], "carry": False, "expect": again["hash"]})[0] == 409)
            check("a fact whose carry isn't said is refused", post(f"/api/p/{NAME}/dream/fact", {"file": needing, "line": kept["facts"][0]["line"], "carry": "no", "expect": again["hash"]})[0] == 400)
            check("turning it back on restores the proposal as the dream wrote it", sign(kept["facts"][1], True, again["hash"])[0] == 200 and (store / "dreams" / needing).read_text() == before)
            check("a fact in a closed proposal can't be changed", post(f"/api/p/{NAME}/dream/fact", {"file": applying, "line": 9, "carry": False, "expect": read(applying)[1]["hash"]})[0] == 409)

            gotchas = store / "project" / "gotchas.md"
            kept_lines = gotchas.read_text()
            gotchas.write_text(kept_lines.rstrip("\n") + f"\n- [{today}] Builds run under the lock, always (pin)\n")
            pins = call("GET", f"/api/p/{NAME}/pins")[1]
            check("a pinned entry is listed by its title, without its date or the words of the pin", [(p["file"], p["title"]) for p in pins] == [("project/gotchas.md", "Builds run under the lock, always")], str(pins))
            gotchas.write_text(kept_lines)
            principles = call("GET", f"/api/p/{NAME}/file?path=ORG.md")[1]
            refused = post(f"/api/p/{NAME}/file", {"path": "ORG.md", "content": principles["content"].rstrip("\n") + "\n- A principle, pinned by hand (pin)\n", "expect": principles["hash"]})
            check("the hub's editor can't add a pin", refused[0] == 409 and "pin" in refused[1]["error"] and call("GET", f"/api/p/{NAME}/file?path=ORG.md")[1]["hash"] == principles["hash"], str(refused))
        finally:
            side.unlink(missing_ok=True)
            subprocess.run(["git", "-C", str(store), "checkout", "-q", "--", f"dreams/{needing}"], check=False)
        nobody = post(f"/api/p/{NAME}/dream/done", {"words": "words no need holds"})
        check("closing what no need holds is refused in the kit's words", nobody[0] == 409 and nobody[1]["error"].startswith('0 things that need you hold "words no need holds"'), str(nobody))
        closed = post(f"/api/p/{NAME}/dream/done", {"words": need})
        check("the person closes one through the kit, and the kit's answer is shown", closed[0] == 200 and closed[1]["said"].startswith(f"closed: {need}"), str(closed))
        check("it is off the list, and listed as done in the person's name, through the hub", dreams()["needs"]["open"] == [] and project()["needs"] == 0 and acts()[0] == {"at": acts()[0]["at"], "what": "Closed something that needed you", "hub": True}, str(acts()[:2]))
        check("the proposal is dismissed", post(f"/api/p/{NAME}/dream/dismiss", {"file": needing})[0] == 200)

        # the person's own pass, in a terminal the hub opens: the kit's script, a lock only the person lifts, a typed yes
        locked = lambda: bool(os.stat(store / "ORG.md").st_flags & 2)  # UF_IMMUTABLE
        opened = post(f"/api/p/{NAME}/dream/yours", {})
        check("the hub opens a terminal for the person's pass", opened[0] == 200 and len(opened[1]["ids"]) == 1, str(opened))
        mine_sid = opened[1]["ids"][0]
        sids.append(mine_sid)
        seen_by = lambda words, seconds=30: on_screen(mine_sid, words, seconds)
        check("with the lock on, the kit's script says so and how to lift it, and the hub has not lifted it", seen_by("is locked, and this command never lifts the lock") and f"chflags nouchg {store / 'ORG.md'}" in tmux_screen(mine_sid).replace("\n", "") and locked() and principle not in (store / "ORG.md").read_text(), tmux_screen(mine_sid))
        subprocess.run(["chflags", "nouchg", str(store / "ORG.md")], check=True)  # the person's own step
        subprocess.run(TMUX + ["send-keys", "-t", f"hub-{mine_sid}", "-l", "python3 scripts/dream_apply.py --yours"], check=True)
        subprocess.run(TMUX + ["send-keys", "-t", f"hub-{mine_sid}", "Enter"], check=True)
        check("with the lock lifted, the script shows the change and asks for a typed yes", seen_by("Apply these 1 as shown? Type yes:") and "A principle added by the person" in tmux_screen(mine_sid) and principle not in (store / "ORG.md").read_text(), tmux_screen(mine_sid))
        subprocess.run(TMUX + ["send-keys", "-t", f"hub-{mine_sid}", "-l", "yes"], check=True)
        subprocess.run(TMUX + ["send-keys", "-t", f"hub-{mine_sid}", "Enter"], check=True)
        check("after the yes it is written and the file is locked again", seen_by("ORG.md is locked again.") and principle in (store / "ORG.md").read_text() and locked(), tmux_screen(mine_sid))
        after = read(applying)[1]
        check("the record now says the ORG.md item is applied, and nothing is left for the person", after["fates"][org]["result"] == "applied" and after["yours"] is None, str(after["fates"]))
        check("it no longer waits, the scratchpad still does, and the kit's check is clean", [(i["quote"], i["waiting"]) for i in mine().values() if i["waiting"]] == [("main", "a session")] and dreams()["checks"] == [], str(mine()) + str(dreams()["checks"]))
        nothing = post(f"/api/p/{NAME}/dream/yours", {})
        check("with nothing of the person's waiting, no terminal is opened", nothing[0] == 409 and nothing[1]["error"] == "No ORG.md or Steering change is waiting for you.", str(nothing))
    finally:
        shutil.rmtree(notes, ignore_errors=True)
        if notes.parent.exists() and not any(notes.parent.iterdir()):
            notes.parent.rmdir()

    # ── how the hub asks the kit: checked on the hub's own functions, against this project ──
    real_env, real_kit, real_wait, real_time = kit.ENV, kit.KIT, kit.TIMEOUT, kit.time
    try:
        kit.ENV = {**real_env, "CLAUDE_CODE_SESSION_ID": "a-session", "AGENT_MEMORY_DREAM_RUN": "a-run", "AGENT_MEMORY_DREAM_REASON": "x", "AGENT_MEMORY_PROPOSE_ONLY": "1", "AGENT_MEMORY_NOW": "5"}
        given = kit.kit_env(store)
        check("a kit script the hub runs is never given the marks of a dream or a moved clock", not any(mark in given for mark in kit.NOT_THE_HUBS) and len(kit.NOT_THE_HUBS) == 5 and given["AGENT_MEMORY_DIR"] == str(store), str(sorted(k for k in given if k.startswith(("AGENT_", "CLAUDE_")))))
        check("and never takes git's optional locks, so a read writes nothing", given["GIT_OPTIONAL_LOCKS"] == "0")
        check("with those marks in the hub's own environment the kit still answers as for a person", kit.record_of(store)["problem"] == "" and kit.dreamer(store) == "")
        kit.ENV = real_env
        asked = []
        real_ask, real_run_kit = kit.ask, kit.run_kit
        kit.ask = lambda store_, script, *args, **more: (asked.append(script), real_ask(store_, script, *args, **more))[1]
        hub.home_of(NAME)
        check("a project's homepage runs neither the kit's check nor its question of who is dreaming", "memcheck.py" not in asked and "dream_owner.py" not in asked, str(asked))
        asked.clear()
        hub.dreams_of(NAME)
        check("the Dreams page does", "memcheck.py" in asked and "dream_owner.py" in asked, str(asked))
        kit.ask = real_ask
        there = [str(repo), str(HOME / "projects")]
        write_job(NAME, there[1], 7)
        check("a dream runs in the folder its job runs in, when that is a folder of the project", hub.dream_repo(NAME, there) == there[1])
        write_job(NAME, "/somewhere/else", 7)
        check("and in the project's first folder when it isn't", hub.dream_repo(NAME, there) == there[0])
        job_of(NAME).unlink()
        check("and with no job", hub.dream_repo(NAME, there) == there[0])
        count, clock = [], [6000.0]
        work = lambda: count.append(1) or len(count)

        class Clock:
            time = staticmethod(lambda: clock[0])
        kit.time = Clock
        kit.remembered("a-test-answer", lambda: "same", work)
        clock[0] += 50
        kit.remembered("a-test-answer", lambda: "same", work)
        check("an answer is kept while what it rests on is the same", len(count) == 1)
        kit.remembered("a-test-answer", lambda: "changed", work)
        check("and asked for again when that changes", len(count) == 2)
        clock[0] += 61
        kit.remembered("a-test-answer", lambda: "changed", work)
        kit.time = real_time
        check("and for a minute at most", len(count) == 3)
        check("a kit script gets a minute to answer", kit.TIMEOUT == 60)
        # a kit that doesn't answer: an action says so in plain words, and a read becomes that project's problem
        kit.TIMEOUT = 2
        with open(store / ".memwrite.lock", "w") as holder:
            fcntl.flock(holder, fcntl.LOCK_EX)  # another writer holds this memory
            try:
                kit.withdraw(store, "anything")
                quiet = ""
            except hub.Refused as refused:
                quiet = str(refused)
            try:
                hub.withdraw_item(NAME, {"item": "anything"})
                through = ""
            except hub.Refused as refused:
                through = f"{refused.status} {refused}"
        check("an action the kit doesn't answer in time is refused in plain words, and nothing is changed", quiet == "The kit didn't answer within 2 seconds. Another writer may hold this memory. Nothing was changed." and through == f"409 Conflict {quiet}" and len(dreams()["left"]) == len(kit.record_of(store)["left"]), quiet + " / " + through)
        slow = Path(tempfile.mkdtemp(prefix="slow-kit."))
        (slow / "scripts").mkdir()
        for script in ("dream_proposal.py", "dream_owner.py", "memcheck.py", "own_notes.py"):
            (slow / "scripts" / script).write_text("import time\ntime.sleep(30)\n")
        kit.KIT = slow
        with contextlib.redirect_stdout(io.StringIO()):
            silent, page = kit.record_of(store), hub.dreams_of(NAME)
            others_notes = kit.own_notes_of(store, [str(repo)])
        kit.KIT = real_kit
        shutil.rmtree(slow)
        check("a read the kit doesn't answer in time is that project's problem, said in plain words, and nothing is raised", silent == {"proposals": [], "left": [], "problem": "The kit didn't answer within 2 seconds. Another writer may hold this memory. Nothing was changed."}
              and page["problem"] == silent["problem"] and page["dreams"] == [] and page["held"] == "" and page["checks"] == [] and others_notes is None, str(page)[:300])
        kit.TIMEOUT = real_wait
        check("with the kit answering again, the project is back", kit.record_of(store)["problem"] == "" and len(kit.record_of(store)["proposals"]) >= 5)
    finally:
        kit.ENV, kit.KIT, kit.TIMEOUT, kit.time = real_env, real_kit, real_wait, real_time
        job_of(NAME).unlink(missing_ok=True)
    record_file = store / "dreams" / (applying[:-len("-proposal.md")] + "-applied.json")
    good_record = record_file.read_bytes()
    try:
        for label, damage in (("isn't JSON", "{"), ("has a row with a result the kit never writes", json.dumps({"proposal": applying, "items": [{**json.loads(good_record)["items"][0], "result": "done"}]})),
                              ("has a row with no key", json.dumps({"proposal": applying, "items": [{k: v for k, v in json.loads(good_record)["items"][0].items() if k != "key"}]})),
                              ("has a row whose commits aren't text", json.dumps({"proposal": applying, "items": [{**json.loads(good_record)["items"][0], "commits": [1]}]}))):
            record_file.write_text(damage)
            got = read(applying)[1]
            check(f"an apply record that {label} shows a plain line and no fate, and breaks nothing", got["record_problem"].startswith("The kit's record of what was applied can't be read") and got["fates"] == {} and got["unread"] == [] and others(), str(got)[:300])
    finally:
        record_file.write_bytes(good_record)
    check("with the record back, the fates are back", len(read(applying)[1]["fates"]) >= 6 and read(applying)[1]["record_problem"] == "")

    status, made = post("/api/sessions", {"project": NAME, "tasks": ["Reply with the single word ready"], "isolate": False})
    check("a session starts", status == 200, str(made))
    sid = made["ids"][0]
    sids.append(sid)
    needs = wait(sid, lambda s: s["state"] == "needs", 25)
    check("the trust prompt shows as needs you", needs and needs["state"] == "needs", str(needs))
    answer_first_run(sid)
    done = wait(sid, lambda s: s["connected"] and s["state"] == "finished", 75)
    check("the session reaches finished", done and done["state"] == "finished", str(done))
    check("the session is on the phone", done and done["phone"] and done["phone"].startswith("https://claude.ai/code/session_"), str(done))
    check("the session's hook writes into this hub's state folder", (STATE / "events" / f"{sid}.json").exists(), str(STATE))

    s, status, first = ws_open(f"/ws/term/{sid}?cols=100&rows=30", {**OWNER, "Origin": CFG["origin"]})
    check("the terminal connects", " 101 " in status, status)
    screen = first + ws_collect(s, 2.5)
    check("the terminal shows the session", b"ready" in screen, repr(screen[:200]))
    ws_send(s, json.dumps({"t": "r", "c": 90, "r": 28}))
    wide = lambda: subprocess.run(TMUX + ["display-message", "-p", "-t", f"hub-{sid}", "#{window_width}"], capture_output=True, text=True).stdout.strip()
    check("resize reaches the session", next((True for _ in range(50) if wide() == "90" or time.sleep(0.2)), False), wide())
    ws_send(s, json.dumps({"t": "i", "d": "/status-not-sent"}))
    check("typing reaches the session", on_screen(sid, "/status-not-sent"), tmux_screen(sid))
    ws_send(s, json.dumps({"t": "i", "d": "\x15"}))  # the terminal is closed only once the key has gone through: what was typed must not stay in Claude's input
    cleared = next((True for _ in range(40) if not hub.typed_draft(tmux_screen(sid)) or time.sleep(0.25)), False)
    check("what was typed is cleared again", cleared, tmux_screen(sid))
    s.close()
    time.sleep(1.5)
    check("closing the terminal leaves the session running", session(sid)["alive"])
    s, status, _ = ws_open(f"/ws/term/{sid}?cols=100&rows=30", {**OWNER, "Origin": CFG["origin"]})
    check("the terminal reattaches", " 101 " in status and b"ready" in ws_collect(s, 2.5))
    s.close()

    # what waits for a session, with no proposal open: /dream apply in a session does it and reports it
    (store / "agents" / "main").mkdir(parents=True, exist_ok=True)
    (store / "agents" / "main" / "scratch.md").write_text("# main: scratch\n\n- a theory nobody needs any more\n- another one\n")
    waits_for_session = lambda: [i for i in dreams()["left"] if i["waiting"] == "a session"]
    check("no proposal is open and the scratchpad item still waits for a session", kit.open_proposal(store) == "" and [i["quote"] for i in waits_for_session()] == ["main"], str(waits_for_session()))
    status, doing = post(f"/api/p/{NAME}/dream/session", {})
    check("the hub starts a session for it", status == 200 and len(doing["ids"]) == 1, str(doing))
    sids += doing["ids"]
    answer_first_run(doing["ids"][0])
    end = time.time() + 300
    while time.time() < end and waits_for_session():
        time.sleep(4)
    check("the session did it and reported it: nothing waits for a session any more", waits_for_session() == [], str(waits_for_session()) + "\n" + tmux_screen(doing["ids"][0]))
    check("the record of the proposal it came from says it is done", read(applying)[1]["fates"][scratch]["result"] == "applied", str(read(applying)[1]["fates"].get(scratch)))
    refused = post(f"/api/p/{NAME}/dream/session", {})
    check("with nothing waiting for a session, none is started", refused[0] == 409 and refused[1]["error"] == "Nothing approved is waiting for a session.", str(refused))

    # a fifth proposal, applied for real: /dream apply in a session writes the line and closes the proposal itself
    lesson = "- [2026-11-08] Hub apply test lesson → it worked"
    fourth = write_dream(repo, SMALL.replace("{LINE}", lesson))
    dream = read(fourth)[1]
    item = dream["sections"][1]["items"][0]
    check("a proposal without the new sections still works", fourth not in (first, second, third, applying) and not any(s["kind"] for s in dream["sections"]) and item["n"] == 1 and item["file"] == "project/gotchas.md", str(dream["sections"]))
    check("a change can be ticked", tick(dream, item["line"])[0] == 200)
    check("the tick is on disk", "- [x] 1." in (store / "dreams" / fourth).read_text())
    status, applied = post(f"/api/p/{NAME}/dream/apply", {"file": fourth})
    check("apply opens a session", status == 200, str(applied))
    sids += applied["ids"]
    answer_first_run(applied["ids"][0])
    end = time.time() + 180
    while time.time() < end and lesson not in (store / "project" / "gotchas.md").read_text():
        time.sleep(3)
    check("apply wrote the approved line", lesson in (store / "project" / "gotchas.md").read_text())
    end = time.time() + 120
    while time.time() < end and read(fourth)[1]["state"] != "applied":
        time.sleep(3)
    check("the session closed the proposal itself, and it reads as applied", read(fourth)[1]["state"] == "applied" and project()["latest"]["state"] == "applied")
    check("the phone link exists for apply too", (wait(applied["ids"][0], lambda x: x["connected"], 20) or {}).get("connected"))


    status, _ = post(f"/api/p/{NAME}/dream/run", {})
    check("Dream now starts", status == 200, str(_))
    state_of = lambda: next(x for x in call("GET", "/api/state")[1]["projects"] if x["name"] == NAME)
    time.sleep(2)
    check("the project reads as dreaming", state_of()["dreaming"])
    check("a second dream is refused", post(f"/api/p/{NAME}/dream/run", {})[0] == 409)
    end = time.time() + 240
    while time.time() < end and state_of()["dreaming"]:
        time.sleep(4)
    check("the dream finishes", not state_of()["dreaming"])

    status, made = post("/api/sessions", {"project": NAME, "tasks": ["Reply with one", "Reply with two"], "isolate": True})
    check("two agents start", status == 200 and len(made["ids"]) == 2, str(made))
    sids += made["ids"]
    for index, agent in enumerate(made["ids"], 1):
        answer_first_run(agent)
        ready = wait(agent, lambda x: x["connected"], 60)
        check(f"agent {index} is on the phone", ready and ready["connected"], str(ready))
    trees = [t for t in (repo / ".claude" / "worktrees").glob("*")] if (repo / ".claude" / "worktrees").exists() else []
    check("each agent has its own worktree", len(trees) == 2, str(trees))
    check("worktrees are linked to memory", trees and all((t / "memory").is_symlink() for t in trees))

    # identity and homepage
    check("a project can be renamed", post(f"/api/p/{NAME}/profile", {"display": "Smoke  Test", "icon": {"kind": "glyph", "value": "bolt", "color": "#4cbb8a"}})[0] == 200)
    entry = next(x for x in call("GET", "/api/state")[1]["projects"] if x["name"] == NAME)
    check("the name and icon show in state", entry["display"] == "Smoke Test" and entry["icon"]["value"] == "bolt", str(entry["icon"]))
    check("the store keeps its name", (HOME / "agent-memory" / NAME / "ORG.md").exists())
    check("a bad icon is refused", post(f"/api/p/{NAME}/profile", {"display": "x", "icon": {"kind": "glyph", "value": "<script>", "color": "#fff"}})[0] == 409)
    raw = {**WRITE, "Content-Type": "application/octet-stream"}
    check("a non-image icon is refused", call("POST", f"/api/p/{NAME}/icon", raw, None)[0] == 409)
    home = call("GET", f"/api/p/{NAME}/home")[1]
    check("the homepage has repo and activity data", home["repos"][0]["name"] == NAME and isinstance(home["activity"], list), str(home)[:200])

    # sharing
    PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")
    def up(sid_, name, data):
        c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=30)
        c.request("POST", f"/api/sessions/{sid_}/upload?name={name}", data, raw)
        r = c.getresponse()
        return r.status, json.loads(r.read())
    status, png = up(sid, "shot.png", PNG)
    check("an image uploads", status == 200, str(png))
    status, wav = up(sid, "note.wav", b"RIFF$\x00\x00\x00WAVEfmt " + b"\x00" * 28)
    check("audio uploads", status == 200, str(wav))
    check("an unsupported upload is refused", up(sid, "run.exe", b"x")[0] == 409)
    url = lambda path: f"/api/sessions/{sid}/file?path={urllib.parse.quote(path)}"
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=30)
    c.request("GET", url(png["path"]), headers=OWNER)
    r = c.getresponse(); body = r.read()
    check("the image is served, sandboxed", r.status == 200 and body == PNG and "sandbox" in (r.getheader("Content-Security-Policy") or ""), str(r.status))
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=30)
    c.request("GET", url(wav["path"]), headers={**OWNER, "Range": "bytes=0-3"})
    r = c.getresponse(); body = r.read()
    check("audio supports seeking", r.status == 206 and body == b"RIFF", str(r.status))
    secret = HOME / "projects" / NAME / ".env"
    secret.write_text("x")
    (HOME / "projects" / NAME / ".git" / "note.md").write_text("x")
    for label, bad in (("an ssh key", str(HOME / ".ssh/id_rsa.pem")), ("keys folder", str(HOME / ".aws/x.json")), ("a file outside the project", str(HOME / "agent-memory" / NAME / "ORG.md")),
                       ("git internals", str(HOME / "projects" / NAME / ".git/note.md")), ("a traversal", "../../../.ssh/config.md"), ("a dotenv", ".env"), ("a bad kind", "/etc/passwd")):
        check(f"shared file refused: {label}", call("GET", url(bad))[0] == 409)
    check("a file needs identity", call("GET", url(png["path"]), {"Host": HOST})[0] == 403)
    message = f"see https://example.com/report?id=7 and {png['path']} and {wav['path']}"
    tell(sid, f"Reply with exactly this text and nothing else: {message}")
    end = time.time() + 70
    shared = []
    while time.time() < end and len(shared) < 3:
        time.sleep(3)
        shared = call("GET", f"/api/sessions/{sid}/shared")[1]
    kinds = {i["kind"] for i in shared}
    check("the chat's link, image and audio are found", {"link", "image", "audio"} <= kinds, str(shared)[:300] + "\n" + tmux_screen(sid))

    # mode and model
    for field, value in (("mode", "bypassPermissions"), ("model", "gpt"), ("effort", "huge"), ("agent", "nobody")):
        check(f"a session refuses an unknown {field}", post("/api/sessions", {"project": NAME, field: value})[0] == 409)
    settings = HOME / ".claude/settings.json"
    before = settings.read_text()
    # Its mode and model are changed between turns: the hub refuses while Claude is working, and the reply above may still be ending.
    idle = next((True for _ in range(240) if (session(sid) or {}).get("state") == "finished" and "esc to interrupt" not in tmux_screen(sid).lower() or time.sleep(0.5)), False)
    check("the session is idle before its mode and model are changed", idle, tmux_screen(sid)[-300:])
    status, result = post(f"/api/sessions/{sid}/mode", {"mode": "plan"})
    check("the mode changes in a running session", status == 200 and wait(sid, lambda x: x["mode"] == "plan", 8)["mode"] == "plan", str(result))
    post(f"/api/sessions/{sid}/mode", {"mode": "auto"})
    check("a bad model is refused on a running session", post(f"/api/sessions/{sid}/model", {"model": "gpt"})[0] == 409)
    subprocess.run(TMUX + ["send-keys", "-t", f"hub-{sid}", "-l", "half typed"], check=True)
    time.sleep(0.6)
    check("a model change won't touch a half-typed message", post(f"/api/sessions/{sid}/model", {"model": "haiku"})[0] == 409)
    subprocess.run(TMUX + ["send-keys", "-t", f"hub-{sid}", "C-u"], check=True)
    time.sleep(0.6)
    status, result = post(f"/api/sessions/{sid}/model", {"model": "haiku"})
    check("the model changes in a running session", status == 200, str(result))
    time.sleep(1.5)
    check("a session's model change leaves your default alone", settings.read_text() == before)
    tell(sid, "Reply with the single word ok")
    landed = wait(sid, lambda x: x["model"] == "haiku" and x["state"] == "finished" and x["said"].lower().startswith("ok"), 40)
    check("the next reply comes from the new model", landed["model"] == "haiku" and landed["said"].lower().startswith("ok"), str(landed)[:300])

    # what a session's subagents did, from a transcript made for this test
    fake = "abcd" + f"{os.getpid() % 65536:04x}"
    folder = HOME / ".claude/projects" / f"hub-test-{os.getpid()}"
    (folder / "run/subagents").mkdir(parents=True)
    pixel = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
    picture = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": pixel}}
    def line(kind, content, **extra):
        return json.dumps({"type": kind, "timestamp": "2026-11-08T10:00:00Z", "message": {"role": kind, "content": content, "model": "claude-sonnet-5-5"}, **extra})
    (folder / "run.jsonl").write_text("\n".join([
        line("assistant", [{"type": "tool_use", "id": "t1", "name": "Agent", "input": {"description": "Check the logs", "subagent_type": "reviewer", "prompt": "Look"}},
                           {"type": "tool_use", "id": "t2", "name": "Agent", "input": {"description": "Still going", "subagent_type": "reviewer", "prompt": "Wait", "run_in_background": True}},
                           {"type": "tool_use", "id": "t3", "name": "Edit", "input": {"file_path": "/a/b.py"}}]),
        line("user", [{"type": "tool_result", "tool_use_id": "t1", "content": [{"type": "text", "text": "[Harness note, not part of the report]\nAll clear"}]}, {"type": "tool_result", "tool_use_id": "t3", "is_error": True, "content": "no"}], toolUseResult={"status": "completed"}),
        line("user", [{"type": "tool_result", "tool_use_id": "t2", "content": "launched"}], toolUseResult={"status": "async_launched"}),
        line("user", [{"type": "tool_result", "tool_use_id": "t9", "content": [picture]}]),
        line("assistant", [{"type": "text", "text": "**All** done here"}]), ""]))
    (folder / "run/subagents/agent-aaaaaa1.meta.json").write_text(json.dumps({"agentType": "reviewer", "toolUseId": "t1", "finished": True}))
    (folder / "run/subagents/agent-aaaaaa2.meta.json").write_text(json.dumps({"agentType": "reviewer", "toolUseId": "t2", "finished": False}))
    (folder / "run/subagents/agent-aaaaaa1.jsonl").write_text("\n".join([
        line("assistant", [{"type": "tool_use", "id": "s1", "name": "Bash", "input": {"description": "List logs"}}], isSidechain=True),
        line("user", [{"type": "tool_result", "tool_use_id": "s1", "content": [picture]}], isSidechain=True),
        line("assistant", [{"type": "text", "text": "Found it"}], isSidechain=True), ""]))
    state_dir = STATE
    (state_dir / "sessions" / f"{fake}.json").write_text(json.dumps({"id": fake, "project": NAME, "repo": "/x", "title": "fixture", "kind": "claude", "isolate": False, "created": time.time()}))
    (state_dir / "events" / f"{fake}.json").write_text(json.dumps({"state": "ended", "transcript": str(folder / "run.jsonl")}))
    try:
        crew = {a["what"]: a for a in call("GET", f"/api/sessions/{fake}/agents")[1]}
        done = crew.get("Check the logs", {})
        check("a finished subagent shows its report without harness notes", done.get("status") == "done" and done.get("result") == "All clear", str(done)[:300])
        check("a subagent's own tool calls are counted", done.get("tools") == 1 and done.get("said") == "Found it", str(done)[:300])
        check("a subagent can't be running in a session that ended", crew.get("Still going", {}).get("status") == "stopped", str(crew)[:300])
        time.sleep(2)  # the state snapshot is cached briefly
        row = session(fake)
        check("the session strip gets a trace with errors", row["trace"] == ["a", "a", "!"] and row["model"] == "sonnet" and row["said"] == "All done here" and row["agents"]["total"] == 2, str(row)[:300])
        media = call("GET", f"/api/sessions/{fake}/shared")[1]
        refs = {i.get("blob") for i in media}
        check("screenshots from tools and from subagents are shown", "3.0.0" in refs and "aaaaaa1:1.0.0" in refs, str(media)[:300])
        status, raw = call("GET", f"/api/sessions/{fake}/blob?ref=aaaaaa1%3A1.0.0")
        check("a subagent's screenshot opens", status == 200 and raw[:4] == b"\x89PNG", str(raw)[:80])
        check("an odd image reference is refused", call("GET", f"/api/sessions/{fake}/blob?ref=..%2F..%3A1.0.0")[0] == 409)
    finally:
        post(f"/api/sessions/{fake}/dismiss", {})
        shutil.rmtree(folder, ignore_errors=True)

    # agents you build
    agents_dir = HOME / "projects" / NAME / ".claude/agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / "pixel-judge.md").write_text("---\nname: pixel-judge\ndescription: Reviews screens\nmodel: haiku\n---\nYou judge pixels.\n")
    listed = {a["name"]: a for a in call("GET", f"/api/p/{NAME}/agents")[1]}
    check("an agent you built is listed", listed.get("pixel-judge", {}).get("description") == "Reviews screens" and "You judge pixels" in listed["pixel-judge"]["body"], str(listed)[:300])
    status, made = post("/api/sessions", {"project": NAME, "agent": "pixel-judge", "mode": "plan", "model": "haiku", "effort": "low"})
    sids += made["ids"]
    run_id = made["ids"][0]
    time.sleep(3)
    command = subprocess.run(TMUX + ["list-panes", "-t", f"hub-{run_id}", "-F", "#{pane_start_command}"], capture_output=True, text=True).stdout
    check("a session starts with the chosen mode, model, effort and agent", all(part in command for part in ("--permission-mode plan", "--model haiku", "--effort low", "--agent pixel-judge", "--remote-control")), command[:300])

    status, made = post("/api/sessions", {"project": NAME, "kind": "shell"})
    sids += made["ids"]
    time.sleep(1)
    check("a plain terminal starts", session(made["ids"][0])["state"] == "shell")
finally:
    try:
        for sid in sids:
            post(f"/api/sessions/{sid}/dismiss", {})
        remaining = [s for s in call("GET", "/api/state")[1]["sessions"] if s["id"] in sids]
        check("sessions clean up", not remaining, str(remaining))
    finally:  # whatever the server did or didn't answer
        left = remove_project(NAME)
        check("the throwaway project is gone, with what the kit and Claude kept for it", not left, str(left))
        check("launchd's folder holds what it held before, and no dream job of a test", launchd_jobs() == launchd_before, str(sorted(set(launchd_jobs()) ^ set(launchd_before))))

print(f"--- failures: {fails}")
sys.exit(1 if fails else 0)

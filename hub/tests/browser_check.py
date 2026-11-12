#!/usr/bin/env python3
"""Drives the real page in headless Chrome at the hub's own address, in real time, and saves screenshots.

Run: /usr/bin/python3 tests/browser_check.py <output-folder>
Runs against a second instance (the six HUB_* variables, see tests/instance.py), at the origin its config gives.
Makes a throwaway project, hub-test-throwaway-<n>, with three dreams made by the kit's own scripts (two applied, one to
review), starts two short sessions in it, and removes all of it. Prints what each page rendered.
"""
import base64, http.client, json, os, plistlib, shutil, socket, struct, subprocess, sys, tempfile, time, urllib.request
from pathlib import Path

from instance import CFG, DREAMER, HOME, KIT, NAME, PROPOSAL, SMALL, TMUX, WRITE as HEAD, answer_first_run, check_file, collect, in_repo, job_of, launchd_jobs, own_notes, screen as tmux_screen, seen as on_screen, quiet_alert, remove_project, sweep, tell, write_dream, write_job
from instance import PORT as HUB_PORT

OUT = Path(sys.argv[1]); OUT.mkdir(parents=True, exist_ok=True)
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PORT = int(os.environ.get("HUB_CDP_PORT", "9347"))  # Chrome's debugging port: give each test run beside another its own
TODAY = time.strftime("%Y-%m-%d")
NEED = "The key pasted into chat on Monday is still live. It needs rotating · evidence: a1b2c3d4"  # what needs the person, not memory
# The proposal every test reads, with the two sections only a full dream has: nine boxes to tick, eight of them numbered.
PROPOSAL = PROPOSAL.replace("{DAY}", TODAY).replace("{REST}", """
## Scratchpads to condense / clear
- [ ] main — keep: nothing. Clear.
""").replace("\n## Proposed changes", f"\n## Needs you\n- {NEED}\n\n## Proposed changes")
HELD = "not due: the last try on 8 Nov failed; next try from 11 Nov"  # the kit's words when a try wrote no proposal
SKIPPED = "due: 12 memory lines since 8 Nov; 3 changelog rows skipped (time unreadable)"


def api(method, path, body=None):
    c = http.client.HTTPConnection("127.0.0.1", HUB_PORT, timeout=60)
    c.request(method, path, json.dumps(body) if body is not None else None, HEAD)
    return json.loads(c.getresponse().read())


class CDP:
    def __init__(self, url):
        rest = url.split("//")[1]
        hostport, path = rest.split("/", 1)
        host, port = hostport.split(":")
        self.s = socket.create_connection((host, int(port)), timeout=30)
        key = base64.b64encode(os.urandom(16)).decode()
        self.s.sendall(f"GET /{path} HTTP/1.1\r\nHost: {hostport}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n".encode())
        data = b""
        while b"\r\n\r\n" not in data:
            data += self.s.recv(4096)
        self.buffer = data.split(b"\r\n\r\n", 1)[1]
        self.n = 0
        self.errors = []

    def _read(self, n):
        while len(self.buffer) < n:
            chunk = self.s.recv(1 << 20)
            if not chunk:
                raise ConnectionError
            self.buffer += chunk
        out, self.buffer = self.buffer[:n], self.buffer[n:]
        return out

    def recv(self):
        data = b""
        while True:
            a, b = self._read(2)
            size = b & 0x7F
            if size == 126:
                size = struct.unpack(">H", self._read(2))[0]
            elif size == 127:
                size = struct.unpack(">Q", self._read(8))[0]
            data += self._read(size)
            if a & 0x80:
                return json.loads(data)

    def call(self, method, **params):
        self.n += 1
        payload, mask = json.dumps({"id": self.n, "method": method, "params": params}).encode(), os.urandom(4)
        size = len(payload)
        head = bytes([0x81]) + (bytes([0x80 | size]) if size < 126 else bytes([0x80 | 126]) + struct.pack(">H", size) if size < 65536 else bytes([0x80 | 127]) + struct.pack(">Q", size))
        self.s.sendall(head + mask + bytes(c ^ mask[i % 4] for i, c in enumerate(payload)))
        while True:
            message = self.recv()
            if message.get("id") == self.n:
                return message.get("result", {})
            if message.get("method") == "Runtime.exceptionThrown":
                self.errors.append(message["params"]["exceptionDetails"].get("text", "") + " " + str(message["params"]["exceptionDetails"].get("exception", {}).get("description", ""))[:200])
            if message.get("method") == "Log.entryAdded" and message["params"]["entry"]["level"] == "error":
                self.errors.append(message["params"]["entry"]["text"][:200])

    def js(self, expression):
        return self.call("Runtime.evaluate", expression=expression, awaitPromise=True, returnByValue=True).get("result", {}).get("value")

    def shot(self, name):
        data = self.call("Page.captureScreenshot", format="png")["data"]
        (OUT / f"{name}.png").write_bytes(base64.b64decode(data))

    def size(self, width, height, mobile=False):
        self.call("Emulation.setDeviceMetricsOverride", width=width, height=height, deviceScaleFactor=1, mobile=mobile)

    def go(self, route, wait=2.5):
        self.js(f"location.hash = {json.dumps(route)}")
        time.sleep(wait)


sweep()
launchd_before = launchd_jobs()
# The first dream: a line that is ticked, one that is passed over (twice, so the kit declines it), a line added and then
# edited (for the change log), a ticked line that isn't written as an item, an ORG.md change, and a scratchpad to clear.
YOURS = "- Throwaway principle from the browser test."
OLD, NEW = "- [2026-11-08] A line that is then edited → old", "- [2026-11-08] A line that is then edited → new"
TWO = SMALL.replace("- [ ] 1.", "- [x] 1.").replace("{LINE}", "- [2026-11-08] Ticked both times → applied").replace("\n## Trend", '- [ ] 2. project/gotchas.md — add — "- [2026-11-08] Passed over both times → declined"\n      why: test · evidence: none\n\n## Trend')
FIRST = TWO.replace("\n## Trend", f'- [x] 3. project/gotchas.md — add — "{OLD}"\n      why: test · evidence: none\n- [x] 4. project/gotchas.md — edit — "{NEW}"\n      why: test · evidence: none\n      replaces: "{OLD}"\n'
                    '* [x] 5. project/gotchas.md — add — "- written with a star, so it is not an item"\n\n'
                    f'## ORG.md and Steering (yours to change)\n- [x] 6. ORG.md — add — "{YOURS}"\n      why: test · evidence: none\n\n## Scratchpads to condense / clear\n- [x] helper — keep: nothing. Clear.\n\n## Trend')
profile = tempfile.mkdtemp(prefix="hub-chrome.")
chrome = subprocess.Popen([CHROME, "--headless=new", "--disable-gpu", f"--user-data-dir={profile}", f"--remote-debugging-port={PORT}", "--window-size=1440,900", "about:blank"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
project, repo, store = NAME, HOME / "projects" / NAME, HOME / "agent-memory" / NAME
ids = []
failures = 0
location_is = lambda c, tail: c.js("location.hash").endswith(tail)


def check(label, ok, extra=""):
    global failures
    print(("PASS  " if ok else "FAIL  ") + label + (f"  [{extra}]" if not ok else ""), flush=True)
    failures += 0 if ok else 1


try:
    made = api("POST", "/api/projects", {"name": NAME})
    check("a throwaway project is created", made.get("name") == NAME, str(made))
    quiet_alert(NAME)
    # What the pages have to show: a daily dream job, the kit's last verdict, notes of Claude's own, and a dream to review.
    write_job(NAME, repo, 7)  # a daily job as the kit writes it, in this instance's own folder
    hour = 7
    (store / "dreams" / "2026-01-01-proposal.md").write_text(SMALL.replace("{DAY}", "2026-01-01").replace("{LINE}", "- [2026-01-01] From before the kit kept a record"))
    subprocess.run(["git", "-C", str(store), "add", "dreams/2026-01-01-proposal.md"], check=True)
    subprocess.run(["git", "-C", str(store), "commit", "-q", "-m", "dream 2026-01-01: an old proposal", "--", "dreams/2026-01-01-proposal.md"], check=True)
    for text in (FIRST, TWO):  # two dreams by the kit's own scripts, each applied by its dream_apply.py and closed; what the first approved for the person and for a session then waits
        write_dream(repo, text)
        did = in_repo(repo, "dream_apply.py")
        closed = in_repo(repo, "dream_proposal.py", "applied")
        check("the kit applies and closes an earlier dream", did.returncode == 0 and closed.returncode == 0, did.stdout + did.stderr + closed.stdout + closed.stderr)
    open_name = write_dream(repo, PROPOSAL)  # the third, open: the one to review
    now = int(time.time())
    check_file(NAME).write_text(json.dumps({"last_dream": now - 3 * 86400, "last_reason": "27 sessions since 8 Nov", "checked": now - 7200, "verdict": HELD, "tries": [{"at": now - 4 * 86400, "ended": "proposal"}, {"at": now - 86400, "ended": "failed"}], "reminded": None}))
    own_notes(repo).mkdir(parents=True)
    (own_notes(repo) / "feedback_tests.md").write_text("Run the tests before saying it works.\n")
    org = api("GET", f"/api/p/{NAME}/file?path=ORG.md")
    api("POST", f"/api/p/{NAME}/file", {"path": "ORG.md", "content": org["content"] + "\n- Jobs that must outlive a session run under launchd.", "expect": org["hash"]})
    task = {"project": project, "isolate": False, "mode": "auto", "model": "haiku", "effort": "low"}
    ids += api("POST", "/api/sessions", {**task, "tasks": ["Reply with the single word ready"]})["ids"]
    answer_first_run(ids[0])  # a new folder asks to be trusted once; the second session starts after that
    ids += api("POST", "/api/sessions", {**task, "tasks": ["Reply with the single word steady"]})["ids"]
    for _ in range(40):
        try:
            targets = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json"))
            page = next(t for t in targets if t["type"] == "page")
            break
        except (OSError, StopIteration):
            time.sleep(0.25)
    time.sleep(16)
    c = CDP(page["webSocketDebuggerUrl"])
    c.call("Page.enable"); c.call("Runtime.enable"); c.call("Log.enable"); c.call("Network.enable")
    c.call("Network.setExtraHTTPHeaders", headers={"Tailscale-User-Login": CFG["login"]})  # what Tailscale Serve adds on the way in
    c.size(1440, 900)
    c.call("Page.navigate", url=CFG["origin"] + "/#/")
    time.sleep(4)
    body = lambda: c.js("document.body.innerText")
    drawn = lambda what, seconds=30: next((True for _ in range(seconds * 4) if c.js(what) or time.sleep(0.25)), False)  # wait for what a page shows, however long its data takes
    drawn("document.querySelectorAll('.bar-col').length === 14")
    text = body()
    check("the platform home renders", "Projects" in text and "Needs you" in text and "Working" in text and "lessons" in text, text[:300])
    check("the platform home is a dashboard with the left sidebar, led by the queue", c.js("getComputedStyle(document.querySelector('#side')).display") != "none" and c.js("[...document.querySelectorAll('.qtab')].map(b => b.firstChild.textContent).join()") == "Needs you,Working,Done"
          and c.js("document.querySelector('.dash > .panel').classList.contains('triage')") and not c.js("!!document.querySelector('.kpi')") and c.js("document.querySelectorAll('.bar-col').length") == 14)
    queue_row = lambda nav: c.js(f"(() => {{ const r = document.querySelector('.qrow[data-nav=\"{nav}\"]'); return r ? [r.querySelector('.q-t').textContent, r.querySelector('.q-t').getAttribute('href'), (r.querySelector('.q-p') || {{}}).textContent || ''] : null; }})()")
    check("what needs you leads, and a zero never does", c.js("document.querySelector('.qtab.on').id") == "qt-needs" and c.js("document.querySelector('.qtab.on b').textContent") != "0")
    check("the platform home has a card per project", c.js("document.querySelectorAll('a.card').length") >= 2)
    check("no stray null or object text", "null" not in text.split() and "[object" not in text and "https://" not in text, text[:300])
    check("an approved change that waits for the person is said on Home, with a way to it", queue_row(f"w:{NAME}") == ["1 approved change waits for you", f"#/p/{NAME}/dreams", NAME], str(queue_row(f"w:{NAME}")))
    check("the review count matches the proposal", queue_row(f"r:{NAME}") == ["Review 8 memory changes", f"#/p/{NAME}/dreams", NAME], str(queue_row(f"r:{NAME}")))
    # Done: a finished turn is unread until it is opened or marked read here; read is kept in this browser
    c.js("document.activeElement.blur()")
    c.call("Input.dispatchKeyEvent", type="keyDown", key="3", code="Digit3", windowsVirtualKeyCode=51, text="3"); c.call("Input.dispatchKeyEvent", type="keyUp", key="3", code="Digit3", windowsVirtualKeyCode=51)
    time.sleep(0.4)
    unread = lambda: int(c.js("document.querySelector('#qt-done b').textContent"))
    before = unread()
    check("the 3 key shows Done, with the finished sessions unread", c.js("document.querySelector('.qtab.on').id") == "qt-done" and before >= 1 and c.js("document.querySelectorAll('.qrow:not(.read)').length") == before, str(before))
    for name, code, vk in (("j", "KeyJ", 74), ("e", "KeyE", 69)):
        c.call("Input.dispatchKeyEvent", type="keyDown", key=name, code=code, windowsVirtualKeyCode=vk, text=name); c.call("Input.dispatchKeyEvent", type="keyUp", key=name, code=code, windowsVirtualKeyCode=vk)
        time.sleep(0.3)
    check("e marks the selected one read, and Done counts one fewer", unread() == before - 1 and c.js("document.querySelectorAll('.qrow.read').length") == 1, str(unread()))
    c.call("Page.reload"); time.sleep(4)
    c.js("document.querySelector('#qt-done').click()"); time.sleep(0.3)
    check("read is kept in this browser across a reload", unread() == before - 1 and c.js("document.querySelectorAll('.qrow.read').length") == 1, str(unread()))
    c.js("document.querySelector('.qrow.read .q-act').click()"); time.sleep(0.3)
    check("Mark unread puts it back", unread() == before, str(unread()))
    c.shot("platform")

    c.go(f"#/p/{project}", 3.5)
    drawn("document.querySelectorAll('.bar-col').length === 14")
    text = body()
    check("the project has a homepage", "Start" in text and "Activity" in text and "Repositories" in text and "Agents" in text and "null" not in text.split(), text[:400])
    no_right = "!document.querySelector('.shared') && Math.max(...[...document.querySelectorAll('.dash > .panel')].map(p => p.getBoundingClientRect().right)) > innerWidth - 32 && Math.min(...[...document.querySelectorAll('.dash > .panel')].map(p => p.getBoundingClientRect().left)) < 260"
    check("the project dashboard keeps the left sidebar and has no right column", c.js("getComputedStyle(document.querySelector('#side')).display") != "none" and c.js(no_right))
    check("the project dashboard has its tabs and charts, and no number tiles", drawn("document.querySelectorAll('#top .ptab').length === 3 && document.querySelectorAll('.bar-col').length === 14") and not c.js("!!document.querySelector('.kpi')"))
    check("the charts carry real numbers", c.js("[...document.querySelectorAll('.bar-col')].some(b => !b.dataset.tip.endsWith(': 0'))"))
    side = c.js("document.querySelector('#side').textContent")
    check("the dropdown holds sessions only", "Memory" not in side and "Dreams" not in side, side[:200])
    check("memory and dreams open from the project's tabs, and the page says which tab it is", c.js("[...document.querySelectorAll('#top .ptab')].map(a => a.firstChild.textContent + ' ' + a.getAttribute('href').split('/').slice(3).join('/')).join()") == "Sessions ,Memory memory,Dreams dreams"
          and c.js("document.querySelector('#top .ptab.on').getAttribute('aria-current')") == "page" and c.js("document.querySelector('#top .ptab.on').textContent").startswith("Sessions"))
    check("sidebar icons stay small", c.js("Math.max(...[...document.querySelectorAll('#side svg')].map(s => s.getBoundingClientRect().width))") <= 20)
    check("the menu button is hidden on a desktop", c.js("getComputedStyle(document.querySelector('.menu-btn')).display") == "none")
    check("session strips say mode and model, not the project", c.js("[...document.querySelectorAll('.board .strip-facts')].every(e => !e.querySelector('.mark') && e.textContent.includes('Auto') && e.textContent.includes('Haiku 5.5'))"))
    check("a project's agents are listed", "transcript-analyst" in c.js("document.querySelector('.rail').innerText"))
    check("the project page composer offers mode and model", c.js("[...document.querySelectorAll('.composer .pill')].map(b => b.title).join()").startswith("Mode,Model"))
    check("the prompt docks under the board", c.js("document.querySelector('.dock').getBoundingClientRect().top > document.querySelector('.board').getBoundingClientRect().top"))
    check("each project has a generated glyph, not a letter", c.js("[...document.querySelectorAll('#side .mark')].every(m => m.querySelector('svg rect') && !m.textContent)"))
    check("glyphs differ between projects", c.js("new Set([...document.querySelectorAll('#side a.nav:not(.session) .mark')].map(m => m.innerHTML)).size") >= 2)
    check("the status line shows this page's keys", all(k in c.js("document.querySelector('#status').innerText") for k in ("memory", "dreams", "prompt")))
    c.js("document.activeElement.blur()")
    key = lambda name, code, vk: (c.call("Input.dispatchKeyEvent", type="keyDown", key=name, code=code, windowsVirtualKeyCode=vk, text=name if len(name) == 1 else ""), c.call("Input.dispatchKeyEvent", type="keyUp", key=name, code=code, windowsVirtualKeyCode=vk))
    key("j", "KeyJ", 74); time.sleep(0.2)
    check("j selects the first row", c.js("document.querySelectorAll('[data-nav].sel').length") == 1)
    key("?", "Slash", 191); time.sleep(0.3)
    check("? opens the keyboard sheet", c.js("!!document.querySelector('dialog.keys-help')"))
    c.js("document.querySelector('dialog.keys-help').close()"); time.sleep(0.2)
    key("m", "KeyM", 77); time.sleep(1.5)
    check("m opens Memory", location_is(c, "/memory"))
    key("Escape", "Escape", 27); time.sleep(1.5)
    check("esc goes back up", c.js("location.hash").endswith(project))
    c.js("document.querySelector('.composer .pill[title=Mode]').click()")
    time.sleep(0.4)
    check("the mode menu has the four modes", c.js("document.querySelectorAll('.popup.menu button').length") == 4 and "Accept edits" in c.js("document.querySelector('.popup.menu').innerText"))
    c.call("Input.dispatchKeyEvent", type="keyDown", key="Escape", code="Escape", windowsVirtualKeyCode=27)
    time.sleep(0.3)
    check("Escape closes the menu", not c.js("!!document.querySelector('.popup.menu')"))
    check("sessions are nested under the project", c.js("document.querySelectorAll('#side a.nav.session').length") == 2)
    c.shot("home")

    check("nothing has rounded corners", c.js("[...document.querySelectorAll('button, a, .composer *, .door, .strip')].every(e => getComputedStyle(e).borderTopLeftRadius === '0px')"))
    box = c.js("(() => { const r = document.querySelector('.composer .pill').getBoundingClientRect(); return [r.left + r.width / 2, r.top + r.height / 2]; })()")
    c.call("Input.dispatchMouseEvent", type="mouseMoved", x=box[0], y=box[1])
    time.sleep(0.4)
    check("a button fills with white on hover", c.js("(() => { const s = getComputedStyle(document.querySelector('.composer .pill')); return s.backgroundSize.startsWith('100%') && s.color === 'rgb(11, 16, 32)'; })()"))
    check("the main button is plain cobalt", c.js("getComputedStyle(document.querySelector('.start')).backgroundColor") == "rgb(47, 98, 245)")
    c.call("Input.dispatchMouseEvent", type="mouseMoved", x=2, y=2)
    c.go(f"#/p/{project}/new", 2.5)
    text = body()
    check("New session asks for the mode and the model", c.js("document.querySelectorAll('.tiles').length") == 2 and c.js("document.querySelectorAll('.tile').length") == 8 and "Ask first" in text and "Fable 5.1" in text, text[:300])
    c.js("[...document.querySelectorAll('.tile')].find(t => t.textContent.startsWith('Plan')).click(); [...document.querySelectorAll('.tile')].find(t => t.textContent.startsWith('Sonnet')).click()")
    time.sleep(0.3)
    check("the command shows what will run", c.js("document.querySelector('.command').textContent") == "claude --permission-mode plan --model sonnet", c.js("document.querySelector('.command').textContent"))
    c.js("[...document.querySelectorAll('.tile')].find(t => t.textContent.startsWith('Auto')).click(); [...document.querySelectorAll('.tile')].find(t => t.textContent.startsWith('Opus')).click()")
    c.js("(() => { const t = document.querySelector('.composer textarea'); t.value = 'Why does this fail'; t.dispatchEvent(new Event('input')); })()")
    time.sleep(0.3)
    check("a question suggests Plan", "Plan keeps it read-only" in c.js("document.querySelector('.note').innerText"))
    c.js("[...document.querySelectorAll('.quiet')].find(b => b.textContent.includes('Add another agent')).click()")
    time.sleep(0.2)
    c.js("(() => { const t = document.querySelectorAll('.composer textarea')[1]; t.value = 'Second job'; t.dispatchEvent(new Event('input')); })()")
    time.sleep(0.3)
    check("several agents get worktrees", c.js("document.querySelectorAll('.prompt-row').length") == 2 and c.js("document.querySelector('.choice .check input').checked"))
    c.shot("new-session")

    # the session gets its own page; a link, image and audio go through it
    PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
    raw = {**HEAD, "Content-Type": "application/octet-stream"}
    def up(name, data):
        conn = http.client.HTTPConnection("127.0.0.1", HUB_PORT, timeout=30)
        conn.request("POST", f"/api/sessions/{ids[0]}/upload?name={name}", data, raw)
        return json.loads(conn.getresponse().read())
    image = up("shot.png", base64.b64decode(PNG))
    sound = up("note.wav", b"RIFF$\x00\x00\x00WAVEfmt " + b"\x00" * 28)
    c.go(f"#/s/{ids[0]}", 4)
    check("a session opens on its own page", c.js("document.querySelectorAll('.pane').length") == 1 and "Activity" not in body())
    screen = lambda i=0: c.js(f"document.querySelectorAll('.pane')[{i}].querySelector('.xterm-rows').innerText") or ""
    check("the terminal draws Claude", "ready" in screen(0) or "steady" in screen(0), repr(screen(0)[:200]))
    check("a running session can change mode and model", c.js("[...document.querySelectorAll('.pane-top .pill')].map(b => b.title).join()") == "Change mode,Change model for this session")
    check("each session shows a phone link", c.js("[...document.querySelectorAll('.pane-top a.icon')].every(a => a.href.startsWith('https://claude.ai/code/session_'))") and c.js("document.querySelectorAll('.pane-top a.icon').length") == 1)
    tmux = TMUX
    prompt = f"Reply with exactly these three lines and nothing else: https://example.com/report , {image['path']} , {sound['path']}"
    tell(ids[0], prompt)
    # Nothing is clicked until the reply is whole: before that the last place a path is on screen is the prompt itself,
    # and the rows move under the pointer as the reply arrives.
    replied = lambda: next((x for x in api("GET", "/api/state")["sessions"] if x["id"] == ids[0] and x["state"] == "finished" and "example.com/report" in (x.get("said") or "")), None)
    answered = next((True for _ in range(120) if replied() or time.sleep(1)), False)
    check("the session has replied before anything on its screen is clicked", answered, repr(api("GET", "/api/state")["sessions"])[:400])
    steady = lambda: next((True for _ in range(60) if (lambda a: time.sleep(0.5) or a == tmux_screen(ids[0]))(tmux_screen(ids[0]))), False)
    check("its screen has stopped moving", on_screen(ids[0], image["path"].split("/")[-1][:12]) and steady(), tmux_screen(ids[0])[-300:])
    c.js("window.__opened = []; window.open = (u) => { window.__opened.push(u); return null; }")
    hovered = []
    def click_text(needle):
        """Finds the on-screen row holding the text, then does a real mouse hover and click on it."""
        at = c.js("""(() => { const t = document.querySelector('.pane .xterm-screen').getBoundingClientRect();
            const rows = [...document.querySelector('.pane').querySelectorAll('.xterm-rows > div')];
            const needle = %s; let i = rows.length - 1;
            for (; i >= 0; i--) { const pos = rows[i].innerText.lastIndexOf(needle); if (pos >= 0) {
              const cw = t.width / document.querySelector('.pane').__cols; const rh = t.height / rows.length;
              return [t.left + (pos + 3) * cw, t.top + (i + 0.5) * rh]; } }
            return null; })()""" % json.dumps(needle))
        if not at:
            return False
        c.call("Input.dispatchMouseEvent", type="mouseMoved", x=at[0] - 1, y=at[1])
        c.call("Input.dispatchMouseEvent", type="mouseMoved", x=at[0], y=at[1])
        time.sleep(0.5)
        hovered.append(c.js("!!document.querySelector('.hover-card img')"))
        c.call("Input.dispatchMouseEvent", type="mousePressed", x=at[0], y=at[1], button="left", clickCount=1)
        c.call("Input.dispatchMouseEvent", type="mouseReleased", x=at[0], y=at[1], button="left", clickCount=1)
        time.sleep(1.2)
        return True
    c.js("document.querySelector('.pane').__cols = Number(new URLSearchParams(1) && 0) || (document.querySelector('.pane .xterm-rows > div').innerText.length, Math.round(document.querySelector('.pane .xterm-screen').getBoundingClientRect().width / (parseFloat(getComputedStyle(document.querySelector('.pane .xterm-rows > div span') || document.body).width) || 7.8)))")
    cols = subprocess.run(tmux + ["display-message", "-p", "-t", f"hub-{ids[0]}", "#{window_width}"], capture_output=True, text=True).stdout.strip()
    c.js(f"document.querySelector('.pane').__cols = {int(cols)}")
    check("the link is on screen", click_text("example.com/report"), repr(screen(0)[-400:]))
    check("clicking a link opens it", c.js("window.__opened.some(u => u.startsWith('https://example.com/report'))"), str(c.js("window.__opened")))
    time.sleep(0.3)
    image_name = image["path"].split("/")[-1]
    check("the image path is on screen", click_text(image_name[:12]), repr(screen(0)[-400:]))
    rows_now = lambda: f"cols {cols}; rows: " + repr(c.js("[...document.querySelector('.pane').querySelectorAll('.xterm-rows > div')].map(r => r.innerText).filter(t => t.trim())"))  # for a failure: exactly what was on screen
    check("hovering an image path shows the image", hovered[-1], rows_now())
    check("clicking an image previews it", c.js("!!document.querySelector('dialog.preview img')"))
    time.sleep(0.6)
    check("the viewer says the image's name, size and time", c.js("document.querySelector('.v-title b').textContent") == image_name and "1×1" in (c.js("document.querySelector('.v-meta').textContent") or ""), str(c.js("document.querySelector('.v-top').innerText")))
    check("the viewer fills the screen", c.js("(() => { const r = document.querySelector('dialog.viewer').getBoundingClientRect(); return r.width === innerWidth && r.height === innerHeight; })()"))
    c.js("document.querySelector('dialog.viewer').dispatchEvent(new KeyboardEvent('keydown', {key: '+', bubbles: true}))")
    time.sleep(0.4)
    check("+ zooms in", c.js("document.querySelector('.v-pct').textContent") == "150%", c.js("document.querySelector('.v-pct').textContent"))
    c.js("document.querySelector('dialog.viewer').dispatchEvent(new KeyboardEvent('keydown', {key: '0', bubbles: true}))")
    time.sleep(0.4)
    check("0 fits it again", c.js("document.querySelector('.v-pct').textContent") == "100%", c.js("document.querySelector('.v-pct').textContent"))
    c.shot("preview-image")
    c.js("document.querySelector('dialog.preview') && document.querySelector('dialog.preview').close()")
    time.sleep(0.3)
    sound_name = sound["path"].split("/")[-1]
    click_text(sound_name[:12])
    check("clicking audio opens a player", c.js("!!document.querySelector('dialog.preview audio')"))
    c.js("document.querySelector('dialog.preview') && document.querySelector('dialog.preview').close()")
    time.sleep(0.3)
    # Two paths on rows of their own, the first reaching almost to the terminal's edge: each is its own link. A row that
    # nearly fills the width looks like one that was wrapped, and a path that ends there must not be joined to the next row's.
    plain = api("POST", "/api/sessions", {"project": project, "kind": "shell"})["ids"][0]
    ids.append(plain)
    def up_to(sid, name, data):
        conn = http.client.HTTPConnection("127.0.0.1", HUB_PORT, timeout=30)
        conn.request("POST", f"/api/sessions/{sid}/upload?name={name}", data, raw)
        return json.loads(conn.getresponse().read())
    near, after = up_to(plain, "edge.png", base64.b64decode(PNG))["path"], up_to(plain, "next.wav", b"RIFF$\x00\x00\x00WAVEfmt " + b"\x00" * 28)["path"]
    c.go(f"#/s/{plain}", 4)
    wide = int(subprocess.run(tmux + ["display-message", "-p", "-t", f"hub-{plain}", "#{window_width}"], capture_output=True, text=True).stdout.strip())
    pad = max(0, wide - 3 - len(near))  # the first row ends three columns short of the edge, or, if the path is longer than the terminal, is wrapped by it
    subprocess.run(tmux + ["send-keys", "-t", f"hub-{plain}", "-l", f"clear; printf '%s\\n%s\\n' '{' ' * pad}{near}' '{after}'"])
    subprocess.run(tmux + ["send-keys", "-t", f"hub-{plain}", "Enter"])
    check("two long paths are printed on rows of their own", on_screen(plain, after.split("/")[-1]) and "printf" not in tmux_screen(plain), tmux_screen(plain)[:400])
    time.sleep(1)
    c.js(f"document.querySelector('.pane').__cols = {wide}")
    check("the first path is on screen", click_text(near.split("/")[-1][:10]), repr(screen(0)[:400]))
    check("a path that ends near the terminal's edge opens by itself, not glued to the path on the next row", c.js("!!document.querySelector('dialog.preview img')"), f"cols {wide}, path {len(near)}")
    c.js("document.querySelector('dialog.preview') && document.querySelector('dialog.preview').close()")
    time.sleep(0.3)
    click_text(after.split("/")[-1][:10])
    check("and so does the path on the row after it", c.js("!!document.querySelector('dialog.preview audio')"))
    c.js("document.querySelector('dialog.preview') && document.querySelector('dialog.preview').close()")
    time.sleep(0.3)
    # Delete asks first, in the hub's own dialog: running now, what goes to the Trash, Keep in focus, Esc keeps it
    c.js(f"deleteSession(session('{plain}'))")
    time.sleep(1.5)
    said = c.js("(() => { const d = document.querySelector('dialog.delete-sheet'); return d ? [d.innerText, document.activeElement.textContent, d.querySelector('.btn.danger').textContent] : null; })()")
    check("Delete opens a dialog that says it runs, that its files go to the Trash, with Keep in focus", said and "running now" in said[0] and "Trash" in said[0] and "2 files you shared" in said[0] and said[1] == "Keep session" and said[2] == "Delete session", str(said))
    c.call("Input.dispatchKeyEvent", type="keyDown", key="Escape", code="Escape", windowsVirtualKeyCode=27)
    c.call("Input.dispatchKeyEvent", type="keyUp", key="Escape", code="Escape", windowsVirtualKeyCode=27)
    time.sleep(0.5)
    check("Esc closes it and nothing is deleted", not c.js("!!document.querySelector('dialog.delete-sheet')") and any(x["id"] == plain for x in api("GET", "/api/state")["sessions"]))
    c.js(f"deleteSession(session('{plain}'))")
    time.sleep(1.5)
    c.js("document.querySelector('dialog.delete-sheet .btn.danger').click()")
    gone = next((True for _ in range(40) if not any(x["id"] == plain for x in api("GET", "/api/state")["sessions"]) or time.sleep(0.25)), False)
    time.sleep(1)
    toast_text = c.js("document.getElementById('toast').innerText")
    check("Delete session stops it, removes it from the list without a reload, and says it is in the Trash", gone and "Trash" in toast_text and c.js(f"!document.querySelector('[data-sid=\"{plain}\"]')"), toast_text)
    trashed = next(iter(sorted((HOME / ".Trash").glob(f"Dream Hub session * {plain} *"))), None)
    check("its uploads are in one folder in the Trash", trashed is not None and (trashed / "uploads").is_dir(), str(trashed))
    if trashed is not None:
        shutil.rmtree(trashed)  # the test's own files: nothing of the person's is in it
    c.js("performance.clearResourceTimings()")  # what came before was asked for by a hover in the terminal
    c.go(f"#/s/{ids[0]}", 4)
    cols = subprocess.run(tmux + ["display-message", "-p", "-t", f"hub-{ids[0]}", "#{window_width}"], capture_output=True, text=True).stdout.strip()
    c.js(f"document.querySelector('.pane').__cols = {int(cols)}")
    if not c.js("!!document.querySelector('.shared')"):
        c.js("document.querySelector('.actions-top .icon[aria-pressed]').click()")
    time.sleep(5)
    text = c.js("document.querySelector('.shared').innerText")
    check("a working session keeps its right panel", c.js("!!document.querySelector('.shared')"))
    check("the Shared panel lists all three on its timeline", "example.com" in c.js("(document.querySelector('.shared .t-row[data-kind=link]') || {}).innerText || ''")
          and c.js("!!document.querySelector('.shared .thumb') && !!document.querySelector('.shared .t-row[data-kind=audio]') && !!document.querySelector('.shared .t-at')"), text[:300])
    check("the Shared panel ignores session context", "build_index.py" not in text and "supabase.com" not in text, text[:300])
    check("the Shared image loads", c.js("(() => { const i = document.querySelector('.shared .thumb img'); return i && i.complete && i.naturalWidth > 0; })()"))
    check("listing a link fetches nothing from it", not c.js("performance.getEntriesByType('resource').some(e => e.name.includes('/api/preview/link'))"))
    c.shot("session-shared")
    at = c.js("(() => { const r = document.querySelector('.shared .t-row[data-kind=link]').getBoundingClientRect(); return [r.left + 40, r.top + r.height / 2]; })()")
    c.call("Input.dispatchMouseEvent", type="mouseMoved", x=at[0] - 2, y=at[1])
    c.call("Input.dispatchMouseEvent", type="mouseMoved", x=at[0], y=at[1])
    card = next((True for _ in range(16) if c.js("!!document.querySelector('.hover-card.link-card') && !document.querySelector('.lc-desc.quiet')") or time.sleep(0.25)), False)
    check("resting on a link shows the card the hub fetched for it, or why there is none", card, str(c.js("(document.querySelector('.hover-card') || {}).innerText")))
    c.shot("session-link-card")
    c.call("Input.dispatchMouseEvent", type="mouseMoved", x=10, y=10)
    time.sleep(0.2)
    check("the card goes when the pointer leaves", not c.js("!!document.querySelector('.hover-card')"))
    c.js("document.querySelector('.shared .t-row[data-kind=audio]').click()")
    time.sleep(1)
    check("audio from the timeline plays in the viewer", c.js("!!document.querySelector('dialog.viewer audio')"))
    c.js("document.querySelector('dialog.preview') && document.querySelector('dialog.preview').close()")

    # live status: the pane's strip, the right panel's rail, a review of the folder's changes, and a peek
    check("the pane has a strip that says the session is idle", c.js("(() => { const s = document.querySelector('.pane .pane-strip'); return s && !s.hidden && s.innerText.includes('Idle'); })()"), c.js("(document.querySelector('.pane-strip') || {}).outerHTML"))
    check("one status mark per pane: the header's dot stands down for the strip's orb", c.js("getComputedStyle(document.querySelector('.pane-top > .dot')).display") == "none")
    (repo / "browser-review.md").write_text("one\ntwo\n")
    rail = lambda: c.js("(document.querySelector('.rail') || {}).innerText || ''")
    seen_rail = next((r for _ in range(40) if "files" in (r := rail()) or time.sleep(1)), rail())
    check("the right panel says what changed and how full the context is", "Changes" in seen_rail and "files" in seen_rail and "Context" in seen_rail and "%" in seen_rail, seen_rail)
    c.js("document.querySelector('.rail-link').click()")
    time.sleep(2)
    check("Changes open as a review over the session, one section per file", c.js("document.querySelectorAll('.review .review-file').length") >= 1 and "browser-review.md" in (c.js("document.querySelector('.review-files').innerText") or ""))
    c.js("(() => { const f = [...document.querySelectorAll('.review-file')].find(s => s.getAttribute('aria-label') === 'browser-review.md'); f.querySelector('.dl .ln').click(); document.querySelector('.line-note-edit textarea').value = 'A line comment'; document.querySelector('.line-note-edit .btn.primary').click(); })()")
    check("a line comment is kept and counted for sending", c.js("document.querySelector('.review-top .btn.primary').textContent") == "Send 1 comment" and c.js("document.querySelectorAll('.line-note').length") == 1)
    c.shot("session-review")
    c.js("document.querySelector('.review-diff').dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}))")
    time.sleep(0.4)
    check("Esc closes the review and stays on the session", not c.js("!!document.querySelector('.review')") and c.js("location.hash") == f"#/s/{ids[0]}", c.js("location.hash"))
    (repo / "browser-review.md").unlink()
    c.js(f"window.hubPeek('{ids[0]}')")
    time.sleep(2)
    peeked = c.js("(document.querySelector('.peek-pop') || {}).innerText || ''")
    check("peek shows the last reply beside the sidebar row, with a reply box", "example.com/report" in peeked and c.js("!!document.querySelector('.peek-pop .peek-reply textarea')") and c.js("document.querySelector('.peek-pop').getBoundingClientRect().left") >= c.js("document.querySelector('#side').getBoundingClientRect().right"), peeked[:300])
    c.shot("session-peek")
    c.js("document.activeElement.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}))")
    time.sleep(0.3)
    check("Esc closes the peek", not c.js("!!document.querySelector('.peek-pop')"))

    c.js("document.querySelector('.pane .xterm-helper-textarea').focus()")
    c.call("Input.insertText", text="hello from the browser")
    time.sleep(1.5)
    check("typing appears in the terminal", "hello from the browser" in screen(0), repr(screen(0)[-300:]))
    c.call("Input.dispatchKeyEvent", type="keyDown", key="u", code="KeyU", windowsVirtualKeyCode=85, modifiers=2, text="\x15", unmodifiedText="u")
    c.call("Input.dispatchKeyEvent", type="keyUp", key="u", code="KeyU", windowsVirtualKeyCode=85, modifiers=2)

    c.go(f"#/s/{ids[0]},{ids[1]}", 4.5)
    check("two sessions open in split view", c.js("document.querySelectorAll('.pane').length") == 2 and c.js("!!document.querySelector('.gutter')"))
    check("both terminals draw", bool(screen(0).strip()) and bool(screen(1).strip()))
    cols = subprocess.run(tmux + ["display-message", "-p", "-t", f"hub-{ids[0]}", "#{window_width}"], capture_output=True, text=True).stdout.strip()
    c.js(f"document.querySelector('.pane').__cols = {int(cols)}")
    tail_of_name = sound_name[-9:]
    check("a wrapped audio path is on screen", click_text(tail_of_name), repr(screen(0)[-300:]))
    check("clicking a path wrapped across rows still opens it", c.js("!!document.querySelector('dialog.preview audio')"))
    c.js("document.querySelector('dialog.preview') && document.querySelector('dialog.preview').close()")
    time.sleep(0.3)
    c.shot("split")

    c.js("window.dispatchEvent(new KeyboardEvent('keydown', {key: 'k', metaKey: true, bubbles: true}))")
    time.sleep(0.4)
    check("command-K opens the jump list", c.js("document.querySelectorAll('dialog.palette .opt').length") > 3)
    check("the jump list has no memory or dream shortcuts", "memory" not in c.js("document.querySelector('dialog.palette .results').innerText").lower())
    held = c.js("(session(page.selected()) || {}).title")
    check("command-K leads with actions for the pane you're in, under its name", c.js("document.querySelector('.pal-group').textContent") == held and c.js("[...document.querySelectorAll('dialog.palette .opt')].slice(0, 2).map(o => o.querySelector('.t').textContent)") == ["Peek", "Rename"], str(c.js("document.querySelector('dialog.palette .results').innerText"))[:300])
    check("the jump list says how to move and how many it found", c.js("document.querySelector('.palette-foot').innerText").endswith("results"))
    c.shot("palette")
    c.js("document.querySelector('dialog.palette').close()")

    # The shell: the sidebar's width and rail, the right panel per session, orbs, and the keys of the sidebar.
    def drag(x0, x1, y=450, clicks=1):
        mouse = lambda kind, x, buttons: c.call("Input.dispatchMouseEvent", type=kind, x=x, y=y, button="left", buttons=buttons, clickCount=clicks)
        mouse("mouseMoved", x0, 0); mouse("mousePressed", x0, 1)
        for i in range(1, 9): mouse("mouseMoved", x0 + (x1 - x0) * i / 8, 1)
        mouse("mouseReleased", x1, 0); time.sleep(0.4)
    side_w = lambda: c.js("Math.round(document.querySelector('#side').getBoundingClientRect().width)")
    c.js("localStorage.removeItem('shell'); localStorage.removeItem('panelBy'); localStorage.removeItem('panelW'); localStorage.setItem('shared', 'true')")
    c.call("Page.reload"); time.sleep(4)
    check("the sidebar starts 240px wide", side_w() == 240, str(side_w()))
    drag(240, 320)
    check("dragging the sidebar's edge widens it", side_w() == 320, str(side_w()))
    c.call("Page.reload"); time.sleep(4)
    check("the sidebar keeps its width across a reload", side_w() == 320, str(side_w()))
    drag(320, 40)
    check("dragging past its narrow end folds the sidebar to a 48px rail", side_w() == 48 and c.js("document.body.classList.contains('rail')"), str(side_w()))
    check("everything in the rail is named", c.js("[...document.querySelectorAll('#side a, #side button')].every(e => (e.getAttribute('aria-label') || e.textContent).trim())"))
    check("the rail has an orb for each running session", c.js("document.querySelectorAll('#side [data-sid] svg.orb').length") >= 2)
    c.call("Page.reload"); time.sleep(4)
    check("the rail is kept across a reload", side_w() == 48)
    c.js("document.activeElement.blur()")
    c.call("Input.dispatchKeyEvent", type="keyDown", key="b", code="KeyB", windowsVirtualKeyCode=66, modifiers=4)
    c.call("Input.dispatchKeyEvent", type="keyUp", key="b", code="KeyB", windowsVirtualKeyCode=66, modifiers=4)
    time.sleep(0.4)
    check("command-B opens the rail again at the width it had", side_w() == 320, str(side_w()))
    for clicks in (1, 2):  # a real double-click: two presses, the second counted as such
        c.call("Input.dispatchMouseEvent", type="mousePressed", x=320, y=450, button="left", buttons=1, clickCount=clicks)
        c.call("Input.dispatchMouseEvent", type="mouseReleased", x=320, y=450, button="left", buttons=0, clickCount=clicks)
    time.sleep(0.4)
    check("a double-click on the edge puts the sidebar back to 240px", side_w() == 240, str(side_w()))
    panel_w = lambda: c.js("(() => { const s = document.querySelector('.shared'); return s ? Math.round(s.getBoundingClientRect().width) : 0; })()")
    check("the right panel starts 316px wide", panel_w() == 316, str(panel_w()))
    left = c.js("document.querySelector('.shared').getBoundingClientRect().left")
    drag(left, left - 100)
    check("dragging the right panel's edge widens it", panel_w() == 416, str(panel_w()))
    c.js("pageKeys.jump(1)"); time.sleep(1)
    check("another pane keeps its own panel width", panel_w() == 316, str(panel_w()))
    c.js("pageKeys.jump(0)"); time.sleep(1)
    check("the first pane's panel is as it was left", panel_w() == 416, str(panel_w()))
    c.call("Page.reload"); time.sleep(4)
    check("the panel's width is kept across a reload", panel_w() == 416, str(panel_w()))
    left = c.js("document.querySelector('.shared').getBoundingClientRect().left")
    drag(left, 1435)
    check("dragging the panel shut closes it, for this pane only", panel_w() == 0 and c.js("document.querySelector('.actions-top .icon[aria-pressed]').getAttribute('aria-pressed')") == "false", str(panel_w()))
    c.js("pageKeys.jump(1)"); time.sleep(1)
    check("the other pane's panel stays open", panel_w() == 316, str(panel_w()))
    c.js("pageKeys.jump(0)"); time.sleep(0.5)
    c.js("pageKeys.p()"); time.sleep(1)
    check("p brings the panel back at the width it had", panel_w() == 416, str(panel_w()))
    check("every session row in the sidebar has an orb", c.js("[...document.querySelectorAll('#side [data-sid]')].every(r => r.querySelector('svg.orb[role=img][aria-label]'))"))
    c.js("document.querySelector('#side [data-sid] svg.orb').__kept = 1; sideKey = ''; drawSide();")
    check("an orb survives a redraw while its state holds", c.js("document.querySelector('#side [data-sid] svg.orb').__kept") == 1)
    check("a live session's row has a second line", c.js("[...document.querySelectorAll('#side .side-row.two .sub .what')].some(e => e.textContent.trim())"))
    turn = c.js("""(() => { const s = S.sessions.find(x => x.id === %s); s.state = 'working'; s.turn = { since: Date.now() / 1000 - 65 }; s.activity = { tool: 'Edit', target: 'ui/app.js', since: Date.now() / 1000 - 3 }; sideKey = ''; drawSide(); titleNow();
        const row = document.querySelector('#side [data-sid=%s]'); return [row.querySelector('.sub .what').textContent, row.querySelector('time[data-since]').textContent, document.title, document.querySelector('link[rel=icon]').href.includes('2dd4a0')]; })()""" % (json.dumps(ids[0]), json.dumps(ids[0])))
    shown_time = f"(document.querySelector('#side [data-sid={json.dumps(ids[0])}] time[data-since]') || {{}}).textContent"
    ticked = next((t for _ in range(12) if (t := c.js(shown_time)) != turn[1] or time.sleep(0.25)), turn[1])  # a second on, without a redraw
    check("a working row says its tool and target, and its turn's time ticks", turn[0] == "Edit ui/app.js" and turn[1] in ("1m 05s", "1m 06s") and bool(ticked) and ticked != turn[1], str(turn) + ticked)
    check("the tab's title and icon count a working session", "1 working" in turn[2] and turn[3], str(turn))
    c.js("tick()"); time.sleep(0.5)  # the server's own state again
    c.js(f"window.__peeked = null; window.hubPeek = (id) => {{ window.__peeked = id; }}; document.querySelector('#side [data-sid={json.dumps(ids[1])}] a.nav').focus()")
    c.call("Input.dispatchKeyEvent", type="keyDown", key=" ", code="Space", windowsVirtualKeyCode=32, text=" "); c.call("Input.dispatchKeyEvent", type="keyUp", key=" ", code="Space", windowsVirtualKeyCode=32)
    time.sleep(0.3)
    check("space on a sidebar row peeks at that session", c.js("window.__peeked") == ids[1], str(c.js("window.__peeked")))
    c.js("delete window.hubPeek")
    before = c.js("document.activeElement.dataset.key")
    key("k", "KeyK", 75); time.sleep(0.2)
    after = c.js("document.activeElement.dataset.key")
    key("j", "KeyJ", 74); time.sleep(0.2)
    check("j and k walk the sidebar's rows", after != before and c.js("document.activeElement.dataset.key") == before, f"{before} {after}")
    c.js("document.activeElement.blur()")
    key("?", "Slash", 191); time.sleep(0.3)
    check("? docks the key sheet beside the app, not over it", c.js("!!document.querySelector('dialog.keys-help[open]') && !document.querySelector('dialog.keys-help').matches(':modal') && document.querySelector('dialog.keys-help').getBoundingClientRect().left === 0"))
    c.js("(() => { const i = document.querySelector('.keys-help input'); i.value = 'dreams'; i.dispatchEvent(new Event('input')); })()")
    check("the key sheet can be searched, and writes a chord as g then d", c.js("[...document.querySelectorAll('.keys-help .keysheet > span:not(.caps)')].map(e => e.textContent).includes('Dreams of this project')") and "then" in c.js("document.querySelector('.keys-help .caps').innerText"))
    c.shot("keys")
    key("Escape", "Escape", 27); time.sleep(0.3)
    check("esc puts the key sheet away", not c.js("!!document.querySelector('dialog.keys-help')"))
    c.js("document.activeElement.blur()")
    key("g", "KeyG", 71); key("h", "KeyH", 72); time.sleep(1.5)
    check("g then h goes home", c.js("location.hash") in ("#/", ""), c.js("location.hash"))
    c.go(f"#/s/{ids[0]},{ids[1]}", 4)

    c.go(f"#/p/{project}", 2.5)
    c.js("document.querySelector('.actions-top .icon').click()")
    time.sleep(0.5)
    check("project settings open with icons", c.js("document.querySelector('dialog .identity .mark svg') !== null") and "Upload image" in c.js("document.querySelector('dialog').innerText"))
    c.shot("settings")
    c.js("document.querySelector('dialog').close()")

    c.go(f"#/p/{project}/dreams", 3)
    text = body()
    check("dreams render", "Proposed changes" in text and "[object" not in text and "1 thing needs you" in text, text[:300])
    need = c.js("(() => { const n = document.querySelector('.callout.needs .need-row'); return n ? [n.querySelector('p').textContent, document.querySelector('.callout.needs .needs-from').textContent, [...n.querySelectorAll('.need-sub > *')].map(e => e.textContent), n.querySelector('.need-done').textContent] : null; })()")
    check("what needs the person is listed once, at the top, with its proposal said once, its evidence and Done", need == [NEED.split(" · evidence: ")[0], f"said in {TODAY}-3", ["a1b2c3d4"], "Done"]
          and c.js("[...document.querySelectorAll('section.sec > h2')].filter(e => e.textContent === 'Needs you').length") == 1 and c.js("[...document.querySelectorAll('.need-row')].filter(n => !n.closest('.callout.needs')).length") == 0,
          str(need) + str(c.js("[[...document.querySelectorAll('section.sec > h2')].map(e => e.textContent), [...document.querySelectorAll('.need-row')].filter(n => !n.closest('.callout.needs')).map(n => n.innerText)]")))
    listed = c.js("[...document.querySelectorAll('.list a')].filter(a => a.querySelector('small')).map(a => [a.firstChild.textContent, a.querySelector('small').textContent, a.classList.contains('on')])")
    check("proposals are listed newest first with their state, the open one shown", open_name == f"{TODAY}-3-proposal.md" and listed == [[f"{TODAY}-3", "open", True], [f"{TODAY}-2", "applied", False], [TODAY, "applied", False]], str(listed))
    count = c.js("document.querySelectorAll('.change input').length")
    check("all changes can be ticked", count == 9, str(count))
    groups = c.js("[...document.querySelector('section.sec:has(.change)').querySelectorAll('.fgroup')].map(g => [g.querySelector('.fg-toggle code').textContent, g.querySelector('.fg-n').textContent, g.querySelectorAll('.change').length])")
    check("proposed changes are grouped by the file they change, in the dream's order, with ticks per file", groups == [["project/gotchas.md", "0 of 4", 4], ["project/decisions.md", "0 of 1", 1]], str(groups))
    head = c.js("document.querySelector('.review-head .totals').textContent")
    check("the totals head the review and stay in view", head.startswith("9 proposed·0 ticked") and c.js("getComputedStyle(document.querySelector('.review-head')).position") == "sticky", head)
    meta = c.js("(() => { const r = document.querySelector('section.sec:has(.change) .change'); return [r.querySelector('.meta .sub').textContent, r.querySelector('.ev').hidden]; })()")
    check("each change has one faint line, without the file its group names, and why behind a chevron", meta == ["seen 2/4 sessions · cost ~6", True], str(meta))
    c.js("document.querySelector('section.sec:has(.change) .change .ev-toggle').click()")
    check("the chevron shows why and the evidence, and doesn't tick", c.js("(() => { const r = document.querySelector('section.sec:has(.change) .change'); return !r.querySelector('.ev').hidden && r.querySelector('.ev').textContent.includes('a1b2c3d4') && !r.querySelector('input').checked; })()"))
    heads = c.js("[...document.querySelectorAll('section.sec > h2')].map(e => e.textContent)")
    check("the sections for your own text and for Claude's own notes have their names", heads == ["Needs you", "Proposed changes", "ORG.md and Steering", "Claude's own notes", "Scratchpads to condense / clear"], str(heads))
    notes = c.js("[...document.querySelectorAll('section.sec:has(.change)')].map(e => (e.querySelector('.sec-note') || {}).textContent || '')")
    check("each of the two says what a tick does", notes == ["", "Only you change these. Applying leaves the ticked ones for you, with the two commands to run.", "Facts from Claude Code's own notes that the dream checked. Each goes into the file it names; switch one off to leave it out.", ""], str(notes))
    check("your own text shows what would replace it", "Keys live in the keychain, never in a repo." in c.js("document.querySelectorAll('section.sec:has(.change)')[1].innerText"))
    first = "document.querySelector('section.sec:has(.change)')"
    was = c.js("[...document.querySelectorAll('.change .was del')].map(e => e.textContent)")
    check("an edit shows the line that goes under the line that comes, whole", was == ["[2025-12-01] The importer needs Python 3.11 · nothing newer", "Keys live in ~/.keys · never in a repo."] and c.js(f"{first}.querySelectorAll('.change')[1].querySelector('.what').innerText") == f"[{TODAY}] The importer needs Python 3.11 · 3.12 also works", str(was))
    merged = c.js(f"(() => {{ const m = {first}.querySelectorAll('.change')[2]; return [m.querySelector('.what b').textContent, m.querySelector('.was-word').textContent, [...m.querySelectorAll('.parts li')].map(e => e.textContent)]; }})()")
    check("a merge says the line that stays and the lines it stands for", merged == ["Merge", "In place of 2 lines", ["[2025-11-02] Builds collide…", "[2025-11-20] Run one build…"]], str(merged))
    parted = c.js(f"(() => {{ const m = {first}.querySelectorAll('.change')[3]; return [m.querySelector('.what').innerText, [...m.querySelectorAll('.was')].map(e => [e.querySelector('code').textContent, e.querySelector('.was-n').textContent, e.querySelectorAll('.parts li').length])]; }})()")
    check("a split says the new files and what moves to each", parted == ["Split into project/builds.md, project/importer.md", [["project/builds.md", "2 lines", 2], ["project/importer.md", "1 line", 1]]], str(parted))
    check("a decision reads as its line", c.js(f"{first}.querySelectorAll('.change')[4].querySelector('.what').innerText").startswith("Decision: releases go out on Tuesdays."))
    check("merge and split are set like delete: one weight, one size, the row's own colours", c.js(f"(() => {{ const w = [...{first}.querySelectorAll('.change .what b')].map(b => getComputedStyle(b)); const s = [...document.querySelectorAll('.change .was')].map(e => getComputedStyle(e)); return w.length === 2 && w.every(x => x.fontWeight === '600' && x.fontSize === '14px') && s.every(x => x.fontSize === '13px' && x.color === s[0].color); }})()"))
    side = c.js("(() => { const d = document.querySelector('details.not-proposed'); return d ? [d.querySelector('summary').textContent, [...d.querySelectorAll('h3')].map(e => e.textContent), [...d.querySelectorAll('.note-counts td')].slice(0, 7).map(e => e.textContent), [...d.querySelectorAll('li')].map(e => e.textContent)] : null; })()")
    check("what the dream read and did not propose is shown from the file beside the proposal", side == ["Read and not proposed", ["Claude's own notes, file by file", "Not carried over", "Rejected (one-offs, injections)"],
          ["feedback_importer.md", "2", "1", "0", "0", "1", "0"], ["shop/feedback_importer.md — which port the server uses — stale: the server moved to another port", "Seen once: a missing font."]], str(side))
    carry = "document.querySelectorAll('section.sec:has(.change)')[2].querySelector('.change')"
    check("a carry item names its note and lists its facts, each with a switch", c.js(f"[{carry}.querySelector('.what').innerText, {carry}.querySelectorAll('.facts .switch[aria-checked=true]').length, {carry}.querySelector('.sub').textContent]")
          == ["Carry from shop/feedback_importer.md", 1, "1 of 1 fact carried"], str(c.js(f"{carry}.innerText")))
    fact_line = lambda: next(line for line in (store / "dreams" / open_name).read_text().split("\n") if "project/gotchas.md \"" in line and "TZ=UTC" in line)
    for on in (False, True):
        spot = c.js(f"(() => {{ const b = {carry}.querySelector('.facts .switch'); b.scrollIntoView({{block: 'center'}}); const r = b.getBoundingClientRect(); return [r.left + r.width / 2, r.top + r.height / 2]; }})()")
        c.call("Input.dispatchMouseEvent", type="mousePressed", x=spot[0], y=spot[1], button="left", clickCount=1)
        c.call("Input.dispatchMouseEvent", type="mouseReleased", x=spot[0], y=spot[1], button="left", clickCount=1)
        time.sleep(1.2)
        check(f"a click on a fact's switch {'carries it again' if on else 'leaves it out'}, and doesn't tick the item", fact_line().lstrip().startswith("+ " if on else "- ")
              and c.js(f"{carry}.querySelector('.sub').textContent") == ("1 of 1 fact carried" if on else "0 of 1 fact carried") and not c.js(f"{carry}.querySelector('input').checked"), fact_line())
    for index, mark in ((6, "- [x] 7. Steering"), (7, "- [x] 8. notes shop/feedback_importer.md")):
        spot = c.js(f"(() => {{ const b = document.querySelectorAll('.change input')[{index}]; b.scrollIntoView({{block: 'center'}}); const r = b.getBoundingClientRect(); return [r.left + r.width / 2, r.top + r.height / 2]; }})()")
        c.call("Input.dispatchMouseEvent", type="mousePressed", x=spot[0], y=spot[1], button="left", clickCount=1)
        c.call("Input.dispatchMouseEvent", type="mouseReleased", x=spot[0], y=spot[1], button="left", clickCount=1)
        time.sleep(1.2)
        check(f"a click ticks a change in the new sections: {mark[6:]}", mark in (store / "dreams" / open_name).read_text() and c.js(f"document.querySelectorAll('.change input')[{index}].checked"))
    check("Apply counts ticks from every section, and Dismiss sits beside it", c.js("[...document.querySelectorAll('.applybar button')].map(b => b.textContent)") == ["Apply 2 changes", "Dismiss"], str(c.js("document.querySelector('.applybar').innerText")))
    check("the totals count the ticks too", c.js("document.querySelector('.review-head .totals').textContent").startswith("9 proposed·2 ticked"), c.js("document.querySelector('.review-head .totals').textContent"))
    c.js("document.querySelector('.fgroup .fg-viewed input').click()")
    time.sleep(0.3)
    check("a file marked viewed folds and is counted", c.js("document.querySelector('.fgroup .fg-body').hidden") and c.js("document.querySelector('.review-head .viewed-n').textContent").startswith("1 of "))
    c.go(f"#/p/{project}/dreams/{open_name}", 3)
    check("viewed is kept in this browser", c.js("document.querySelector('.fgroup .fg-body').hidden") and c.js("document.querySelector('.fgroup .fg-viewed input').checked"))
    c.js("document.querySelector('.fgroup .fg-viewed input').click()")
    time.sleep(0.3)
    check("unmarking it opens it again", not c.js("document.querySelector('.fgroup .fg-body').hidden"))
    summaries = lambda: c.js("[...document.querySelectorAll('details.carried:not(.actions) > summary')].map(e => e.textContent)")
    kept = lambda: c.js("[...document.querySelectorAll('details.carried:not(.actions)')].map(d => [...d.querySelectorAll('.change.kept')].map(r => [r.querySelector('.what').textContent, r.querySelector('.sub').textContent, (r.querySelector('.retry') || {}).textContent || '']))")
    check("what was approved and not yet done is told apart from what is declined", summaries() == ["2 approved, not yet done", "1 declined"] and len(kept()[0]) == 2 and kept()[0][0][0] == YOURS[2:] and kept()[0][0][1].startswith(f"ORG.md · add · from {TODAY} · approved ") and kept()[0][0][1].endswith(" · waits for you") and kept()[0][0][2] == "Open a terminal for it"
          and kept()[0][1][0] == "helper" and kept()[0][1][1].startswith(f"keep: nothing. Clear. · from {TODAY} · approved ") and kept()[0][1][1].endswith(" · waits for a session") and kept()[0][1][2] == "Start a session for it", str(summaries()) + str(kept()))
    names = c.js("[...document.querySelectorAll('.list > *')].map(e => e.classList.contains('list-title') ? 'title:' + e.textContent : e.textContent)")
    check("a proposal from before the kit's record is listed under Earlier", names[-2:] == ["title:Earlier", "2026-01-01"], str(names))
    done = c.js("(() => { const d = document.querySelector('details.actions'); return d ? [d.querySelector('summary').textContent, [...d.querySelectorAll('.p')].map(r => [r.querySelector('.k').textContent, (r.querySelector('small') || {}).textContent || ''])] : null; })()")
    check("what was done in the person's name is listed, with the door it came through", done and done[0] == "Done in your name" and ["Principles edited", "through Dream Hub"] in done[1], str(done))
    way = c.js("[...document.querySelectorAll('details.carried:not(.actions)')[0].querySelectorAll('.change.kept')].map(r => [r.querySelector('.fate').textContent, [...r.querySelectorAll('.kept-acts .retry')].map(b => b.textContent), r.querySelectorAll('input').length])")
    check("an approved item says what gets it done, in the kit's words, can be taken back, and can't be ticked again", way == [["It is yours to make, in a terminal: chflags nouchg memory/ORG.md, then python3 scripts/dream_apply.py --yours.", ["Open a terminal for it", "Take back"], 0], ["A session does it: /dream apply in a session.", ["Start a session for it", "Take back"], 0]], str(way))
    # both ways of getting on with an approved item, clicked: each opens a session of its own, which is then removed
    for key, shows in (("yours:", lambda sid: on_screen(sid, "is locked, and this command never lifts the lock", 30)),
                       ("session:", lambda sid: next((x.get("task") == "/dream apply" for x in api("GET", "/api/state")["sessions"] if x["id"] == sid), False))):
        c.js("document.querySelectorAll('details.carried:not(.actions)').forEach(d => { d.open = true; })")
        c.js(f"document.querySelector('details.carried:not(.actions) [data-key^=\"{key}\"]').click()")
        opened = next((c.js("location.hash") for _ in range(40) if c.js("location.hash").startswith("#/s/") or time.sleep(0.25)), "")
        check(f"the button for an approved item ({key[:-1]}) opens its session, which does what it says", opened.startswith("#/s/") and shows(opened[4:]), opened + "\n" + tmux_screen(opened[4:]))
        api("POST", f"/api/sessions/{opened[4:]}/dismiss", {})
        c.go(f"#/p/{project}/dreams", 3)
    check("neither of them was done by that: both still wait", summaries()[0] == "2 approved, not yet done", str(summaries()))
    c.js("document.querySelector('.list a:nth-child(2)').click()")
    time.sleep(2.5)
    check("an applied proposal says so and can't be ticked, applied or dismissed", c.js("document.querySelector('.dream-state').textContent").startswith("Applied ") and c.js("[...document.querySelectorAll('.change input')].every(b => b.disabled)") and c.js("document.querySelectorAll('.applybar button').length") == 0)
    fates = lambda: c.js("[...document.querySelectorAll('section.sec .change')].map(r => { const f = r.querySelector('.fate'); return f ? [f.querySelector('b').textContent, f.querySelectorAll('code').length] : null; })")
    check("each ticked item says what became of it: already there, or yours to apply", fates() == [["Already there", 0], None] and not c.js("!!document.querySelector('.callout.yours')"), str(fates()))
    c.js("document.querySelector('.list a:nth-child(3)').click()")
    time.sleep(2.5)
    check("an item the kit applied says so, with its commit, and an ORG.md item that it is yours", fates() == [["Applied", 1], None, ["Applied", 1], ["Applied", 1], ["Yours to apply", 0], ["Waiting for a session", 0], ["Refused", 0]], str(fates()))
    waits_here = c.js("(() => { const f = [...document.querySelectorAll('section.sec .fate')].find(e => e.querySelector('b').textContent === 'Waiting for a session'); return [f.querySelector('span').textContent, (f.querySelector('button') || {}).textContent || '']; })()")
    check("an item waiting for a session on a closed proposal says what does it, with the button", waits_here == ["/dream apply in a session does it.", "Start a session for it"], str(waits_here))
    lost = c.js("(() => { const sec = [...document.querySelectorAll('section.sec')].find(e => e.querySelector('h2').textContent.startsWith('Ticked lines')); return sec ? [sec.querySelector('h2').textContent, sec.querySelector('.what code').textContent, sec.querySelector('.fate b').textContent, sec.querySelector('.fate span').textContent.length > 10] : null; })()")
    check("a ticked line the kit couldn't read as an item is shown, with why it was refused", lost and lost[0] == "Ticked lines the kit couldn't read as items" and "written with a star" in lost[1] and lost[2] == "Refused" and lost[3], str(lost))
    mine = c.js("(() => { const b = document.querySelector('.callout.yours'); return b ? [b.querySelector('h2').textContent, [...b.querySelectorAll('.command-row code')].map(e => e.textContent), [...b.querySelectorAll('.command-row .retry')].map(e => e.textContent), b.querySelector('[data-key=yours-terminal]').textContent, b.querySelector('p').textContent] : null; })()")
    check("the person's tier says it is theirs and gives the two commands, each to copy, and a terminal", mine and mine[:4] == ["Yours to apply: 1 change to ORG.md or Steering", [f"chflags nouchg ~/agent-memory/{NAME}/ORG.md", f"cd ~/projects/{NAME} && python3 scripts/dream_apply.py --yours"], ["Copy", "Copy"], "Open a terminal for it"] and "No agent applies these" in mine[4], str(mine))
    c.shot("yours")
    c.js("document.querySelector('.list a:last-child').click()")
    time.sleep(2.5)
    check("an earlier proposal says where it is from and can't be changed", c.js("document.querySelector('.dream-state').textContent").startswith("From before the kit kept a record of proposals") and c.js("[...document.querySelectorAll('.change input')].every(b => b.disabled)") and c.js("document.querySelectorAll('.applybar button').length") == 0)
    c.js("document.querySelector('.list a').click()")
    time.sleep(2.5)
    timing = f"Automatic dreams, checked daily at {hour:02d}:00"
    why = ["Last dream 3d ago: 27 sessions since 8 Nov", f"Not due when checked 2h ago: {HELD[9:]}"]
    check("the Dreams switch says automatic dreams and the hour of the daily check", c.js("document.querySelector('.dream-meta .switch').title") == "Automatic dreams" and c.js("document.querySelector('.dream-meta .switch').getAttribute('aria-pressed')") == "true" and c.js("document.querySelector('.dream-meta span').textContent") == timing, c.js("document.querySelector('.dream-meta').innerText"))
    check("the Dreams page says why it last dreamed and what the last check found", c.js("[...document.querySelectorAll('.dream-why')].map(e => e.textContent)") == why, str(c.js("[...document.querySelectorAll('.dream-why')].map(e => e.textContent)")))
    left_to_do = c.js("(document.querySelector('.dream-note') || {}).innerText") or ""
    check("with the notes already in the newest dream, Dreams says what is left: tick, apply, turn them off", left_to_do.startswith("The newest dream lists the notes of Claude's own worth keeping. Tick and apply them, then turn them off with") and f"retire-auto-memory.sh {NAME}" in left_to_do and "next dream" not in left_to_do, left_to_do)
    c.shot("dreams")

    c.go(f"#/p/{project}", 3.5)
    row = lambda: c.js("document.querySelector('.dream-row .k').textContent")
    check("the project page says automatic dreams and the hour of the daily check", row() == timing and c.js("document.querySelector('.dream-row .switch').title") == "Automatic dreams", row())
    check("the project page says why it last dreamed and what the last check found", c.js("[...document.querySelectorAll('.rail .dream-why')].map(e => e.textContent)") == why, str(c.js("[...document.querySelectorAll('.rail .dream-why')].map(e => e.textContent)")))
    check("nothing says Sundays or weekly for a daily job", "Sunday" not in body() and "eekly" not in body())
    setup = c.js("[...document.querySelectorAll('.setup-box a.notice')].map(a => [a.innerText.trim(), a.getAttribute('href')])")
    check("moving Claude's own notes is a setup step that leads to Dreams", ["Move Claude's own notes into memory", f"#/p/{NAME}/dreams"] in setup, str(setup))
    c.shot("project-dreams")
    write_job(NAME, repo, 18, weekday=0)
    time.sleep(5)
    check("a weekly job from before still reads as Sundays", row() == "Dreams Sundays 18:00", row())
    write_job(NAME, repo, 7)
    check_file(NAME).write_text("[1, 2]")
    time.sleep(5)
    refused = c.js("[...document.querySelectorAll('.rail .dream-why')].map(e => e.textContent)")
    check("a state file the kit would refuse shows one plain line, not a blank panel", len(refused) == 1 and refused[0].startswith("The kit can't read its record of this project's dream checks") and row() == timing and c.js("document.querySelectorAll('.rail .panel').length") >= 2, str(refused))
    check_file(NAME).unlink()
    time.sleep(5)
    check("with no check recorded, nothing is said about one", row() == timing and c.js("document.querySelectorAll('.dream-why').length") == 0, row())
    waits = f"not due: the proposal {open_name} (of 9 Nov, open for 3 days) has not been applied or dismissed"
    check_file(NAME).write_text(json.dumps({"last_dream": now - 3 * 86400, "last_reason": "started by hand", "checked": now - 7200, "verdict": waits, "tries": [], "reminded": {"proposal": open_name, "at": now - 86400}, "waiting_said": now - 86400}))
    time.sleep(5)
    said = c.js("[...document.querySelectorAll('.rail .dream-why')].map(e => e.textContent)")
    reminded = time.strftime("%-d %b", time.localtime(now - 86400))
    check("an open proposal holding a due dream back is said whole, with when its person was reminded", said == ["Last dream 3d ago, started by hand", f"Not due when checked 2h ago: {waits[9:]}", f"You were reminded of this proposal on {reminded}", f"You were reminded on {reminded} that approved changes are waiting"], str(said))
    changed = c.js("[...document.querySelectorAll('.feed .log-change')].map(e => [(e.querySelector('del') || {}).textContent || '', (e.querySelector('ins') || {}).textContent || ''])")
    check("the activity list shows an edit to memory as the line before and after", [OLD[2:], NEW[2:]] in changed, str(changed))
    check_file(NAME).unlink()

    # dismissing, in the page: it asks first, and the kit's answer is what is shown
    c.go(f"#/p/{project}/dreams", 3)
    c.js("document.querySelector('.applybar [data-key=dismiss]').click()")
    time.sleep(0.5)
    words = c.js("(document.querySelector('dialog.confirm') || {}).innerText") or ""
    check("dismissing asks first and says what it means", "Dismiss this proposal?" in words and "Nothing in it is applied" in words and "handed to the next dream" in words and "none counts as declined" in words, words)
    c.js("document.querySelector('dialog.confirm .btn.primary').click()")
    time.sleep(5)
    told = c.js("(document.querySelector('.dream-note.said') || {}).textContent")
    check("what the kit said about the dismissal is shown", told == f"dismissed: {open_name}. 8 of its items are handed to the next dream, and none of them counts as declined by this.", str(told))
    check("the proposal now reads as dismissed and can't be ticked", c.js("document.querySelector('.dream-state').textContent").startswith("Dismissed ") and c.js("document.querySelector('.list a small').textContent") == "dismissed" and c.js("[...document.querySelectorAll('.change input')].every(b => b.disabled)") and c.js("document.querySelectorAll('.applybar button').length") == 0)
    check("what was ticked, what waits for the next dream and what is declined are counted apart", summaries() == ["2 approved, not yet done", "8 items wait for the next dream", "1 declined"], str(summaries()))
    # a dream that holds the project: the kit's collector ran in a session and its dream is not finished
    took = collect(repo)
    asked = in_repo(repo, "dream_due.py")
    c.go(f"#/p/{project}", 5)
    said = c.js("[...document.querySelectorAll('.rail .dream-why')].map(e => e.textContent)")
    check("the kit's own verdict that a dream is running is shown whole, with who and since when", took.returncode == 0 and asked.returncode == 3 and asked.stdout.startswith(f"not due: a dream is running (session {DREAMER}, since ") and said == [f"Not due when checked just now: {asked.stdout.strip()[9:]}"], f"{took.stderr}{asked.stdout}{asked.stderr} {said}")
    c.go(f"#/p/{project}/dreams", 3)
    held = c.js("(document.querySelector('.dream-note.held span') || {}).textContent") or ""
    check("the Dreams page says who is dreaming, in the kit's words, and offers to give it up", held.startswith(f"A dream is already running for {NAME}: session {DREAMER}, since ") and c.js("(document.querySelector('[data-key=release]') || {}).textContent") == "Give it up", held)
    c.js("document.querySelector('[data-key=release]').click()")
    time.sleep(0.5)
    words = c.js("(document.querySelector('dialog.confirm') || {}).innerText") or ""
    check("giving a dream up asks first and says what is discarded", "Give up the dream that is running?" in words and "discards its batch" in words and "Nothing in memory changes" in words, words)
    c.js("document.querySelector('dialog.confirm .btn.primary').click()")
    time.sleep(5)
    told = c.js("(document.querySelector('.dream-note.said') || {}).textContent") or ""
    check("what the kit discarded is shown, and nobody is dreaming", told.startswith(f"Released {NAME} from session {DREAMER}, which collected on ") and not c.js("!!document.querySelector('.dream-note.held')") and not (store / "dreams/_batch").exists(), told)
    check_file(NAME).unlink()
    retired = subprocess.run(["/bin/sh", str(KIT / "retire-auto-memory.sh"), NAME], capture_output=True, text=True)
    check("the kit's retire-auto-memory.sh ran", retired.returncode == 0, retired.stdout + retired.stderr)
    time.sleep(5)
    setup = c.js("[...document.querySelectorAll('.setup-box a.notice')].map(a => a.innerText.trim())")
    check("the step goes once auto memory is off", "Move Claude's own notes into memory" not in setup, str(setup))

    # what the kit's check finds, and a record of an apply it can't read: each a plain line on the page
    orphan = store / "dreams" / f"{TODAY}-9-proposal.md"
    orphan.write_text("# Dream nobody finished\n")
    record_file = store / "dreams" / f"{TODAY}-applied.json"
    good_record = record_file.read_bytes()
    record_file.write_text("{")
    c.go(f"#/p/{project}/dreams/{TODAY}-proposal.md", 4)
    problems = lambda: [c.js("[...document.querySelectorAll('.callout.checks li')].map(e => e.textContent)"), c.js("[...document.querySelectorAll('.split .callout[role=alert] p')].map(e => e.textContent)")]
    found = problems()
    check("a proposal no dream finished is listed as what the kit's check found", len(found[0]) == 1 and found[0][0].startswith(f"dreams/{orphan.name} is a proposal no dream finished"), str(found[0]))
    check("an apply record that can't be read is said in one line, and no fate is shown", len(found[1]) == 1 and found[1][0].startswith("The kit's record of what was applied can't be read") and c.js("document.querySelectorAll('section.sec .fate').length") == 0, str(found[1]))
    c.shot("problems")

    c.go(f"#/p/{project}/memory/project/gotchas.md", 3)
    history = c.js("[...document.querySelectorAll('.history .log-change')].map(e => [(e.querySelector('del') || {}).textContent || '', (e.querySelector('ins') || {}).textContent || ''])")
    check("a memory file's history shows an edit as the line before and after", [OLD[2:], NEW[2:]] in history, str(history))
    c.go(f"#/p/{project}/memory", 2.5)
    text = body()
    check("memory renders", "ORG.md" in text and "gotchas.md" in text and "null" not in text.split(), text[:300])
    c.js("document.querySelector('input.search').value='launchd'; document.querySelector('input.search').dispatchEvent(new Event('input'))")
    time.sleep(1.2)
    check("memory search shows hits", c.js("document.querySelectorAll('a.hit').length") > 0)

    c.size(390, 844, mobile=True)
    c.go("#/", 2)
    width = c.js("[document.documentElement.scrollWidth, innerWidth]")
    check("no sideways scroll on a phone", width[0] <= width[1], str(width))
    c.shot("phone-platform")
    check_file(NAME).write_text(json.dumps({"last_dream": now - 3 * 86400, "last_reason": "started by hand", "checked": now - 7200, "verdict": SKIPPED, "tries": None, "waiting_said": now - 86400}))
    c.go(f"#/p/{project}", 5)
    long_line = c.js("[...document.querySelectorAll('.dream-why')].map(e => e.textContent)")
    check("a verdict that says rows were skipped is shown whole, with the reminder of what waits", long_line == ["Last dream 3d ago, started by hand", f"Due when checked 2h ago: {SKIPPED[5:]}", f"You were reminded on {reminded} that approved changes are waiting"], str(long_line))
    check("an edit in the activity list fits a phone", c.js("document.querySelectorAll('.feed .log-change').length") >= 1 and c.js("[...document.querySelectorAll('.feed .log-change, .dream-why')].every(e => e.getBoundingClientRect().right <= innerWidth + 1)"))
    c.go(f"#/p/{project}/memory/project/gotchas.md", 3)
    width = c.js("[document.querySelector('#view').scrollWidth, document.querySelector('#view').clientWidth]")
    check("a memory file's history with an edit fits a phone", width[0] <= width[1] and c.js("document.querySelectorAll('.history .log-change').length") >= 1 and c.js("[...document.querySelectorAll('.history .log-change')].every(e => e.getBoundingClientRect().right <= innerWidth + 1)"), str(width))
    c.go(f"#/p/{project}/dreams/{TODAY}-proposal.md", 4)
    found = problems()
    width = c.js("[document.querySelector('#view').scrollWidth, document.querySelector('#view').clientWidth, document.documentElement.scrollWidth, innerWidth]")
    check("what the kit's check found and the unreadable record fit a phone", len(found[0]) == 1 and len(found[1]) == 1 and width[0] <= width[1] and width[2] <= width[3] and c.js("[...document.querySelectorAll('.callout, .callout li, .callout p')].every(e => e.getBoundingClientRect().right <= innerWidth + 1)"), str(width))
    c.shot("phone-problems")
    orphan.unlink()
    record_file.write_bytes(good_record)
    c.go(f"#/p/{project}", 4)
    width = c.js("[document.querySelector('#view').scrollWidth, document.querySelector('#view').clientWidth]")
    check("the homepage fits a phone", width[0] <= width[1], str(width))
    c.go(f"#/p/{project}/dreams", 3)
    width = c.js("[document.querySelector('#view').scrollWidth, document.querySelector('#view').clientWidth, document.documentElement.scrollWidth, innerWidth]")
    check("a dream with edits, a merge, a split and notes not carried over fits a phone", width[0] <= width[1] and width[2] <= width[3] and c.js("[...document.querySelectorAll('.change, .skip, .dream-why')].every(e => e.getBoundingClientRect().right <= innerWidth + 1)"), str(width))
    c.shot("phone-dreams")
    c.js("document.querySelectorAll('details.carried:not(.actions)').forEach(d => { d.open = true; })")
    time.sleep(0.3)
    rows = kept()
    check("items that were ticked and items that wait say where they came from and how they were left", len(rows) == 3 and [r[1].split(" · ")[-1] for r in rows[0]] == ["waits for you", "waits for a session"] and len(rows[1]) == 8 and [r[1] for r in rows[1][6:]] == [f"Steering · add · from {TODAY}-3 · ticked, not applied", f"from {TODAY}-3 · ticked, not applied"] and rows[1][0][1] == f"project/gotchas.md · add · from {TODAY}-3 · not decided" and all(not r[2] for r in rows[1]), str(rows)[:600])
    check("a declined item can be let back in", rows[2] == [["[2026-11-08] Passed over both times → declined", f"project/gotchas.md · add · from {TODAY}-2", "Allow it to be proposed again"]], str(rows[2]))
    width = c.js("[document.querySelector('#view').scrollWidth, document.querySelector('#view').clientWidth, document.documentElement.scrollWidth, innerWidth]")
    check("what waits and what is declined fit a phone", width[0] <= width[1] and width[2] <= width[3] and c.js("[...document.querySelectorAll('.change.kept, details.carried, .dream-note')].every(e => e.getBoundingClientRect().right <= innerWidth + 1)"), str(width))
    c.js("document.querySelector('details.carried:not(.actions)').scrollIntoView()")
    c.shot("phone-carried")
    c.js("document.querySelector('details.carried:not(.actions) [data-key^=\"forget:\"]').click()")
    time.sleep(5)
    told = c.js("(document.querySelector('.dream-note.said') || {}).textContent") or ""
    check("letting a declined item back in shows the kit's answer, and it leaves the list", told.startswith('forgotten: project/gotchas.md — add — "- [2026-11-08] Passed over both times → declined"') and "A dream may propose it again." in told and summaries() == ["2 approved, not yet done", "8 items wait for the next dream"], told + str(summaries()))
    c.go(f"#/p/{project}/dreams/{TODAY}-proposal.md", 3)
    width = c.js("[document.querySelector('#view').scrollWidth, document.querySelector('#view').clientWidth, document.documentElement.scrollWidth, innerWidth]")
    check("what became of each item and the person's two commands fit a phone", width[0] <= width[1] and width[2] <= width[3] and c.js("document.querySelectorAll('section.sec .fate').length") == 6 and c.js("[...document.querySelectorAll('.fate, .fate button, section.sec, .callout.yours, .command-row, .command-row code')].every(e => e.getBoundingClientRect().right <= innerWidth + 1)"), str(width))
    c.js("document.querySelector('.callout.yours').scrollIntoView()")
    c.shot("phone-yours")
    c.go(f"#/p/{project}/dreams", 3)
    c.js("document.querySelectorAll('details.carried:not(.actions)').forEach(d => { d.open = true; })")
    c.js("document.querySelector('details.carried:not(.actions) [data-key^=\"withdraw:\"]').click()")
    time.sleep(0.5)
    words = c.js("(document.querySelector('dialog.confirm') || {}).innerText") or ""
    check("taking back an approval asks first and says what it means", "Take back this approval?" in words and "stops waiting to be done" in words and "A dream may propose it again" in words, words)
    c.js("document.querySelector('dialog.confirm .btn.primary').click()")
    time.sleep(5)
    told = c.js("(document.querySelector('.dream-note.said') || {}).textContent") or ""
    check("the kit's answer to taking it back is shown, and it no longer waits", told.startswith(f'withdrawn: ORG.md — add — "{YOURS}"') and "It is no longer waiting to be done." in told and summaries() == ["1 approved, not yet done", "8 items wait for the next dream"], told + str(summaries()))
    c.go(f"#/p/{project}/dreams/{TODAY}-proposal.md", 3)
    check("once it is taken back, its proposal no longer says anything is yours to apply", not c.js("!!document.querySelector('.callout.yours')"))
    (store / "dreams" / f"{TODAY}-2-proposal.md").unlink()
    c.go(f"#/p/{project}/dreams/{TODAY}-2-proposal.md", 4)
    gone = c.js("(document.querySelector('.dream-state') || {}).textContent") or ""
    check("a closed proposal whose file was removed says what happened", gone.startswith("This proposal was applied on ") and gone.endswith("its file has been removed since, so there is nothing to read.") and "didn't load" not in body(), gone)
    c.go(f"#/p/{project}", 3)
    c.shot("phone-home")
    c.go(f"#/s/{ids[0]}", 3)
    c.js("Object.assign(shell, { w: 300, rail: true }); keepShell(); applyShell(); sideKey = ''; drawSide();")
    c.js("document.querySelector('.menu-btn').click()")
    time.sleep(0.5)
    check("a phone opens the full drawer, never the rail", c.js("!document.body.classList.contains('rail') && Math.round(document.querySelector('#side').getBoundingClientRect().width) === 280 && !!document.querySelector('#side .side-title')"))
    c.shot("phone-menu")
    c.js("Object.assign(shell, { w: 240, rail: false }); keepShell(); applyShell();")
    c.js("document.body.classList.remove('side-open')")
    c.go(f"#/s/{ids[0]}", 4)
    check("a phone terminal has its key bar", c.js("getComputedStyle(document.querySelector('.keys')).display") == "flex")
    c.shot("phone-session")
    check("no script errors or policy violations", not c.errors, str(c.errors[:5]))
    # a page from before the server was updated: the next poll answers with another build, and the page says so
    c.js("(() => { const real = window.fetch; window.fetch = async (url, options) => { const answer = await real(url, options); if (!String(url).includes('/api/state')) return answer; const data = await answer.json(); data.version = 'another-build'; return new Response(JSON.stringify(data), { status: 200, headers: { 'Content-Type': 'application/json' } }); }; })()")
    stale = next((True for _ in range(40) if c.js("!document.querySelector('.banner.stale').hidden") or time.sleep(0.25)), False)
    check("a page from before the hub was updated says so and offers to reload", stale and c.js("document.querySelector('.banner.stale span').textContent").startswith("Dream Hub was updated.") and c.js("document.querySelector('.banner.stale button').textContent") == "Reload" and c.js("document.querySelector('.banner.stale').getBoundingClientRect().right <= innerWidth + 1"))
finally:
    try:
        chrome.terminate()
        chrome.wait()
        shutil.rmtree(profile, ignore_errors=True)
        for sid in ids:
            api("POST", f"/api/sessions/{sid}/dismiss", {})
    finally:  # whatever the server did or didn't answer
        left = remove_project(NAME)
        check("the throwaway project is gone", not left, str(left))
        check("launchd's folder holds what it held before", launchd_jobs() == launchd_before, str(sorted(set(launchd_jobs()) ^ set(launchd_before))))
print(f"--- failures: {failures}")
sys.exit(1 if failures else 0)

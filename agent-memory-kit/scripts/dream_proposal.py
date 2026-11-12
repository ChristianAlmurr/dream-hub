#!/usr/bin/env python3
"""The life of a proposal: open, then applied or dismissed, and what the person left unticked.

  python3 scripts/dream_proposal.py                    the path of the store's open proposal, or that there is none
  python3 scripts/dream_proposal.py --json             every proposal the store knows, oldest first, with its state, and the items left open
  python3 scripts/dream_proposal.py applied [numbers]  the last step of /dream apply: the open proposal is applied; the numbers are ticked items that could not be
                                                       (refused when nothing is ticked: dismiss closes a proposal without applying; and inside an unattended dream)
  python3 scripts/dream_proposal.py dismiss [name]     the person closes the open proposal without applying it
  python3 scripts/dream_proposal.py declined [file]    the items the person has declined, for one project/ file or for all
  python3 scripts/dream_proposal.py forget <words>     the person takes one item off the declined list, so a dream may propose it again
  python3 scripts/dream_proposal.py withdraw <words>   the person takes back one approved item that nobody has done yet
  python3 scripts/dream_proposal.py done <words>       the person closes one thing a dream said needs them (needs.py keeps those)

With no argument it also lists, after its first line, the items the person approved that are not done yet and
what dreams said needs the person and is not closed.

One place says what state a proposal is in: dreams/proposals.json in the store, written only here
and committed with every change, so the store's history shows when a proposal was written, applied
or dismissed. A proposal is open from the moment dream_finish.py commits it until the person has
dealt with it, and there are two ways to do that: `/dream apply`, which ends with `applied`, or
`dismiss`. Age alone closes nothing: an old proposal with nothing ticked is still open.

Who knows what. The proposal file says what was proposed and what the person ticked; nothing here
changes it. The apply record beside it (<proposal stem>-applied.json, written by the apply step when
there is one) says what became of each ticked item, and when it exists it alone decides which
ticked items count as applied: those it calls `applied` or `already applied`. The rest are carried
on as ticked, among them `yours`, a change to ORG.md or Steering that the person has still to make;
item_done() drops one once it has been done. This file's record says the proposal's state and what is carried on.

A store has one open proposal at most. While it has one, no dream starts on it: the collector
stops, the daily check answers not due and reminds the person, and run-dream.sh turns a start by
hand away. A dream is paid for and its proposal would push the open one out of sight with the
person's ticks in it; dismissing takes one command and loses nothing, because of the next rule.

What was not applied is not lost with the proposal. A dream moves the store's last_dream to the
moment it collected, so the sessions behind an item are never read again. When a proposal is
closed, each numbered item that was not applied is kept (`left`) and the collector hands it to the
next dream (_left-open.md), which proposes it again only if what it reads still supports it.
An item the person ticked that the apply step could not do by itself is approved and waits: a change to
ORG.md or Steering waits for the person, a `needs a session` item for a session (`waiting` in its entry).
It stays, through any number of later proposals, until it is done (item_done) or the person withdraws it
(`withdraw`). No dream proposes it again meanwhile: it is handed to the dream as approved, and
proposal_check.py refuses a repeat. A ticked item the store refused can't be done as written by anyone,
so it does not wait: like an unticked one it goes to the next dream once, without a no.
Only one thing counts as the person saying no: applying a proposal with the item left unticked.
They read it and passed it over. A dismissal says nothing about any item, and neither does a tick
that could not be applied. An item with DECLINED noes is declined: no dream proposes it again, and
proposal_check.py refuses a proposal that does. `forget` takes it off that list.

Beside a proposal sits its side file (side_file(): <stem>-notes.md), written by the same dream and
committed with it: what the dream read and did not propose. Nothing in it is ticked or applied.

Three kinds of tick line are not ordinary items. A tick line under "Needs you" is no item at all: that
section holds no memory (needs.py). A `carry` item (`notes <note file> — carry — <n> checked facts`) moves
the checked facts of one of Claude's own notes with one tick: each fact is a line under it,
`+ project/<file>.md "<line>"`, and the person turns a `+` into a `-` to leave that fact out. Its id
(item_id) is made of the facts without their signs, so an item whose signs were changed is still the item
the dream wrote; its key is the note file, so what is left or declined is the note, not a wording. And an
item of the last section, UNCHECKED, is a fact nothing could check: ticked, it is applied as the line it
quotes; unticked when the proposal is applied, it is a no that is not asked again, and is not carried.

Whether two items are the same is decided by what they are about, not by their bytes (key()): the
file, the action and the line without its date and its "(checked: …)" or "(by …)" ending, with
case and spacing folded. A dream that says the same thing in other words is not caught by this;
the list it is handed is what stops that. A declined item is kept for KEPT_DAYS and then dropped.

Names are unique: the collector asks name_for() and writes the answer into the batch, and
dream_finish.py commits the proposal under that name. Nothing looks for "the newest proposal" by
sorting file names or times; everything asks here.

A store from before this file has no dreams/proposals.json. Until the first change is written it
is read as holding one proposal, the newest that a dream committed, if its file is still there. It
is applied if the history after it shows the old kit's apply (a commit by the dreamer, or one that
records the approvals), and open otherwise.
"""
import datetime, hashlib, json, os, re, sys
from pathlib import Path

import memcheck
import needs

MEM = memcheck.MEM
LEDGER = Path("dreams") / "proposals.json"
STATES = ("open", "applied", "dismissed")
DECLINED = 2  # an item the person passed over this many times when applying is not proposed again
KEPT_DAYS = 180  # a declined item is held against later proposals this long
NAME = re.compile(r"\d{4}-\d{2}-\d{2}(-\d+)?-proposal\.md")
SUFFIX, SIDE = "-proposal.md", "-notes.md"  # a proposal, and the file beside it for what the dream read and did not propose
CARRY = "carry"  # the action of an item that moves the checked facts of one note
FACT = re.compile(r'^([+-]) (.+?\.md) "(.*)"(?: · machine: (\d+))?$')  # a line under a carry item: taken or left out, the file, the line, the check on this machine that confirms it
UNCHECKED = "Unchecked facts from Claude's own notes"  # the last section: facts nothing could check, one tick each
TICK = re.compile(r"^- \[([ xX])\] (\d+)\. (.+)$")
LEFT = re.compile(r"^left open: (\d+)")  # a line under an item: how many noes it had before
HANDOVER = "_left-open.md"  # in the batch
DREAM_COMMIT = r"^dream: .*-proposal\.md$"
APPLIED_BEFORE = re.compile(r"^memory\(dreamer\): |^dream\b.*approv", re.I)  # how an apply under the kit before this one shows in a store's history
# Results in an apply record that mean the tick was dealt with. The others (`refused`, `needs a session`, and `yours`: an ORG.md or
# Steering change the person has still to make themselves) are carried as ticked items, without a no, until item_done() says otherwise.
DONE = ("applied", "already applied")
ENTRY = ("item", "key", "id", "detail", "left", "ticked", "from", "number", "at", "waiting")
ANY = re.compile(r"^- \[([ xX])\] (.*)$")  # any tick line; one with a number is `<n>. <item>`
NUMBERED = re.compile(r"^(\d+)\.\s+(.*)$")
WAITS = (None, "you", "a session")  # `waiting` in a carried entry: nobody, or who has still to do what the person approved
WAIT_DAYS = 3  # an approved change that has waited this long is said in the daily check's reminder
WAYS = "`/dream apply` after ticking what you want, or `python3 scripts/dream_proposal.py dismiss`"


def now() -> float:
    """The kit's clock (archive.now). Asked for here and not at the top: archive.py loads memwrite.py, which loads build_index.py, which loads this file."""
    import archive
    return archive.now()


def number(value) -> bool:
    return type(value) in (int, float) and value == value and abs(value) != float("inf")


def claim(line: str) -> str:
    """What a memory line says, without what only says when and how it is known: its dash and date and its "(checked: …)" or "(by …)" ending.

    A pinned line keeps its pin: the item that pins a line is not the item that corrects it."""
    line = re.sub(r"^-\s*", "", line.strip().strip('"').strip())
    line = re.sub(r"^\[\d{4}-\d{2}-\d{2}\]\s*", "", line)
    line, pin = re.subn(r"\s+\(pin\)$", "", line)  # an item that pins a line is about that line, pinned: the same item whatever evidence stands before the pin
    return re.sub(r"\s*\((checked|by)\b[^()]*\)\s*$", "", line) + (" (pin)" if pin else "")


def key(item: str) -> str:
    """What an item is about, for telling whether two items are the same one. See the top of this file for what it does and doesn't catch."""
    parts = item.split(" — ", 2)
    if len(parts) != 3:
        return " ".join(item.casefold().split())
    if parts[1].strip() == CARRY:  # one note's checked facts: the note, however many facts the item now counts
        return " ".join(f"{parts[0]} — {CARRY}".casefold().split())
    return " ".join(f"{parts[0]} — {parts[1]} — {claim(parts[2])}".casefold().split())


def before_ledger(mem: Path) -> dict:
    """A store with no ledger yet: its newest committed proposal, applied if the history after it shows the old kit's apply, else open."""
    r = memcheck.git(mem, "log", "-1", "--format=%H%x09%ct%x09%s", "--grep", DREAM_COMMIT)
    commit, _, rest = r.stdout.strip().partition("\t")
    when, _, subject = rest.partition("\t")
    name = subject[len("dream: "):]
    if not (r.returncode == 0 and when.isdigit() and NAME.fullmatch(name) and (mem / "dreams" / name).is_file()):
        return {"proposals": [], "left": []}
    later = [row.partition("\t") for row in memcheck.git(mem, "log", "--format=%ct%x09%s", f"{commit}..HEAD").stdout.splitlines()]
    applied = [int(at) for at, _, said in later if at.isdigit() and APPLIED_BEFORE.search(said)]
    return {"proposals": [{"name": name, "written": int(when), "state": "applied" if applied else "open", "closed": max(applied) if applied else None}], "left": []}


def load(mem: Path) -> dict:
    """{"proposals": [{"name", "written", "state", "closed"}], "left": [{"item", "key", "detail", "left", "ticked", "from", "number", "at"}]}, oldest first."""
    path = mem / LEDGER
    try:
        text = path.read_text()
    except FileNotFoundError:
        return before_ledger(mem)
    except OSError as error:
        raise SystemExit(f"{path} can't be read ({type(error).__name__}), so there is no telling which proposal is open.")
    try:
        found = json.loads(text)
    except ValueError:
        found = None
    proposals, left = (found.get("proposals"), found.get("left")) if isinstance(found, dict) else (None, None)
    for entry in left if isinstance(left, list) else []:
        if isinstance(entry, dict) and not ("waiting" in entry and "id" in entry):
            # Written before entries said which item they are and who they wait for. The proposal says which item has that number,
            # and what its apply record says about that item decides whether it waits, as it would have then.
            was = next((found for found in items(read_proposal(mem, entry.get("from", ""))) if found["number"] == entry.get("number")), None)
            entry.setdefault("id", was["id"] if was else "")
            if "waiting" not in entry:
                entry["waiting"] = waits((rows_of(mem, entry.get("from", "")) or {}).get(entry["id"])) if entry.get("ticked") is True else None
    good = (isinstance(proposals, list) and isinstance(left, list)
            and all(isinstance(p, dict) and isinstance(p.get("name"), str) and NAME.fullmatch(p["name"]) and number(p.get("written"))
                    and p.get("state") in STATES and (p.get("closed") is None or number(p["closed"])) for p in proposals)
            and sum(p["state"] == "open" for p in proposals) <= 1
            and all(isinstance(e, dict) and set(e) == set(ENTRY) and all(isinstance(e[k], str) for k in ("item", "key", "id", "from"))
                    and isinstance(e["detail"], list) and all(isinstance(d, str) for d in e["detail"])
                    and type(e["left"]) is int and e["left"] >= 0 and (e["number"] is None or type(e["number"]) is int) and isinstance(e["ticked"], bool) and number(e["at"])
                    and e["waiting"] in WAITS and (e["ticked"] or e["waiting"] is None) for e in left))
    if not good:
        raise SystemExit(f"{path} is not a record of this store's proposals that this version can read. "
                         "Correct it, or restore it from the store's history (git -C memory log -- dreams/proposals.json).")
    return {"proposals": proposals, "left": left}


def problem(mem: Path) -> str:
    """Why the record can't be used, or empty. For those that must go on without it and say so: the index, memcheck."""
    try:
        load(mem)
    except SystemExit as stop:
        return str(stop)
    return ""


def write(mem: Path, ledger: dict):
    """Write the ledger whole. Called under the store's lock, by whoever commits it next."""
    path = mem / LEDGER
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(ledger, indent=1, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def open_one(mem: Path):
    """The store's open proposal, or None."""
    return next((p for p in load(mem)["proposals"] if p["state"] == "open"), None)


def newest(mem: Path):
    """The proposal written last, whatever its state, or None. The one answer to "the newest proposal"."""
    proposals = load(mem)["proposals"]
    return proposals[-1] if proposals else None


def name_for(mem: Path, at: float) -> str:
    """The file name for a proposal collected at this moment: its date, and a number from the second of a day on. Never one that a record or a file has."""
    date = datetime.date.fromtimestamp(at).isoformat()
    taken = {p["name"] for p in load(mem)["proposals"]}
    count = 1
    while True:
        name = f"{date}-proposal.md" if count == 1 else f"{date}-{count}-proposal.md"
        if name not in taken and not (mem / "dreams" / name).exists():
            return name
        count += 1


def age(record: dict) -> str:
    """"of 8 Nov, open for 3 days": for every sentence that names an open proposal."""
    written, at = record["written"], now()
    day = datetime.datetime.fromtimestamp(written)
    days = max(0, (datetime.date.fromtimestamp(at) - day.date()).days)
    months = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
    return f"of {day.day} {months[day.month - 1]}, open for {days} day{'' if days == 1 else 's'}"


def waiting_words(record: dict) -> str:
    """For whoever is turned away by an open proposal: which one, how old, and the two ways to deal with it."""
    return (f"The proposal {record['name']} ({age(record)}) has not been applied or dismissed. Tick what you want in it and run /dream apply, "
            "or close it with `python3 scripts/dream_proposal.py dismiss`: every item in it is then handed to the next dream.")


def opened(mem: Path, name: str, written: float):
    """dream_finish.py, under the store's lock, as it commits a proposal: the store now has this one open."""
    ledger = load(mem)
    held = next((p for p in ledger["proposals"] if p["state"] == "open"), None)
    if held:
        raise SystemExit(f"{waiting_words(held)} Nothing was committed.")
    ledger["proposals"].append({"name": name, "written": int(written), "state": "open", "closed": None})
    write(mem, ledger)  # the dream's own commit takes it, with the proposal


def item_id(text: str, details: list) -> str:
    """Which item this is: its tick line after the box, and the lines under it, exactly as written but for the signs of a carry item's facts.
    dream_apply.py names items the same way.

    A number is a label for people: an item that is rewritten keeps its number and is another item.
    """
    if f" — {CARRY} — " in text:  # the person turns a + into a - to leave a fact out: still the item the dream wrote
        details = [("+" + row[1:]) if FACT.match(row) else row for row in details]
    return hashlib.sha256("\n".join([text] + details).encode()).hexdigest()[:12]


def items(text: str) -> list:
    """Every tick line of a proposal written as an item: {"number", "ticked", "item", "detail", "id", "section"}.

    `number` is None for an item that has none (a scratchpad to condense, an inbox item). `item` is the
    line after its number, `detail` the indented lines under it, `id` what item_id() makes of the two,
    `section` the heading it stands under.
    A tick line under "Needs you" is not an item: that section holds no memory, so nothing there is applied, carried or declined.
    """
    found, lines, heading = [], text.split("\n"), ""
    for at, line in enumerate(lines):
        if line.startswith("## "):
            heading = line[3:].strip().replace("\u2019", "'")
        tick = ANY.match(line)
        if not tick or heading.startswith(needs.HEADING):
            continue
        end = at + 1
        while end < len(lines) and lines[end].startswith("      "):
            end += 1
        details = [row.strip() for row in lines[at + 1:end]]
        numbered = NUMBERED.match(tick[2])
        found.append({"number": int(numbered[1]) if numbered else None, "ticked": tick[1] != " ", "item": (numbered[2] if numbered else tick[2]).strip(),
                      "detail": [row for row in details if row], "id": item_id(tick[2], details), "section": heading})
    return found


def section(text: str, heading: str) -> list:
    """(line number counted from 1, line) for every line under the first `## ` heading that starts with `heading`, blank lines left out."""
    found, inside = [], None
    for at, line in enumerate(text.split("\n"), 1):
        if line.startswith("## "):
            if inside:
                break
            inside = line[3:].replace("\u2019", "'").startswith(heading)
        elif inside and line.strip():
            found.append((at, line))
    return found


def side_file(name: str):
    """The name of the file beside a proposal that holds what its dream read and did not propose, or None for a name that is no proposal's."""
    return name[:-len(SUFFIX)] + SIDE if name.endswith(SUFFIX) else None


def read_proposal(mem: Path, name: str) -> str:
    """A proposal's text, or empty when the file is not there."""
    try:
        return (mem / "dreams" / name).read_text(errors="ignore") if NAME.fullmatch(name) else ""
    except OSError:
        return ""


def apply_record(mem: Path, name: str) -> Path:
    return mem / "dreams" / (name[:-len(SUFFIX)] + "-applied.json")


def rows_of(mem: Path, name: str):
    """The apply record of a proposal, when the apply step wrote one: {item id: its row}. None when there is no record.

    By the item's id and never by its number: after an item is rewritten, the record still holds a row for
    what the number used to mean.
    """
    path = apply_record(mem, name)
    try:
        return {row["key"]: row for row in json.loads(path.read_text())["items"] if isinstance(row["result"], str) and isinstance(row["key"], str)}
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError):
        raise SystemExit(f"{path} can't be read as an apply record, so there is no telling which ticked items were applied. Nothing was recorded.")


def waits(row):
    """Who has still to do a ticked item that was not applied: "you", "a session", or None when nobody can do it as it is written.

    From the item's row in the apply record. A change to ORG.md or Steering is the person's, whether the
    apply step left it for them (`yours`) or their own pass could not place it (`refused`): it waits for
    them. `needs a session` waits for a session. Anything else that was ticked and not applied was refused
    by the store, or has no row: nobody can do that as written, so it does not wait; it goes to the next
    dream once, which may propose it in a form that can be applied.
    """
    if row is None or row["result"] in DONE:
        return None
    if row["result"] == "yours" or row.get("kind") in ("org", "steering"):
        return "you"
    return "a session" if row["result"] == "needs a session" else None


def stuck(mem: Path, entry: dict) -> str:
    """Why an entry that waits can no longer be done by the command that would do it, or empty when it can.

    The commands work from the proposal and its apply record. If the proposal is gone, the item in it was
    changed, or the record no longer says the item waits, nothing will ever offer it: the person is told
    so, and that withdrawing is what clears it.
    """
    text = read_proposal(mem, entry["from"])
    if not text:
        return f"its proposal {entry['from']} is gone"
    if not any(found["id"] == entry["id"] and found["ticked"] for found in items(text)):
        return f"it is no longer a ticked item of {entry['from']} as it was approved"
    try:
        row = (rows_of(mem, entry["from"]) or {}).get(entry["id"])
    except SystemExit:
        return f"the apply record of {entry['from']} can't be read"
    if entry["waiting"] == "you":  # the person's pass works from the proposal: only a record that calls the item done keeps it from offering it
        return f"the apply record of {entry['from']} says `{row['result']}` for it" if row is not None and row["result"] in DONE else ""
    if row is None:
        return f"the apply record of {entry['from']} has no row for it"
    return "" if waits(row) == "a session" else f"the apply record of {entry['from']} says `{row['result']}` for it"


def orphans(mem: Path) -> list:
    """Proposal files in dreams/ that git doesn't track, no record knows and no batch names: left by a dream that never finished.

    Nothing deletes one of these by itself: memcheck.py and the collector say they are there.
    """
    try:
        current = json.loads((mem / "dreams" / "_batch" / "_timing.json").read_text())["proposal"]
    except (OSError, ValueError, KeyError, TypeError):
        current = None  # no dream in progress has named one
    known = {p["name"] for p in load(mem)["proposals"]} | {current}
    listed = memcheck.git(mem, "ls-files", "--others", "--exclude-standard", "--", "dreams").stdout.splitlines()
    return sorted(Path(row).name for row in listed if NAME.fullmatch(Path(row).name) and Path(row).name not in known)


def read_only(what: str = "Closing a proposal, or changing what is declined, is for the person and for /dream apply"):
    """An unattended dream only proposes: it closes nothing and forgets nothing, like memwrite.py, which refuses its writes.

    The one rule for every command that is the person's. store.py and interpreter.py ask it too, each with its own words for `what`.
    """
    if os.environ.get("AGENT_MEMORY_PROPOSE_ONLY"):
        raise SystemExit(f"READ-ONLY: this dream only proposes. {what}. Nothing was recorded.")


def old(entry: dict, at: float) -> bool:
    return at - entry["at"] > KEPT_DAYS * 86400


def close(mem: Path, name, state: str, failed=()) -> dict:
    """Close the open proposal and keep what was not applied. Returns {"proposal", "state", "left", "declined", "gone", "waiting", "now_declined", "unasked"}.

    The proposal file is committed with the change of state, as the person left it: their ticks, or its absence.
    """
    read_only()
    with memcheck.locked(mem):
        ledger = load(mem)
        record = next((p for p in ledger["proposals"] if p["state"] == "open"), None)
        if record is None:
            raise SystemExit(f"{mem.name} has no open proposal.")
        if name is not None and name != record["name"]:
            raise SystemExit(f"{name} is not the open proposal of {mem.name}: that is {record['name']}.")
        path, at, gone = mem / "dreams" / record["name"], now(), False
        try:
            text = path.read_text(errors="ignore")
        except OSError as error:
            if state != "dismissed" or not isinstance(error, FileNotFoundError):
                raise SystemExit(f"{path} can't be read ({type(error).__name__}), so there is no telling what was ticked. Nothing was recorded. "
                                 f"Put the file back (git -C memory checkout -- dreams/{record['name']}), "
                                 "or close the proposal with `python3 scripts/dream_proposal.py dismiss`, which then carries nothing over.")
            text, gone = "", True  # the person closes a proposal whose file is gone: there is nothing to carry
        rows = rows_of(mem, record["name"]) if state == "applied" else None
        if rows is not None and failed:
            raise SystemExit(f"{apply_record(mem, record['name']).name} says what became of each ticked item, so no numbers are taken beside it. Nothing was recorded.")
        if state == "applied" and not any(found["ticked"] for found in items(text)):
            raise SystemExit(f"Nothing is ticked in {record['name']}, so there is nothing to apply, and it was not closed. If you ticked items somewhere that "
                             "has not saved them, save and apply again. The one way to close a proposal without applying it is to dismiss it "
                             "(`python3 scripts/dream_proposal.py dismiss`): every item then goes to the next dream and none counts as a no.")
        before = {entry["key"]: entry for entry in ledger["left"]}
        kept, unasked = {}, 0
        for found in items(text):
            row = (rows or {}).get(found["id"])  # by the item, not its number: a rewritten item is another item
            wait = waits(row) if state == "applied" and found["ticked"] else None
            if found["number"] is None and not wait:
                continue  # an item without a number is carried only when the person approved it and it waits for someone
            if found["section"].startswith(UNCHECKED) and not found["ticked"]:
                unasked += 1
                continue  # a fact nothing could check, and the person did not vouch for: asked once, and its note says so in the side file
            applied = found["ticked"] and (row is not None and row["result"] in DONE if rows is not None else found["number"] not in failed)
            if state == "applied" and applied:
                continue
            same = key(found["item"])
            said = [int(LEFT.match(d)[1]) for d in found["detail"] if LEFT.match(d)]
            noes = max(said + [before[same]["left"] if same in before else 0])
            no = state == "applied" and not found["ticked"]  # read and passed over. Nothing else is a no
            kept[same] = {"item": found["item"], "key": same, "id": found["id"], "detail": [d for d in found["detail"] if not LEFT.match(d)],
                          "left": noes + 1 if no else noes, "ticked": found["ticked"], "from": record["name"], "number": found["number"], "at": int(at),
                          "waiting": wait}
        # What the person approved and nobody has done yet stays until it is done (item_done) or withdrawn, however many proposals
        # open and close meanwhile: the approval was given once. What was declined stays, for a time, so no later dream proposes it.
        # What was only carried for another look, and not proposed again, was no longer supported.
        stays = [e for e in ledger["left"] if e["waiting"]]
        taken = {e["key"] for e in stays}
        kept = {same: e for same, e in kept.items() if same not in taken}
        ledger["left"] = stays + list(kept.values()) + [e for e in ledger["left"] if not e["waiting"] and e["left"] >= DECLINED and e["key"] not in kept and not old(e, at)]
        record.update(state=state, closed=int(at))
        write(mem, ledger)
        # With the state: the proposal as the person left it (ticked, or deleted) and the apply record. A path git has never seen and that is gone is left out.
        paths = [str(LEDGER)] + [rel for rel in (f"dreams/{record['name']}", str(apply_record(mem, record["name"]).relative_to(mem)))
                                 if (mem / rel).exists() or memcheck.git(mem, "ls-files", "--error-unmatch", "--", rel).returncode == 0]
        error = memcheck.commit(mem, f"proposal: {state} {record['name']}", paths)
        if error:
            raise SystemExit(f"{mem / LEDGER} says {record['name']} is {state}, but the commit failed: {error}")
    return {"proposal": record["name"], "state": state, "left": sum(e["left"] < DECLINED and not e["waiting"] for e in kept.values()),
            "declined": sum(e["left"] >= DECLINED for e in kept.values()), "gone": gone, "waiting": sum(bool(e["waiting"]) for e in kept.values()),
            "now_declined": [e["item"] for same, e in kept.items() if e["left"] >= DECLINED and (same not in before or before[same]["left"] < DECLINED)],
            "unasked": unasked}


def mark_applied(mem: Path, proposal: str = None, failed=()) -> dict:
    """For the apply step, once every ticked item has been written or reported: the store's open proposal is applied.

    `proposal` is its file name, or None for whichever is open. `failed` holds the numbers of ticked items
    that could not be applied; with an apply record beside the proposal it must be empty, because the
    record says it. Items that were not applied are handed to the next dream; the unticked ones count as
    one no each. Takes the store's lock and commits, so call it after the last memwrite and outside any
    lock of your own. Stops with a plain message when `proposal` is not the store's open proposal, when
    nothing in it is ticked (a proposal is closed without applying by dismissing it), and inside an unattended dream.
    Returns {"proposal", "state", "left", "declined", "gone", "waiting", "now_declined", "unasked"}: `left` items go to the next dream
    for another look, `waiting` ones are approved and stay until item_done() or the person's withdrawal; `unasked` unchecked facts left unticked are dropped.
    """
    return close(mem, proposal, "applied", {int(n) for n in failed})


def dismiss(mem: Path, proposal: str = None) -> dict:
    """The person closes the open proposal without applying it. Every item is handed to the next dream, and none counts as a no."""
    return close(mem, proposal, "dismissed")


def item_done(mem: Path, proposal: str, which) -> bool:
    """A ticked item of a closed proposal that was carried on has now been done (the person made the ORG.md change, a session applied what needed one).

    `proposal` is the proposal's file name; `which` is the item's id (item_id(), as in the apply record's `key`), or its number as an int. The carried entry is dropped,
    so the next dream is not handed it. Takes the store's lock and commits. Returns False, with nothing
    changed, when no carried entry is that item (it was dropped already, or was never carried).
    """
    read_only()
    with memcheck.locked(mem):
        ledger = load(mem)
        match = [e for e in ledger["left"] if e["from"] == proposal and (e["number"] == which if type(which) is int else e["id"] == which)]
        if not match:
            return False
        ledger["left"] = [e for e in ledger["left"] if e not in match]
        write(mem, ledger)
        error = memcheck.commit(mem, f"proposal: {label(match[0])} of {proposal} was done", [str(LEDGER)])
        if error:
            raise SystemExit(f"{mem / LEDGER} was written, but the commit failed: {error}")
    return True


def label(entry: dict) -> str:
    """ "item 4", or for an item without a number its first words. """
    return f"item {entry['number']}" if entry["number"] is not None else f"\"{entry['item'].split(' — ')[0][:60]}\""


def waiting(mem: Path) -> list:
    """The carried items the person approved and that nobody has done yet, oldest first."""
    return sorted((e for e in load(mem)["left"] if e["waiting"]), key=lambda e: e["at"])


def how(entry: dict) -> str:
    """What gets an approved item done, for the person."""
    if entry["waiting"] == "you":
        return "it is yours to make, in a terminal: chflags nouchg memory/ORG.md, then python3 scripts/dream_apply.py --yours"
    return "a session does it: /dream apply in a session"


def waiting_lines(mem: Path) -> list:
    """For the person: each approved item that waits, the proposal it came from, how long it has waited and what gets it done."""
    found, at = waiting(mem), now()
    lines = []
    for e in found:
        gone = stuck(mem, e)
        lines.append(f"  {e['item']}\n      approved in {e['from']}, {max(0, int((at - e['at']) // 86400))} days ago; "
                     + (f"nothing can do it any more: {gone}. Clear it with withdraw, below" if gone else how(e)))
    return lines and ["Approved and waiting to be done:"] + lines + [
        "To take one back: python3 scripts/dream_proposal.py withdraw \"<words only it holds>\""]


def withdraw(mem: Path, words: str) -> str:
    """The person takes back an approval that has not been carried out. Returns the item."""
    read_only()
    with memcheck.locked(mem):
        ledger = load(mem)
        match = [e for e in ledger["left"] if e["waiting"] and words.casefold() in e["item"].casefold()]
        if len(match) != 1:
            listed = "".join(f"\n  {e['item']}" for e in match)
            raise SystemExit(f"{len(match)} approved items that wait hold \"{words}\", and one is withdrawn at a time. Nothing was changed." + listed
                             + ("" if match else " `python3 scripts/dream_proposal.py` lists them."))
        ledger["left"] = [e for e in ledger["left"] if e is not match[0]]
        write(mem, ledger)
        error = memcheck.commit(mem, f"proposal: {label(match[0])} of {match[0]['from']} was withdrawn", [str(LEDGER)])
        if error:
            raise SystemExit(f"{mem / LEDGER} was written, but the commit failed: {error}")
    return match[0]["item"]


def declined(mem: Path) -> dict:
    """{key: entry} for the items the person has declined and that are still held against a proposal."""
    at = now()
    return {e["key"]: e for e in load(mem)["left"] if e["left"] >= DECLINED and not old(e, at)}


def forget(mem: Path, words: str) -> str:
    """The person takes one declined item off the list. Returns the item."""
    read_only()
    with memcheck.locked(mem):
        ledger = load(mem)
        match = [e for e in ledger["left"] if e["left"] >= DECLINED and words.casefold() in e["item"].casefold()]
        if len(match) != 1:
            listed = "".join(f"\n  {e['item']}" for e in match)
            raise SystemExit(f"{len(match)} declined items hold \"{words}\", and one is forgotten at a time. Nothing was changed." + listed
                             + ("" if match else " `python3 scripts/dream_proposal.py declined` lists them."))
        ledger["left"] = [e for e in ledger["left"] if e is not match[0]]
        write(mem, ledger)
        error = memcheck.commit(mem, "proposal: a declined item may be proposed again", [str(LEDGER)])
        if error:
            raise SystemExit(f"{mem / LEDGER} was written, but the commit failed: {error}")
    return match[0]["item"]


def target(entry: dict) -> str:
    return entry["item"].split(" — ", 1)[0]


def handover(mem: Path) -> str:
    """_left-open.md for the batch: what closed proposals left unapplied, for the next dream. The declined items only as counts."""
    left, at = load(mem)["left"], now()
    again = [e for e in left if e["left"] < DECLINED and not e["waiting"]]
    approved = [e for e in left if e["waiting"]]
    held = list(declined(mem).values())
    aged = sum(e["left"] >= DECLINED and old(e, at) for e in left)
    out = ["# Left open last time (written by scripts/collect_transcripts.py from dreams/proposals.json and dreams/needs.json)", "",
           "These items were in a proposal that was closed, and were not applied. The sessions behind them are not in this batch.",
           "Each part says what to do with it. Don't count an approved item in `<M>`."]
    out += needs.handover(mem)
    out += ["", "## To propose again, if what this dream reads still supports them",
            "Test each the way its `why:` says it was found: against the code, against `memory/project/`, or with `python3 scripts/past.py \"<terms>\"`. "
            "Still true, still missing and still worth a line: propose it again, in the same words where they still hold, "
            "with the line `left open: <the number it has here>` after its `why:` line. No longer true, or memory has it now: leave it out.",
            "A `carry` item is one note's facts: propose it again with the facts it still has, each still to be checked as before."]
    for e in again:
        out.append(f"- {e['item']}")
        out += [f"      {d}" for d in e["detail"]]
        out.append(f"      left open: {e['left']}" + (" (it was ticked, and not applied)" if e["ticked"] else ""))
    if not again:
        out.append("None.")
    out += ["", "## Approved and waiting to be done. Leave these out of the proposal: the person has said yes, and must not be asked again"]
    for e in approved:
        out.append(f"- {e['item']}")
        out.append(f"      approved in {e['from']} as {label(e)}, waits for {e['waiting']}")
    if approved:
        out.append(f"If what you read shows that one of these is no longer true, say so under \"{needs.HEADING}\", with its proposal and number, "
                   "so the person can withdraw it. Don't propose it again and don't drop it.")
    else:
        out.append("None.")
    out += ["", f"## Declined: passed over {DECLINED} times or more when a proposal was applied. Never propose these again",
            f"- In all: {len(held)}"]
    for file in sorted({target(e) for e in held}):
        out.append(f"- {file}: {sum(target(e) == file for e in held)}. Before you propose a change to this file, read them: `python3 scripts/dream_proposal.py declined {file}`")
    if aged:
        out.append(f"- Dropped for age: {aged} declined more than {KEPT_DAYS} days ago and no longer held against a proposal. Say this number in the Summary.")
    return "\n".join(out) + "\n"


def main():
    args = sys.argv[1:]
    if args[:1] == ["dismiss"] and len(args) <= 2:
        done = dismiss(MEM, *args[1:])
        print(f"dismissed: {done['proposal']}. " + ("Its file is gone, so nothing could be handed to the next dream." if done["gone"] else
              f"{done['left'] + done['declined']} of its items are handed to the next dream, and none of them counts as declined by this."))
    elif args[:1] == ["applied"] and all(n.isdigit() for n in args[1:]):
        done = mark_applied(MEM, None, args[1:])
        print(f"applied: {done['proposal']}. {done['left']} of its items were not applied and are handed to the next dream; "
              + (f"{done['unasked']} unchecked facts from Claude's own notes were left unticked: a no, not asked again; " if done["unasked"] else "")
              + (f"{done['waiting']} approved and not done yet stay until done or withdrawn (`python3 scripts/dream_proposal.py` lists them); " if done["waiting"] else "")
              + f"{done['declined']} have now been passed over {DECLINED} times and are declined" + (":" if done["now_declined"] else "."))
        for item in done["now_declined"]:
            print(f"  {item}")
        if done["now_declined"]:
            print("No dream proposes these again. If one should come back, the person runs `python3 scripts/dream_proposal.py forget \"<words only it holds>\"`.")
    elif args[:1] == ["declined"] and len(args) <= 2:
        held = [e for e in declined(MEM).values() if len(args) == 1 or target(e) == args[1]]
        print("\n".join(f"- {e['item']}" for e in held) or "None.")
    elif args[:1] == ["withdraw"] and len(args) == 2:
        print(f"withdrawn: {withdraw(MEM, args[1])}\nIt is no longer waiting to be done. A dream may propose it again.")
    elif args[:1] == ["forget"] and len(args) == 2:
        print(f"forgotten: {forget(MEM, args[1])}\nA dream may propose it again.")
    elif args[:1] == ["done"] and len(args) == 2:
        print(f"closed: {needs.done(MEM, args[1])}\nIt is off the record. A dream says it again only if its sessions show it again.")
    elif args == ["--json"]:
        print(json.dumps(load(MEM), ensure_ascii=False))
    elif not args:
        held = open_one(MEM)
        print(f"{MEM / 'dreams' / held['name']} ({age(held)})" if held else f"{MEM.name} has no open proposal.")
        after = waiting_lines(MEM) + needs.lines(MEM, now())  # after the first line, which is all a program reads
        print("\n".join(after), end="\n" if after else "")
    else:
        sys.exit("usage: dream_proposal.py [--json | applied [numbers] | dismiss [<proposal file name>] | declined [<file>] | forget <words> | withdraw <words> | done <words>]")


if __name__ == "__main__":
    main()

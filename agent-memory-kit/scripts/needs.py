#!/usr/bin/env python3
"""What dreams said needs the person and is not memory: a job that is broken, a login that ran out, a credential to rotate.

A proposal's section "Needs you" holds such things as plain lines with no tick box. Nothing applies
them and no dream proposes them as items. They are kept here, in dreams/needs.json in the store,
which this module alone reads and writes: a file of its own, so a kit copy from before it existed,
which rewrites dreams/proposals.json whole, can't erase them.

  {"needs": [{"text", "key", "from", "at", "last", "seen", "said"}]}, oldest first

`text` is the line as it was last said, `key` its sentence in a normal form (key()), `from` and `at`
the proposal that first said it and when, `last` and `seen` the one that last said it and when,
`said` how many proposals have said it.

Their life. dream_finish.py records what a proposal says (record()) in the commit that makes the
proposal the store's open one, whatever becomes of that proposal afterwards. The next dream is
handed the open ones (handover(), in _left-open.md) and says one again, in the same words, only if
its sessions still show it: that is the same entry, said once more. When its sessions show the
thing put right, it writes `- resolved: <the same words> · evidence: <where it shows>` and the
entry is closed with that proposal: the person reads the line there and does nothing. The person
closes one themselves with `python3 scripts/dream_proposal.py done "<words only it holds>"`. One
that no dream has said for KEPT_DAYS is dropped when the next proposal is written. The daily
check says how many are open once the oldest has waited WAIT_DAYS (dream_due.py).

Whether two lines are the same need is decided by key(): the sentence before its evidence, with
case, punctuation and spacing folded. A dream that says the same thing in other words is not
caught by this; the list it is handed is what stops that, as for declined items.
"""
import json, os, re
from pathlib import Path

import memcheck

FILE = Path("dreams") / "needs.json"
HEADING = "Needs you"  # the section of a proposal
EVIDENCE = " · evidence: "  # what ends the sentence of a line there
RESOLVED = "resolved: "  # how a line starts that says an earlier need has been put right
KEPT_DAYS = 180  # one no dream has said for this long is dropped
WAIT_DAYS = 3  # one that has been open this long is said in the daily check's reminder
ENTRY = ("text", "key", "from", "at", "last", "seen", "said")


def key(line: str) -> str:
    """The sentence of a line, without its evidence and its `resolved:`, in a normal form: letters and digits, one space between words."""
    sentence = line.split(EVIDENCE)[0]
    sentence = sentence[len(RESOLVED):] if sentence.startswith(RESOLVED) else sentence
    return " ".join(re.sub(r"[^\w]+", " ", sentence.casefold()).split())


def parse(lines: list) -> tuple:
    """(said, resolved) from the lines of a proposal's "Needs you" section: each a list of {"text", "key"}.

    Only plain list lines count. A tick line there is not one of these: proposal_check.py refuses it and dream_apply.py never applies it.
    """
    said, resolved = [], []
    for line in lines:
        if not line.startswith("- ") or re.match(r"^- \[[ xX]\] ", line):
            continue
        text = line[2:].strip()
        if text:
            (resolved if text.startswith(RESOLVED) else said).append({"text": text, "key": key(text)})
    return said, resolved


def number(value) -> bool:
    return type(value) in (int, float) and value == value and abs(value) != float("inf")


def load(mem: Path) -> list:
    """The open needs, oldest first. A store that has no file yet has none."""
    path = mem / FILE
    try:
        found = json.loads(path.read_text())
    except FileNotFoundError:
        return []
    except (OSError, ValueError):
        found = None
    entries = found.get("needs") if isinstance(found, dict) else None
    if not (isinstance(entries, list) and all(isinstance(e, dict) and set(e) == set(ENTRY) and all(isinstance(e[k], str) and e[k] for k in ("text", "key", "from", "last"))
                                               and number(e["at"]) and number(e["seen"]) and type(e["said"]) is int and e["said"] >= 1 for e in entries)):
        raise SystemExit(f"{path} is not a record of what needs the person that this version can read. "
                         "Correct it, or restore it from the store's history (git -C memory log -- dreams/needs.json).")
    return entries


def write(mem: Path, entries: list):
    """Write the file whole. Called under the store's lock, by whoever commits it next."""
    path = mem / FILE
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"needs": entries}, indent=1, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def record(mem: Path, proposal: str, at: float, lines: list) -> dict:
    """dream_finish.py, under the store's lock, as it commits a proposal: what its "Needs you" lines say goes on record.

    Returns {"new", "again", "resolved", "aged"}: how many were said for the first time, said again,
    closed because the dream showed them put right, and dropped for age. Writes the file only when
    there is something to hold or the file is there already; the dream's own commit takes it.
    """
    said, resolved = parse(lines)
    entries = load(mem)
    known = {entry["key"]: entry for entry in entries}
    counts = dict(new=0, again=0, resolved=0, aged=0)
    for line in said:
        if line["key"] in known:
            entry = known[line["key"]]
            if entry["last"] != proposal:
                entry.update(text=line["text"], last=proposal, seen=int(at), said=entry["said"] + 1)
                counts["again"] += 1
        else:
            known[line["key"]] = {"text": line["text"], "key": line["key"], "from": proposal, "at": int(at), "last": proposal, "seen": int(at), "said": 1}
            counts["new"] += 1
    for line in resolved:
        if known.pop(line["key"], None):
            counts["resolved"] += 1
    kept = [entry for entry in known.values() if at - entry["seen"] <= KEPT_DAYS * 86400]
    counts["aged"] = len(known) - len(kept)
    if kept or (mem / FILE).exists():
        write(mem, kept)
    return counts


def done(mem: Path, words: str) -> str:
    """The person closes one need: it is dealt with, or they don't want it. Returns its text. Takes the store's lock and commits."""
    if os.environ.get("AGENT_MEMORY_PROPOSE_ONLY"):
        raise SystemExit("READ-ONLY: this dream only proposes. Closing what needs the person is the person's step. Nothing was recorded.")
    with memcheck.locked(mem):
        entries = load(mem)
        match = [e for e in entries if words.casefold() in e["text"].casefold()]
        if len(match) != 1:
            listed = "".join(f"\n  {e['text']}" for e in match)
            raise SystemExit(f"{len(match)} things that need you hold \"{words}\", and one is closed at a time. Nothing was changed." + listed
                             + ("" if match else " `python3 scripts/dream_proposal.py` lists them."))
        write(mem, [e for e in entries if e is not match[0]])
        error = memcheck.commit(mem, f"needs: what {match[0]['from']} said needs the person is closed", [str(FILE)])
        if error:
            raise SystemExit(f"{mem / FILE} was written, but the commit failed: {error}")
    return match[0]["text"]


def lines(mem: Path, at: float) -> list:
    """For the person: every open need, oldest first, with the proposal that said it, how often it was said and the command that closes it."""
    found = sorted(load(mem), key=lambda e: e["at"])
    out = [f"  {e['text']}\n      said in {e['from']}, {max(0, int((at - e['at']) // 86400))} days ago"
           + (f", and {e['said'] - 1} more time{'' if e['said'] == 2 else 's'}, last in {e['last']}" if e["said"] > 1 else "") for e in found]
    return out and [f"{HEADING} (not memory; no dream applies these):"] + out + [
        "When one is dealt with: python3 scripts/dream_proposal.py done \"<words only it holds>\""]


def handover(mem: Path) -> list:
    """The lines of _left-open.md that hand the open needs to the next dream, with the rule for them."""
    found = load(mem)
    out = ["", f"## {HEADING}: said by an earlier dream and not closed. Not memory: never propose one as an item"]
    for e in found:
        out += [f"- {e['text']}", f"      said in {e['from']}" + (f", {e['said']} times, last in {e['last']}" if e["said"] > 1 else "")]
    if not found:
        return out + ["None."]
    return out + [f"A session of this batch still shows one: put it under \"{HEADING}\" again, in the same words. "
                  f"A session shows one put right: write `- {RESOLVED}<the same words>{EVIDENCE}<where it shows>` there, and it is closed with this proposal. "
                  "Neither: leave it out. It stays on record until the person closes it."]

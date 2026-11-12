#!/usr/bin/env python3
"""PreToolUse hook for Bash. Settings deny rules only cover Claude's file tools, so without this
an agent could still run `echo x >> memory/ORG.md`.

It reads the command as shell: what each part writes to, following `cd`, redirects and `sh -c`.
It also covers .claude/commands/dream.md, whose Steering block is the person's and is loaded into
every session, and the store's STEERING.md, which takes its place: agents propose changes to them, they don't make them.
It covers the record of an apply (dreams/<date>-applied.json), which says what is done and is written by dream_apply.py alone.
And it keeps the shell from running what a memory line holds. In a command that runs memwrite.py: a heredoc whose delimiter
is not quoted is stopped, so is a backtick or $( anywhere the shell would act on it (outside single quotes and outside the
body of a quoted heredoc: a here-string, echo or printf with double quotes, a why in double quotes), and so is a heredoc
whose delimiter also stands on a line of the text, because the text would end there and the rest would run.
`amk write` and the other verbs of bin/amk that run memwrite.py are such commands, by any path to amk: which verbs
those are comes from the table amk itself runs from (verbs.py), so the two can't drift apart.
Which store a repository belongs to is the person's too: `amk attach`, `amk detach` (scripts/store.py)
and a `git config` that sets or removes the agent-memory.store key are stopped, as an edit of
roots.txt and a change of the memory link always were. So are the two other commands that are the person's
through amk: `amk apply --yours` and `amk interpreter record`. And taking back an approved item,
`dream_proposal.py withdraw`, however it is spelled.
What a reader of text can't see (amk under another name, a key written by a tool it doesn't know) is held
elsewhere: a key only works when the store's own roots.txt lists the repository, and roots.txt is guarded here.
It stops mistakes. It can't see inside a script file or a variable, so memcheck.py exists to
catch anything that gets past it.
"""
import fnmatch, json, os, re, shlex, sys

try:
    import verbs
except ImportError:
    verbs = None  # the kit's files here are incomplete. Nothing that touches memory passes until they are whole: see INCOMPLETE

TOP = ("ORG.md", "STEERING.md", "INDEX.md", "CHANGELOG.md", "reject.txt", "roots.txt", "notify.sh")
DREAM = re.compile(r"(?:^|/)(?:(?:\.claude/)?commands/dream|\.claude/dream-apply)\.md$")  # the command file, and the file it sends to for apply
STORE = re.compile(r"(?:^|/)(?:agent-memory/[^/]+|memory)(?:/(.*))?$")
EVERY_ARG = {"rm", "rmdir", "unlink", "truncate", "touch", "chmod", "chown", "chflags", "shred", "ln", "mv", "tee", "patch"}
LAST_ARG = {"cp", "install", "rsync"}
IN_PLACE = {"sed", "gsed", "perl", "ruby"}
PREFIXES = {"sudo", "command", "nohup", "time", "exec", "env", "nice"}
KEYWORDS = {"if", "then", "else", "elif", "fi", "while", "until", "do", "done", "!", "{", "}"}  # the shell's own words in front of a command
ENV_VALUED = {"-u", "--unset", "-C", "--chdir", "-S", "--split-string"}  # options of env whose next word is theirs
GIT_READS = {"log", "show", "diff", "status", "blame", "ls-files", "rev-parse", "grep", "cat-file", "shortlog", "describe", "ls-tree"}
SHELLS = {"sh", "bash", "zsh", "dash"}
RUNNERS = SHELLS | {"python", "python3", "node", "ruby", "perl"}
CODE_PATH = re.compile(r"(memory/|agent-memory/[^/\s'\"]+/)(ORG\.md|STEERING\.md|CHANGELOG\.md|INDEX\.md|project/|dreams/[\w.-]*-applied\.json)|commands/dream\.md|\.claude/dream-apply\.md")
APPLIED = "dreams/0-applied.json"  # stands for any apply record when a path holds a wildcard
# Every spelling that runs memwrite.py: the script by its name, and amk, by any path to it, with one of its verbs for that script.
AMK = r"(?:^|(?<=[\s;|&(/]))amk(?:\.py)?\s+"
WRITES = "write|edit|delete"  # the commands of memwrite.py whose stdin is memory text
BY_AMK = verbs.running("memwrite.py") if verbs else []
MEMORY_COMMAND = re.compile(r"memwrite\.py|dream_\w+\.py|store\.py|interpreter\.py|" + AMK)  # what is stopped whole while the table of verbs is missing
INCOMPLETE = ("Blocked: the kit's files in this repo are incomplete: scripts/verbs.py, which the guard reads amk's verbs from, is missing, "
              "so it can't tell which commands write memory and stops them all. Tell the person to run the kit's sync: "
              "sh <the kit's folder>/sync-kit.sh (the kit's folder is agent-memory-kit, by default ~/tools/agent-memory-kit)")
FEEDS = re.compile(r"\bpython3?\s+(\S*/)?scripts/memwrite\.py\s+(" + WRITES + r")\b"  # the heredoc is memory text for memwrite, not code
                   + "".join("|" + AMK + verb + r"\b" for verb in BY_AMK if verbs.VERBS[verb][1][0] in WRITES.split("|")))
RUNS = re.compile(r"memwrite\.py\s+(read|write|edit|delete|undo)\b" + "".join("|" + AMK + verb + r"\b" for verb in BY_AMK))
OPENS = re.compile(r"(?<!<)<<(?!<)(-?)[ \t]*(['\"\\]?)(\w+)")  # a heredoc: its dash, the quote before its delimiter, the delimiter
FORM = ("\n  python3 scripts/memwrite.py <verb> <file> --agent <name> --why '<one line, no apostrophe>' <<'MEMWRITE'"
        "\n  <the memory text>\n  MEMWRITE")
HEREDOC_RULE = ("Blocked: the heredoc that feeds memwrite.py needs its delimiter in single quotes (<<'MEMWRITE'). "
                "Unquoted, the shell runs what a memory line holds in backticks and $( )." + FORM)
SUBSTITUTION_RULE = ("Blocked: this memwrite.py command holds a backtick or $( where the shell would run it. Memory text goes on stdin "
                     "in a heredoc with a quoted delimiter, and the why in single quotes. Read the HASH first and paste it." + FORM)
DELIMITER_RULE = ("Blocked: a line of the text is the heredoc's own delimiter, so the text would end there and the rest would run as shell. "
                  "Use a delimiter that is not a line of the text." + FORM)
CODE_WRITE = re.compile(r"write_text|write_bytes|open\([^)]*['\"][wax+]|os\.(remove|unlink|rename|replace)|shutil\.|fs\.(write|append|unlink|rm)|File\.write")
HEREDOC = re.compile(r"([^\n]*)<<-?\s*['\"]?(\w+)['\"]?[^\n]*\n(.*?)(?:\n\2\b[^\n]*|\Z)", re.S)
REDIRECTS = {">", ">>", ">|", "&>", "&>>", ">&"}
BREAKS = {";", "&&", "||", "|", "&", "(", ")", ";;", "|&"}
EXPLAIN = ("Blocked: protected memory changes only through `python3 scripts/memwrite.py` (project/) or by the human "
           "(ORG.md, STEERING.md, and .claude/commands/dream.md with its Steering block). Put proposals in memory/inbox/. "
           "Git history of the store is read-only for agents.")


STORE_KEY = ("agent-memory.store", "agent-memory")  # the key, and its section for --remove-section
KEY_WRITES = {"--unset", "--unset-all", "--add", "--replace-all", "--remove-section", "--rename-section"}
KEY_VERBS = {"set", "unset", "remove-section", "rename-section"}  # git config's newer spelling
KEY_READS = {"get", "list"}
KEY_READ_FLAGS = {"--get", "--get-all", "--get-regexp", "--get-urlmatch", "-l", "--list"}
KEY_EDITS = {"-e", "--edit", "edit"}  # opens the whole file in an editor, which names no key: stopped for a repository's own file
ELSEWHERE = {"--global", "--system"}  # not a repository's own configuration, where the key is never read from
TAKE_A_VALUE = {"-f", "--file", "--blob", "--default", "--type", "-t", "--comment", "--value", "--url"}  # options of git config whose next word is theirs
STORE_CODE = re.compile(r"\bstore\b.*\b(attach|detach)\s*\(", re.S)  # store.py's two functions, called from code
YOURS_RULE = "Blocked: this is the person's to run, not an agent's: it is about {}."
STORE_RULE = ("Blocked: which store a repository belongs to is the person's to decide. `amk attach`, `amk detach` and the "
              "agent-memory.store key in a repository's git configuration are theirs to run. `amk where` says which store this folder has.")


class Blocked(Exception):
    pass


def where(token: str, inside):
    """The path within the store this token names, or None if it isn't in the store."""
    token = token.rstrip("/") or token
    match = STORE.search(token)
    if match:
        return os.path.normpath(match[1]) if match[1] else "."
    if inside is not None and not token.startswith(("/", "~", "$")):
        rel = os.path.normpath(os.path.join(inside, token))
        return None if rel.startswith("..") else rel
    return None


def record(rel: str) -> bool:
    """Whether this path in the store is, or as a wildcard takes in, the record of an apply."""
    return bool(re.fullmatch(r"dreams/[^/]*-applied\.json", rel)) or fnmatch.fnmatchcase(APPLIED, rel)


def protected(token: str, inside) -> bool:
    if DREAM.search(token):
        return True
    rel = where(token, inside)
    if rel is None:
        return False
    if any(c in rel for c in "*?") and not rel.startswith(("agents/", "inbox/", "dreams/")):
        return True
    return rel == "." or rel in TOP or rel.split("/")[0] in ("project", ".git") or record(rel)


def segments(command: str):
    # A backtick starts or ends a command the shell runs, as $( ) does: what stands inside is read as a command of its own.
    lexer = shlex.shlex(command.replace("\n", " ; ").replace("`", " ; "), posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    current = []
    for token in lexer:
        if token in BREAKS:
            yield current
            current = []
        else:
            current.append(token)
    yield current


def git_verb(words: list):
    """(the folder git is pointed at, or None; where its verb is among the words)."""
    index, target = 1, None
    while index < len(words) and words[index].startswith("-"):
        if words[index] in ("-C", "--git-dir", "--work-tree", "-c") and index + 1 < len(words):
            if words[index] != "-c":
                target = words[index + 1]
            index += 2
        else:
            index += 1
    return target, index


def check_store_key(words: list):
    """A `git config` that sets, changes or removes the key that says which store a repository belongs to. Reading it is fine."""
    _, index = git_verb(words)
    if words[index:index + 1] != ["config"]:
        return
    rest = words[index + 1:]
    if KEY_EDITS & set(rest) and not ELSEWHERE & set(rest):
        raise Blocked(STORE_RULE)
    if not any(word.casefold() in STORE_KEY for word in rest):
        return
    plain = [word for at, word in enumerate(rest) if not word.startswith("-") and (at == 0 or rest[at - 1] not in TAKE_A_VALUE)]
    reads = KEY_READ_FLAGS & set(rest) or plain[:1] in ([verb] for verb in KEY_READS)
    if KEY_WRITES & set(rest) or plain[0] in KEY_VERBS or (len(plain) > 1 and not reads):
        raise Blocked(STORE_RULE)


def check_persons_command(name: str, args: list):
    """The commands that are the person's (verbs.PERSONS): attach and detach, the choice of amk's Python, closing or
    changing what a proposal left, giving up a dream, the person's pass of an apply.

    By the script run directly, and through amk by any path to it, with an interpreter in front of it or not.
    """
    if verbs is None:
        return  # every such command was stopped whole (INCOMPLETE)
    if name in RUNNERS:  # the interpreter is in front: what it runs is the first word that is not an option
        name, args = next(((os.path.basename(arg), args[at + 1:]) for at, arg in enumerate(args) if not arg.startswith("-")), ("", []))
    found = (name, args) if name.endswith(".py") and name != "amk.py" else verbs.behind(args[0], args[1:]) if name in ("amk", "amk.py") and args else None
    about = found and verbs.persons(*found)
    if about:
        raise Blocked(STORE_RULE if found[0] == "store.py" else YOURS_RULE.format(about))


def check_git_output(words: list, inside):
    """`--output=<file>` makes git's log, show and diff write a file, so a read could write memory: it is checked as a redirect is."""
    target, index = git_verb(words)
    rest = words[index + 1:]
    rest = rest[:rest.index("--")] if "--" in rest else rest
    for at, word in enumerate(rest):
        value = word[len("--output="):] if word.startswith("--output=") else rest[at + 1] if word == "--output" and at + 1 < len(rest) else None
        if value is None:
            continue
        if target and where(target, inside) is not None:
            raise Blocked  # git pointed at the store: whatever it writes there is the store's
        if protected(os.path.join(target, value) if target and not value.startswith(("/", "~", "$")) else value, inside):
            raise Blocked


def check_git(words: list, inside):
    target, index = git_verb(words)
    in_store = where(target, inside) is not None if target else inside is not None
    if not in_store or index >= len(words):
        return
    verb, rest = words[index], [w for w in words[index + 1:] if not w.startswith("-")]
    if verb in GIT_READS or (verb == "rm" and rest and all(w.startswith("inbox/") for w in rest)):
        return
    raise Blocked


def check_segment(words: list, inside):
    """Returns the working directory inside the store after this part runs."""
    plain, index = [], 0
    while index < len(words):
        if words[index] in REDIRECTS:
            if index + 1 < len(words) and protected(words[index + 1], inside):
                raise Blocked
            index += 2
        else:
            plain.append(words[index])
            index += 1
    while plain and (re.match(r"^\w+=", plain[0]) or plain[0] in PREFIXES or plain[0] in KEYWORDS):
        if plain.pop(0) == "env":  # env's own options stand between it and the command it runs
            while plain and plain[0].startswith("-"):
                del plain[:2 if plain[0] in ENV_VALUED else 1]
    if not plain:
        return inside
    name, args = os.path.basename(plain[0]), plain[1:]
    if name in ("cd", "pushd"):
        return where(args[0], inside) if args else None
    if name in SHELLS and "-c" in args and args.index("-c") + 1 < len(args):
        inspect(args[args.index("-c") + 1], inside)
    if name == "eval":
        inspect(" ".join(args), inside)
    if name == "xargs":
        check_segment(args, inside)
    if name in RUNNERS and name not in SHELLS:
        for flag in ("-c", "-e"):
            if flag in args and args.index(flag) + 1 < len(args):
                code = args[args.index(flag) + 1]
                if CODE_PATH.search(code) and CODE_WRITE.search(code):
                    raise Blocked
                if STORE_CODE.search(code):
                    raise Blocked(STORE_RULE)
    if name == "git":
        check_store_key(plain)
        check_git_output(plain, inside)
        check_git(plain, inside)
    check_persons_command(name, args)
    files = [a for a in args if not a.startswith("-")]
    if name in EVERY_ARG and any(protected(a, inside) for a in files):
        raise Blocked
    if name in LAST_ARG and files and protected(files[-1], inside):
        raise Blocked
    if name == "dd" and any(a.startswith("of=") and protected(a[3:], inside) for a in args):
        raise Blocked
    if name in IN_PLACE and any(a.startswith("-") and "i" in a[1:] and not a.startswith("--") for a in args) and any(protected(a, inside) for a in files):
        raise Blocked
    if name == "find" and any(a in ("-delete", "-exec", "-execdir") for a in args) and files and where(files[0], inside) is not None:
        raise Blocked
    return inside


def substitutes(text: str) -> bool:
    """Whether the shell would run something in this text: a backtick or $( outside single quotes."""
    quote, at = "", 0
    while at < len(text):
        c = text[at]
        if quote == "'":
            quote = "" if c == "'" else quote
        elif c == "\\":
            at += 1
        elif c == "'" and not quote:
            quote = "'"
        elif c == '"':
            quote = "" if quote else '"'
        elif c == "`" or text[at:at + 2] == "$(":
            return True
        at += 1
    return False


def memory_text(command: str):
    """What the shell does with the text of a command that runs memwrite.py, before memwrite ever sees it."""
    lines = command.split("\n")
    code, open_docs = [], []
    for number, line in enumerate(lines):
        if open_docs:
            dash, quoted, word = open_docs[0]
            if (line.lstrip("\t") if dash else line) == word:
                open_docs.pop(0)
            continue  # the body of a quoted heredoc reaches memwrite as it is written
        code.append(line)
        for dash, quoted, word in OPENS.findall(line):
            if sum(1 for later in lines[number + 1:] if (later.lstrip("\t") if dash else later) == word) > 1:
                raise Blocked(DELIMITER_RULE)
            if not quoted:
                raise Blocked(HEREDOC_RULE)
            open_docs.append((dash, quoted, word))
    if substitutes("\n".join(code)):
        raise Blocked(SUBSTITUTION_RULE)


def inspect(command: str, inside=None):
    if verbs is None:
        if MEMORY_COMMAND.search(command):
            raise Blocked(INCOMPLETE)
        return inspect_paths(command, inside)
    if RUNS.search(command):
        memory_text(command)
    inspect_paths(command, inside)


def inspect_paths(command: str, inside=None):
    """What each part of the command writes to, and whether it is one of the person's commands."""
    for head, _, body in HEREDOC.findall(command):
        runner = next((os.path.basename(w) for w in re.findall(r"[\w./-]+", head) if os.path.basename(w) in RUNNERS), "")
        if runner in SHELLS:
            inspect(body, inside)
        elif runner and not FEEDS.search(head) and CODE_PATH.search(body) and CODE_WRITE.search(body):
            raise Blocked
        elif runner and STORE_CODE.search(body):
            raise Blocked(STORE_RULE)
    without_bodies = HEREDOC.sub(lambda m: m[1], command)
    for words in segments(without_bodies):
        inside = check_segment(words, inside)


def main():
    command = str((json.load(sys.stdin).get("tool_input") or {}).get("command", ""))
    try:
        inspect(command)
    except Blocked as stop:
        print(str(stop) or EXPLAIN, file=sys.stderr)
        sys.exit(2)
    except ValueError:  # shell syntax this reader can't follow: fall back to looking for the pattern
        if CODE_PATH.search(command) and re.search(r"(>>?|\btee\b|\bsed\s+-i|\brm\b|\bmv\b|\bcp\b|\btruncate\b)", command):
            print(EXPLAIN, file=sys.stderr)
            sys.exit(2)


if __name__ == "__main__":
    main()

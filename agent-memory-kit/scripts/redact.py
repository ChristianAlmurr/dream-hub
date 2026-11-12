#!/usr/bin/env python3
"""What must never be kept: credentials, and whatever a store's own reject.txt lists. Not run by itself.

One place for both, so a memory write (memwrite.py), a proposal (proposal_check.py), Claude's own
notes (own_notes.py), the session archive (archive.py) and the dream batch (collect_transcripts.py)
refuse the same things.

- `SECRET` is the credential pattern. It knows the keys that have a shape of their own (the list
  below), a private key block, a bearer or basic authorization header, a URL that carries a
  password, a password given on a command line, and a value given to a name that says it is one
  (`API_KEY=…`, `"apiKey": "…"`, `client_secret: …`, `DATABASE_PW=…`).
  A value that only points at a credential is not one: `os.environ["API_KEY"]`, `settings.API_KEY`,
  `$API_KEY`, `<your-key>`, `YOUR_API_KEY_HERE`, `********`, a path to a file, a word in a sentence.
- `credential()` also asks the pattern the kit had before (`OLDER`). A memory write is refused when
  either matches: what was refused then is refused now.
- `rules()` reads the store's reject.txt: one regular expression per line, human-edited.
- `scrub()` replaces both in a text with a marker that says which it was, and leaves a text it has
  already scrubbed exactly as it is. It keeps count for each of the store's rules (`Tally`) and
  stops the run when a rule is plainly a mistake: one that matches in most entries, or one that
  takes seconds on a single text.
"""
import collections, contextlib, hashlib, re, signal

REMOVED = "[credential removed]"
REFUSED = "[removed: reject.txt]"
MARKERS = re.compile("(" + "|".join(re.escape(marker) for marker in (REMOVED, REFUSED)) + ")")
PASSES = 20  # a text is clean after two or three; more than this means a rule is taking the markers apart
BUDGET = 2.0  # seconds one rule of the store may take on one text
SHARE = 0.5  # a rule that matches in more than this share of entries is stopped
ENOUGH = 200  # entries seen before that share means anything

# Every repeat below has an end or can't fail once it has started, so a megabyte of letters with no space in it
# (a picture, a minified file) is read in the time its length takes.
# Where a value stops: at a space, a quote, a closing bracket, a backslash (an escaped quote or newline in JSON) or the end.
END = r"""(?![^\s"'`,;)}\]>\\])"""
STOP = r"""(?=[\s"'”’\\]|$)"""
# What stands in for a credential, or says where one is kept.
SAME = r"(?!(.)\{n}{{7}})"  # ******** and xxxxxxxx; {n} is the number of the group, which differs from use to use
SHOUTED = rf"(?![A-Z][A-Z_]*{STOP})"  # YOUR_API_KEY_HERE
FILE = rf"(?![\w.~-]*(?:/[\w.~-]+)+\.[A-Za-z0-9]{{1,5}}{STOP})"  # config/secrets/production.yml
KEPT_IN = (r"(?!(?:os|process|env|settings|config|cfg|conf|secrets|self|this|ctx|context|req|request|app|vars|github|steps|inputs|options|opts|args"
           r"|props|params|data|response|res|result|json|body|payload|creds|credentials|auth|session|user|account|client|import\.meta|Deno|Bun)[.\[])")
CALLED = r"(?![A-Za-z_][\w.]{0,80}\()"  # get_token(user)
PROSE = r"(?![a-z]+[^\S\n]+[A-Za-z])"  # "the token: authentication is done elsewhere"
# The word that says what follows is one, and what may follow it in the same name: SECRET_KEY, token-v2.
# Not a name for where one is kept or how long it is: TOKEN_URL, PASSWORD_FILE, SECRET_NAME.
WORD = r"(?:api[_-]?key|secret|passw(?:or)?d|passphrase|token|credential)"
BESIDE = (r"(?:file|path|dir|url|uri|name|id|type|len|length|ttl|count|limit|header|field|endpoint|expiry|expires|expiration|timeout"
          r"|prefix|suffix|env|var)(?![A-Za-z0-9])")
NAME = rf"{WORD}(?:[_-](?!{BESIDE})[A-Za-z0-9]{{1,40}}){{0,6}}"
SHAPES = (
    r"\bsk-[A-Za-z0-9_-]{20,}",  # OpenAI, Anthropic
    r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}|\bwhsec_[A-Za-z0-9]{24,}",  # Stripe
    r"\bgh[pousr]_[A-Za-z0-9]{30,}|\bgithub_pat_\w{30,}",
    r"\bA[KS]IA[0-9A-Z]{16}",  # AWS
    r"\bxox[baprs]-[\w-]{10,}|\bhooks\.slack\.com/services/[\w/]{20,}",  # Slack tokens and webhooks
    r"\bAIza[0-9A-Za-z_-]{35}|\bya29\.[0-9A-Za-z_-]{20,}",  # Google
    r"\bnpm_[A-Za-z0-9]{36}|\bglpat-[\w-]{20,}|\bSG\.[\w-]{20,}\.[\w-]{20,}|\bsbp_[0-9a-f]{40}|\bsb_secret_[\w-]{20,}"
    r"|\btskey-[a-z]+-[\w-]{20,}|\bvercel_blob_rw_\w{20,}",  # npm, GitLab, SendGrid, Supabase, Tailscale, Vercel
    # A private key up to its END line. One whose end was cut off goes as far as its lines of key go, numbered or indented or not.
    r"-----BEGIN [A-Z ]*PRIVATE KEY(?: BLOCK)?-----(?:[\s\S]{0,10000}?-----END [A-Z ]*PRIVATE KEY(?: BLOCK)?-----"
    r"|[^\n]*(?:\n[ \t]*(?:\d+\t)?[ \t]*[A-Za-z0-9+/=]+(?![^\n]))*)",
    r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}",  # a JSON web token
    r"(?<!\d)\d{6,12}:[A-Za-z0-9_-]{35}(?![A-Za-z0-9_-])",  # a Telegram bot token, also inside its API URL
    r"(?i:\bbearer\s+|\bauthorization\s*[:=]\s*basic\s+)[A-Za-z0-9._~+/=-]{20,}",
    r"\b[a-z][a-z0-9+.-]{1,20}://[^\s/:@]{0,200}:[^\s/@]{1,200}@",  # a URL carrying a password
    r"[?&](?:key|sig|signature|auth|access_token|token|api_?key|secret)=[A-Za-z0-9_%.-]{16,}",  # and one carrying a key
    r"(?i:\b(?:set-)?cookie\s*:\s*)[^\n=]{0,40}=[A-Za-z0-9%._-]{20,}",
    # On a command line.
    r"\bmysql\b[^\n]{0,80}\s-p(?!ort)\S{6,}|\bsshpass\s+-p\s*\S+|\bcurl\b[^\n]{0,200}\s(?:-u|--user)\s+['\"]?[^\s:'\"$]+:(?!\$)[^\s'\"]{4,}",
    r"--password[ =](?!\$|<|\[|[A-Z_]+\b)\S{6,}|--token[ =](?!\$|<|\[)[A-Za-z0-9_-]{16,}",
    r"(?i:\bpassw(?:or)?d\s+(?:is\s+)?)(?=\S*\d)(?=\S*[A-Za-z])[^\s\"'`.,;:]{8,}",  # "the password is hunter2hunter2"
    # A password is often short: after a name that says password, with nothing around the equals sign, six characters are enough.
    rf"""(?i:passw(?:or)?d|passphrase)(?:[_-](?!{BESIDE})[A-Za-z0-9]{{1,40}}){{0,6}}=(?!["']|\$|<|\{{|\[){SAME.format(n=1)}{SHOUTED}{KEPT_IN}{CALLED}[^\s"'`\\]{{6,11}}{STOP}""",
    # The value on the line after its name, as YAML and some logs have it: a mixed run of 16 or more.
    rf"""(?i:{NAME})["']?[^\S\n]*[:=][^\S\n]*\n[ \t]*(?=[A-Za-z0-9+/_=-]*[0-9])(?=[A-Za-z0-9+/_=-]*[A-Za-z])[A-Za-z0-9+/_=-]{{16,}}{END}""",
    # NAME=value with nothing around the equals sign, as in a shell or an .env file: whatever the value holds.
    rf"""(?:(?i:{NAME})|\b[A-Z][A-Z0-9_]{{0,40}}_(?:PW|PASS|AUTH))=(?!["']|\$|<|\{{|\[){SAME.format(n=2)}{SHOUTED}{FILE}{KEPT_IN}{CALLED}[^\s"'`\\]{{12,}}""",
    # A quoted value: JSON, also with its quotes escaped inside a tool call, Python, YAML.
    rf"""(?i:{NAME})(?:\\*["'])?\s*[:=]\s*\\*["'“‘](?!\$|<|\{{){SAME.format(n=3)}{SHOUTED}{FILE}[^\s"'”’\\]{{12,}}(?=\\*["'”’])""",
    # name: value and name = value with no quotes. Here a word in a sentence, a call and a pointer are not values.
    rf"""(?i:{NAME})["']?(?:[^\S\n]*:[^\S\n]*|[^\S\n]+=[^\S\n]*|=[^\S\n]+){SAME.format(n=4)}{SHOUTED}{FILE}{KEPT_IN}{PROSE}[^\s"'`,;(){{}}\[\]<>$\\]{{12,}}{END}""",
)
SECRET = re.compile("|".join(SHAPES))
# The pattern the kit had before this one. It is broader on a name and narrower on everything else.
OLDER = re.compile(
    r"(sk-[A-Za-z0-9_-]{20,}|ghp_[A-Za-z0-9]{30,}|github_pat_\w{30,}|AKIA[0-9A-Z]{16}|xox[baprs]-[\w-]{10,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----|(?i:(api[_-]?key|secret|password|token)\s*[:=]\s*\S{12,}))")
# Lines of a private key left behind a marker: what an older version did, which took only the BEGIN line.
ORPHAN = re.compile(re.escape(REMOVED) + r"(?:(?:\\n|\s)*[ \t]*(?:\d+\t)?[ \t]*[A-Za-z0-9+/=]{40,}(?![A-Za-z0-9+/=]))+"
                    r"(?:(?:\\n|\s)*-----END [A-Z ]*PRIVATE KEY(?: BLOCK)?-----)?")


class Tally:
    """What the store's rules did in one run: texts seen, and for each rule how often it replaced something and in how many texts.

    A rule is known by its number, the first pattern in reject.txt being rule 1: what is printed lands in
    the transcript of whoever ran the script, and the patterns say what the store holds private.
    """

    def __init__(self):
        self.texts, self.replaced, self.touched = 0, collections.Counter(), collections.Counter()

    def check(self, source, enough=ENOUGH):
        """Stops the run when one rule matched in most of what it was shown. `source` says where the rules are, for the message."""
        for rule, n in self.touched.items():
            if self.texts >= enough and n > SHARE * self.texts:
                raise SystemExit(f"{source}: rule {rule} matches in {n:,} of {self.texts:,} texts. A rule that removes that much is nearly "
                                 "always a mistake, and in a session whose transcript is gone it could not be undone. Nothing more was changed: "
                                 "correct the rule and run again.")

    def lines(self) -> list:
        return [f"reject.txt: rule {rule} replaced {n:,}× in {self.touched[rule]:,} texts" for rule, n in sorted(self.replaced.items())]


def credential(text: str) -> bool:
    """Does this text hold what looks like a credential, to this pattern or to the one before it?"""
    return bool(SECRET.search(text) or OLDER.search(text))


def rules(mem) -> list:
    """The patterns in a store's reject.txt, compiled. A line that is not a regular expression stands for its own words."""
    path, found = mem / "reject.txt", []
    for line in (path.read_text().splitlines() if path.exists() else []):
        line = line.strip()
        if line and not line.startswith("#"):
            try:
                found.append(re.compile(line))
            except re.error:
                found.append(re.compile(re.escape(line)))
    return found


@contextlib.contextmanager
def limited(number: int):
    """Gives one of the store's rules BUDGET seconds on one text. A rule written by hand can take minutes on a single line.

    The alarm interrupts the match, which nothing else in Python can. It needs the main thread, where every script of the kit runs.
    """
    def late(*_):
        raise SystemExit(f"reject.txt: rule {number} took more than {BUDGET:g} seconds on one text, so it would hold up every run. "
                         "Nothing more was changed: write the rule so it can't try the same text in many ways (no repeat inside a repeat), and run again.")
    before = signal.signal(signal.SIGALRM, late)
    signal.setitimer(signal.ITIMER_REAL, BUDGET)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, before)


def refused(text: str, patterns: list) -> str:
    """The first of a store's patterns that this text matches, or empty."""
    for number, pattern in enumerate(patterns, 1):
        with limited(number):
            if pattern.search(text):
                return pattern.pattern
    return ""


def outside(text: str, change) -> str:
    """The text with `change` applied to everything that is not a marker: a marker is never matched or taken apart."""
    pieces = MARKERS.split(text)
    pieces[::2] = [change(piece) for piece in pieces[::2]]  # the odd ones are the markers
    return "".join(pieces)


def once(text: str, patterns: list, counts: dict) -> str:
    """One pass: every match outside the markers replaced, the credentials first and then each of the store's patterns."""
    text = ORPHAN.sub(REMOVED, outside(text, lambda piece: SECRET.sub(REMOVED, piece)))
    for number, pattern in enumerate(patterns, 1):
        def replace(piece: str) -> str:
            # A pattern that can match nothing at all (`x*`) must not put a marker between every two characters.
            after = pattern.sub(lambda found: REFUSED if found[0] else found[0], piece)
            if after != piece:
                counts[number] += after.count(REFUSED)  # a piece between markers holds none of its own
            return after
        with limited(number):
            text = outside(text, replace)
    return text


def scrub(text: str, patterns: list, tally=None) -> str:
    """The text with every credential and everything the store refuses replaced by a marker.

    Repeated until nothing changes, so scrubbing a text twice gives what scrubbing it once gave: the
    archive is scrubbed again whenever the rules change, and what was already clean must stay as it is.
    Each pass replaces at least one character that is not in a marker, so it ends after a few.
    """
    counts = collections.Counter()
    for _ in range(PASSES):
        after = once(text, patterns, counts)
        if after == text:
            break
        text = after
    else:
        raise RuntimeError(f"A text was still changing after {PASSES} passes of the rules: one of them is matching inside the markers.")
    if tally is not None:
        tally.texts += 1
        tally.replaced.update(counts)
        tally.touched.update(dict.fromkeys(counts, 1))
    return text


def scrubber(mem, patterns: list, tally: Tally):
    """The function that scrubs one text under a store's rules, keeps count, and stops the run when a rule is plainly a mistake."""
    def clean(text: str) -> str:
        after = scrub(text, patterns, tally)
        tally.check(mem / "reject.txt")
        return after
    return clean


def fingerprint(patterns: list) -> str:
    """Changes whenever what would be removed changes: the credential pattern or the store's rules."""
    told = "\n".join([SECRET.pattern, ORPHAN.pattern] + [pattern.pattern for pattern in patterns])
    return hashlib.sha256(told.encode("utf-8", "replace")).hexdigest()[:16]

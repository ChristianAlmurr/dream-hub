"""What every part of Dream Hub stands on: which hub this is, the environment its commands run in, and how a refusal
and a file that can't be used are said."""
import json, os, shlex, subprocess
from pathlib import Path

HOME = Path.home()
APP = Path(__file__).resolve().parent
STORES = HOME / "agent-memory"
LAUNCHD_JOBS = Path(os.path.realpath(HOME / "Library" / "LaunchAgents"))  # where launchd keeps a person's jobs, the kit's dream jobs among them
INSTALLED_CONFIG = HOME / ".config" / "agent-hub" / "config.json"
DEFAULT_PORT = 8491


def settings_in(config) -> dict:
    """The optional keys of a hub's config.json that say where things are on this Mac (see the README, Configuration):
    "port", "projects", "kit", "path", "no_vcs", "private_folders". A file that can't be read gives none of them, and
    each missing key keeps its default."""
    try:
        found = json.loads(Path(config).read_text())
    except (OSError, ValueError):
        return {}
    return found if isinstance(found, dict) else {}


def installed_port() -> int:
    port = settings_in(INSTALLED_CONFIG).get("port")
    return port if isinstance(port, int) and not isinstance(port, bool) and 1 <= port <= 65535 else DEFAULT_PORT


def default_kit() -> Path:
    """The agent memory kit: the config's "kit", else the agent-memory-kit folder beside this hub, else ~/tools/agent-memory-kit."""
    named = settings_in(INSTALLED_CONFIG).get("kit")
    if isinstance(named, str) and named.strip():
        return Path(os.path.expanduser(named))
    beside = APP.parent / "agent-memory-kit"
    return beside if (beside / "bin" / "amk").exists() else HOME / "tools" / "agent-memory-kit"


INSTALLED = {"port": installed_port(), "state": Path(os.path.realpath(HOME / ".local" / "state" / "agent-hub")), "socket": "agent-hub"}  # what only the installed hub may have

INSTANCE = ("HUB_PORT", "HUB_STATE", "HUB_TMUX", "HUB_CONFIG", "HUB_KIT", "HUB_JOBS")


class BadInstance(Exception):
    """The HUB_* variables don't describe one whole instance. Its text is one sentence naming the variable."""


def read_instance(env) -> dict:
    """Which hub this is. With none of the six variables it is the installed one. With any of them it is a second
    instance and needs all six, so it can never run half on the installed hub's state, socket, config or dream jobs."""
    if not any(name in env for name in INSTANCE):
        return {"port": installed_port(), "state": HOME / ".local" / "state" / "agent-hub", "socket": "agent-hub",
                "config": INSTALLED_CONFIG, "kit": default_kit(), "jobs": LAUNCHD_JOBS}
    for name in INSTANCE:
        if name not in env:
            raise BadInstance(f"{name} is not set: a second instance needs all six of {', '.join(INSTANCE)}.")
        if not env[name].strip():
            raise BadInstance(f"{name} is set but empty.")
    port = env["HUB_PORT"]
    if not (port.isascii() and port.isdigit() and 1 <= int(port) <= 65535):
        raise BadInstance(f"HUB_PORT must be a port from 1 to 65535, and {port!r} isn't.")
    for name in ("HUB_STATE", "HUB_CONFIG", "HUB_KIT", "HUB_JOBS"):
        if not os.path.isabs(env[name]):
            raise BadInstance(f"{name} must be an absolute path, and {env[name]!r} isn't.")
    # Paths are taken as the folders they really are, so a roundabout spelling of launchd's folder is launchd's folder.
    return {"port": int(port), "state": Path(os.path.realpath(env["HUB_STATE"])), "socket": env["HUB_TMUX"], "config": Path(os.path.realpath(env["HUB_CONFIG"])),
            "kit": Path(os.path.realpath(env["HUB_KIT"])), "jobs": Path(os.path.realpath(env["HUB_JOBS"]))}


try:
    _instance = read_instance(os.environ)
except BadInstance as problem:
    raise SystemExit(str(problem))
PORT, STATE, SOCKET_NAME, CONFIG, KIT, JOBS = (_instance[key] for key in ("port", "state", "socket", "config", "kit", "jobs"))
SETTINGS = settings_in(CONFIG)


def listed(key: str) -> list:
    """A config key that holds a list of text, or [] for anything else."""
    value = SETTINGS.get(key)
    return [v for v in value if isinstance(v, str) and v.strip()] if isinstance(value, list) else []


def search_path() -> str:
    """What commands the hub starts find programs on: ~/.local/bin, the config's "path", the PATH the server was started
    with (install.sh puts the installing shell's PATH in the launchd job), then the usual folders. Each folder once."""
    folders = [f"{HOME}/.local/bin", *map(os.path.expanduser, listed("path")), *os.environ.get("PATH", "").split(":"),
               "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    return ":".join(dict.fromkeys(f for f in folders if f))


PROJECTS = Path(os.path.expanduser(SETTINGS["projects"])) if isinstance(SETTINGS.get("projects"), str) and SETTINGS["projects"].strip() else HOME / "projects"
ENV = {**os.environ, "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8", "TERM": "xterm-256color", "PATH": search_path()}


def whole(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


class Refused(Exception):
    """The request is understood and not allowed. Its text is shown to the person. `more` goes to the page with it."""
    status = "409 Conflict"

    def __init__(self, text: str, **more):
        super().__init__(text)
        self.more = more


class Malformed(Refused):
    """The request isn't one the page would ever send: a field is missing or of the wrong kind. Refused before anything is looked at."""
    status = "400 Bad Request"


def text_in(body: dict, key: str, what: str) -> str:
    """A field that must be text with something in it. An empty one would fit everything a search is run over."""
    value = body.get(key) if isinstance(body, dict) else None
    if not isinstance(value, str) or not value.strip():
        raise Malformed(f"Say {what}.")
    return value


def number_in(body: dict, key: str, what: str, low: int, high: int) -> int:
    value = body.get(key) if isinstance(body, dict) else None
    if not whole(value) or not low <= value <= high:
        raise Malformed(f"{what} must be a whole number from {low} to {high}.")
    return value


# Repos the hub never runs git or jj in, and so never commits to: the config's "no_vcs", each a path from the home
# folder or a whole path (someone else's repo where the kit's files stay untracked, a shared monorepo). Writing a file
# there, such as Steering, is still the hub's to do.
NO_VCS = tuple(HOME / os.path.expanduser(rel) for rel in listed("no_vcs"))
VCS = ("git", "jj")


def off_limits(folder) -> bool:
    """Whether a folder is, or is inside, a repo the hub never runs git or jj in."""
    try:
        where = Path(folder).expanduser().resolve()
    except (OSError, RuntimeError):
        return False
    return any(where == repo.resolve() or repo.resolve() in where.parents for repo in NO_VCS)


def vcs_folder(cmd: list, cwd=None):
    """The folder a git or jj command would act in: its -C (or jj's -R / --repository), else where it runs."""
    for flag in ("-C", "-R", "--repository"):
        if flag in cmd[1:-1]:
            return cmd[cmd.index(flag, 1) + 1]
    return cwd or os.getcwd()


def refused_vcs(cmd: list, cwd=None, text=True):
    """A finished command that never ran, for git or jj in a folder off limits; None for anything else."""
    if Path(str(cmd[0])).name not in VCS or not off_limits(vcs_folder([str(c) for c in cmd], cwd)):
        return None
    why = f"{vcs_folder([str(c) for c in cmd], cwd)} is a repo the hub never runs {Path(str(cmd[0])).name} in."
    return subprocess.CompletedProcess(list(map(str, cmd)), 128, "" if text else b"", why if text else why.encode())


def run(*cmd, cwd=None, timeout=30):
    return refused_vcs(cmd, cwd) or subprocess.run(list(map(str, cmd)), cwd=cwd, env=ENV, capture_output=True, text=True, timeout=timeout)


_damage = {}


def stamp_of(path: Path):
    try:
        seen = os.stat(path)
    except OSError:
        return None
    return (seen.st_mtime_ns, seen.st_size, seen.st_mode)


def damaged(path: Path, why):
    """A file the hub reads and can't use. It is logged once for each change of the file, and whatever it would have
    shown for its project is left out, so one bad file never takes the other projects down with it."""
    stamp = stamp_of(path)
    if _damage.get(str(path), "new") != stamp:
        _damage[str(path)] = stamp
        print(f"error: {path} can't be used: {why}", flush=True)
    return None




def pasteable(path: Path) -> str:
    """A path for a command a person will paste: the home folder as ~, kept outside the quotes so the shell expands it."""
    if HOME in path.parents:
        return "~/" + shlex.quote(str(path.relative_to(HOME)))
    return shlex.quote(str(path))

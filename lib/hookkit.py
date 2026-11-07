"""Shared plumbing for the lean-agent hooks.

Every hook reads one JSON event on stdin and must never break the tool call it
watches because of its own bug. So `run` turns any internal error into exit 0,
and every git call has a short timeout. Standard library only, Python 3.9+.

  python3 lib/hookkit.py --self-test
"""
import ast
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import threading

SEPARATORS = {";", "&&", "||", "|", "&", "\n", "(", ")", ";;", "|&"}
PIPES = {"|", "|&"}
ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*", re.S)
HEREDOC = re.compile(r"<<(-?)[ \t]*((?:'[^'\n]*'|\"(?:[^\"\\\n]|\\.)*\"|\\.|[^\s;&|()<>'\"\\])+)")
HEREDOC_MARK = re.compile(r"__lean_heredoc_(\d+)__")
GIT_GLOBAL_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}


class _State(threading.local):
    """Per-thread hook state: the last event read (for the session id and cwd of a recorded fire) and, when
    the dispatcher binds one, the hook's own name, manifest and mode. Unbound, the name is sys.argv[0]."""

    def __init__(self):  # runs once per thread, so no thread shares an event dict
        self.event = {}
        self.name = None
        self.manifest = None
        self.mode = None
        self.guard_text = []  # guard notes waiting to go out with the hook's own output
        self.emitted = False  # a JSON note already went to stdout in this call


_state = _State()
_RECORD = "--self-test" not in sys.argv[1:]  # a hook's own self-test must not write to the real database
if not _RECORD and not os.environ.get("LEAN_BRAIN_DB"):
    # a self-test, and every hook it spawns, must write to a scratch brain and never the real one
    import atexit
    import shutil
    import tempfile
    _scratch = tempfile.mkdtemp(prefix="lean-selftest-brain-")
    atexit.register(shutil.rmtree, _scratch, True)
    os.environ["LEAN_BRAIN_DB"] = os.path.join(_scratch, "brain.db")


def bind(name, manifest, mode=None):
    """Make NAME this thread's current hook, in place of sys.argv[0]. The dispatcher calls it per member."""
    _state.name = name
    _state.manifest = manifest
    _state.mode = mode
    _state.event = {}
    _state.guard_text = []
    _state.emitted = False


def _hook_name():
    return _state.name or os.path.basename(sys.argv[0])


def _fire(outcome):
    """Record this hook's fire for lean-stats. Any failure is dropped: statistics never touch the tool call."""
    if not _RECORD:
        return
    try:
        import leanstats
        leanstats.record_fire(_hook_name(), outcome,
                              str(_state.event.get("session_id") or ""), str(_state.event.get("cwd") or ""))
    except Exception:
        pass


TOOLS = os.path.normpath(os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "tools"))


def session_id(value):
    """The event's session id when it is a string safe for a file name and a shell line, else None. A number
    or anything else is refused, so session-env and the hooks agree on which sessions get a marker."""
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9-]{1,100}", value):
        return value
    return None


def marker_file(sid):
    """Where session-env records that it put TOOLS on this session's PATH. Hooks never see CLAUDE_ENV_FILE
    outside a few events, so they read this instead."""
    sid = session_id(sid)
    if not sid:
        return None
    return os.path.join(os.path.expanduser("~"), ".local", "share", "lean", "tools-on-path", sid)


def tools_on_path():
    """True when the tools folder is on PATH for this session: PATH names it, or the marker session-env
    writes once the PATH line has landed in the env file names it (so an upgrade does not match an old one).
    CLAUDE_ENV_FILE alone never counts: SessionStart hooks run in parallel, so session-env may not have
    written it yet, or the write may have failed."""
    if TOOLS in [os.path.normpath(p) for p in os.environ.get("PATH", "").split(os.pathsep) if p]:
        return True
    marker = marker_file(_state.event.get("session_id"))
    try:
        with open(marker, encoding="utf-8") as f:
            return f.read().strip() == TOOLS
    except (OSError, TypeError):
        return False


def tool_path(name, base=None):
    """How to run a plugin tool: the bare name when the tools folder is on PATH, else its absolute path, quoted."""
    if base is None and tools_on_path():
        return name
    return shlex.quote(os.path.join(base or TOOLS, name))


PROFILES = ("strict", "standard", "off")
EVENT_ORDER = ("SessionStart", "PostToolUse", "PreToolUse", "PostToolUseFailure", "SessionEnd", "UserPromptSubmit")
MANIFEST_KEYS = ("events", "timeout", "mode", "pack", "guard", "default")
MANIFEST_EVENT_KEYS = ("event", "matcher", "if", "group", "order", "timeout")


def read_manifest(path):
    """Return the HOOK literal of a hook file without importing it. Raises ValueError("PATH: reason")."""
    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=path)
    found = [node for node in tree.body if isinstance(node, ast.Assign)
             and any(isinstance(t, ast.Name) and t.id == "HOOK" for t in node.targets)]
    if not found:
        raise ValueError(f"{path}: no HOOK literal")
    if len(found) > 1:
        raise ValueError(f"{path}: two HOOK literals")
    try:
        manifest = ast.literal_eval(found[0].value)
    except ValueError:
        raise ValueError(f"{path}: HOOK is not a literal") from None
    if not isinstance(manifest, dict):
        raise ValueError(f"{path}: HOOK is not a literal dict")
    for key in manifest:
        if key not in MANIFEST_KEYS:
            raise ValueError(f"{path}: unknown key {key!r}")
    events = manifest.get("events")
    if not isinstance(events, list):
        raise ValueError(f"{path}: events must be a list")
    for entry in events:
        if not isinstance(entry, dict):
            raise ValueError(f"{path}: an event entry is not a dict")
        for key in entry:
            if key not in MANIFEST_EVENT_KEYS:
                raise ValueError(f"{path}: unknown event key {key!r}")
        if entry.get("event") not in EVENT_ORDER:
            raise ValueError(f"{path}: unknown event {entry.get('event')!r}")
        for key in ("group", "order"):
            if not isinstance(entry.get(key), int):
                raise ValueError(f"{path}: event {entry['event']} is missing {key!r}")
    default = manifest.get("default", {})
    if not isinstance(default, dict):
        raise ValueError(f"{path}: default must be a dict")
    for level, value in default.items():
        if value not in ("off", "note", "block"):
            raise ValueError(f"{path}: default {value!r} for {level!r} is not off, note or block")
    return manifest


def profile():
    """LEAN_PROFILE: standard when unset, and anything unrecognised counts as strict."""
    if "LEAN_PROFILE" not in os.environ:
        return "standard"
    raw = os.environ["LEAN_PROFILE"].strip().lower()
    return raw if raw in PROFILES else "strict"


def switch_name(hook):
    """The switch name of a hook file: "commit-author.py" gives "COMMIT_AUTHOR"."""
    base = os.path.basename(hook)
    if base.endswith(".py"):
        base = base[:-3]
    return re.sub(r"[^A-Za-z0-9]", "_", base).upper()


def _manifest():
    """This hook's manifest: the bound one, else the HOOK literal of the script that is running."""
    if _state.manifest is not None:
        return _state.manifest
    if _state.name is None:
        found = getattr(sys.modules.get("__main__"), "HOOK", None)
        return found if isinstance(found, dict) else None
    return None


_WARNED = set()


def _candidates(env, name, quiet=False):
    """Every (mode, key) one layer asks for, in the order they win: LEAN_HOOK_<NAME> first, then
    LEAN_SKIP_<NAME>=1, then the old name of the switch at its old value (the last two both mean off).
    Any other value of LEAN_HOOK_<NAME> is ignored with one stderr line."""
    key = "LEAN_HOOK_" + name
    value = env.get(key)
    if value is not None:
        if value in ("off", "note", "block"):
            yield value, key
        elif not quiet and (key, value) not in _WARNED:
            _WARNED.add((key, value))
            print(f"lean-agent: {key}={value} is not one of off, note, block; ignored.", file=sys.stderr)
    if env.get("LEAN_SKIP_" + name) == "1":
        yield "off", "LEAN_SKIP_" + name
    if key in ALIASES:
        old, old_value = ALIASES[key]
        if env.get(old) == old_value:
            yield "off", old


def _layer(env, name, quiet=False):
    """The mode one layer (an env prefix dict or the process env) asks for, or None."""
    for found, _key in _candidates(env, name, quiet):
        return found
    return None


RANK = {"off": 0, "note": 1, "block": 2}
# Claude Code's managed policy files. A test replaces this list so it never reads the real ones.
MANAGED_SETTINGS = ["/Library/Application Support/ClaudeCode/managed-settings.json",
                    "/etc/claude-code/managed-settings.json"]
REPO_WHERE = "this repo's .claude/settings.json"
PREFIX_WHERE = "the command prefix"


def _project_dirs():
    """The start directory (CLAUDE_PROJECT_DIR, else the event cwd, else the current directory) and every
    parent up to and including the first one that holds .git (file or directory). With no .git, just the start."""
    start = os.environ.get("CLAUDE_PROJECT_DIR") or str(_state.event.get("cwd") or "") or os.getcwd()
    start = os.path.abspath(start)
    found, here = [], start
    while True:
        found.append(here)
        if os.path.exists(os.path.join(here, ".git")):
            return found
        parent = os.path.dirname(here)
        if parent == here:
            return [start]
        here = parent


def repo_root():
    """The first directory at or above the project directory that holds .git, else the project directory."""
    return _project_dirs()[-1] if os.path.exists(os.path.join(_project_dirs()[-1], ".git")) else _project_dirs()[0]


def _env_block(path):
    """The env block of one settings file the user owns, {} when the file is missing, unreadable or odd."""
    try:
        with open(path, encoding="utf-8-sig") as handle:
            data = json.load(handle)
        block = data.get("env") if isinstance(data, dict) else None
        return block if isinstance(block, dict) else {}
    except (OSError, ValueError):
        return {}


EVERY_NAME = object()  # a repository settings file we cannot read counts as setting every name


def _repo_env(path):
    """The env block of a repository's settings.json: {} when there is no such file, EVERY_NAME when the
    file is there but does not parse (after a leading BOM is dropped) or cannot be read."""
    if not os.path.lexists(path):
        return {}
    try:
        with open(path, encoding="utf-8-sig") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return EVERY_NAME
    block = data.get("env") if isinstance(data, dict) else None
    return block if isinstance(block, dict) else {}


def _git_list(directory, *args):
    """The NUL-separated entries of `git ls-files -z ARGS` run in DIRECTORY, or None when git cannot say
    (missing, a timeout, an error). core.fsmonitor is off and no GIT_* location variable is passed on, so a
    cloned repository's config cannot run a program or point git elsewhere."""
    env = {k: v for k, v in os.environ.items() if k not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")}
    try:
        done = subprocess.run(["git", "-c", "core.fsmonitor=false", "-C", directory, "ls-files", "-z", *args],
                              capture_output=True, timeout=3, env=env)
    except Exception:
        return None
    if done.returncode != 0:
        return None
    return [p for p in done.stdout.decode("utf-8", "replace").split("\0") if p]


def _lists_local(entries):
    return any(os.path.basename(p.split("\t")[-1]).lower() == "settings.local.json" for p in entries)


def _local_is_user(directory):
    """True only when DIRECTORY/.claude/settings.local.json is the user's own: a real file under a real
    .claude folder (no symlink), spelled exactly so, and not tracked by git under any case spelling, by the
    outer repo or by a repo that holds .claude itself (a submodule). Any doubt, including a git error, says False."""
    claude = os.path.join(directory, ".claude")
    local = os.path.join(claude, "settings.local.json")
    try:
        if os.path.islink(claude) or os.path.islink(local):
            return False
        real_claude, real_local = os.path.realpath(claude), os.path.realpath(local)
        if os.path.commonpath([real_claude, real_local]) != real_claude:
            return False
        if "settings.local.json" not in os.listdir(claude):
            return False
    except (OSError, ValueError):
        return False
    if os.path.lexists(os.path.join(claude, ".git")):
        return False  # .claude is a submodule or a work tree of its own
    outer = _git_list(directory, "-s", "--", ".claude")
    if outer is None or any(e.startswith("160000 ") for e in outer) or _lists_local(outer):
        return False
    inner = _git_list(claude)  # whichever repo actually holds the folder answers here
    return inner is not None and not _lists_local(inner)


def _repo_sourced(name, value):
    dirs = _project_dirs()
    found = False
    for d in dirs:
        block = _repo_env(os.path.join(d, ".claude", "settings.json"))
        if block is EVERY_NAME:
            found = True
        elif name in block and (not isinstance(block[name], str) or block[name] == value):
            found = True
    if not found:
        return False
    for path in MANAGED_SETTINGS:
        if name in _env_block(path):
            return False
    for d in dirs:
        if name in _env_block(os.path.join(d, ".claude", "settings.local.json")) and _local_is_user(d):
            return False
    return True


def from_repo(name):
    """True when a repository's .claude/settings.json (the project directory's, or any parent up to the git
    root) sets NAME to the value now in the environment, and the user did not also set it: not in a managed
    policy file, and not in a settings.local.json that git does not track (a committed one is the repo's).
    An error while checking counts as repo-sourced, so a loosening is never honoured on a doubt."""
    value = os.environ.get(name)
    if value is None:
        return False
    try:
        return _repo_sourced(name, value)
    except Exception:
        return True


def guard_note(name, where):
    """Record that a guard ignored a loosening of NAME from WHERE. The note itself is said once per session
    and name, when it goes out: with the hook's own note, on stderr with a block, or by itself when the hook
    ends (flush_guard). It waits because the session id is known only after the hook has read its event."""
    if (name, where) not in _state.guard_text:
        _state.guard_text.append((name, where))


def _take_guard():
    """The waiting guard notes as one text, leaving out any already said in this session. Empty when none."""
    waiting, _state.guard_text = _state.guard_text, []
    hook = os.path.basename(_hook_name())
    if hook.endswith(".py"):
        hook = hook[:-3]
    sid = session_id(_state.event.get("session_id"))
    lines = []
    for name, where in waiting:
        if sid:  # without a session there is nothing to mark, so the note is said every time
            folder = os.path.join(tempfile.gettempdir(), "lean-agent", sid)
            try:
                os.makedirs(folder, exist_ok=True)
                flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
                os.close(os.open(os.path.join(folder, "guard-" + re.sub(r"\W", "_", name)), flags, 0o600))
            except FileExistsError:
                continue
            except OSError:
                pass  # no marker: say it every time rather than never
        lines.append(f"{name} from {where} is ignored: {hook} is a guard. "
                     "Set it in your user settings (~/.claude/settings.json) to change it.")
    return " ".join(lines)


def flush_guard(code=0):
    """Send the waiting guard notes: to stderr when the call is blocked (code 2) or a note already went out
    (one JSON document at most), else as one note. Hooks that go through run() never call this."""
    text = _take_guard()
    if not text:
        return
    if code == 2 or _state.emitted:
        print(text, file=sys.stderr)
        sys.stderr.flush()
    else:
        _emit(str(_state.event.get("hook_event_name") or "PreToolUse"), text)
    _keep_notice(text)


def _is_guard(manifest):
    return bool(isinstance(manifest, dict) and manifest.get("guard"))


def _default(manifest, standard, level=None):
    level = level or profile()
    if standard is not None and level != "strict":
        # a per-call standard= replaces the manifest default; strict has always blocked
        return {"nothing": "off", "off": "off", "note": "note"}.get(standard, "block")
    default = (manifest or {}).get("default") or {}
    if level in default:
        return default[level]
    return "block" if level == "strict" else "note"


def resolve_for(hook, manifest, seg=None, standard=None, bound=None, quiet=False):
    """(mode, source) for the hook file HOOK. source is "profile-off", "prefix", "env" or "default".

    1. LEAN_PROFILE=off. 2. The command's env prefix. 3. The process env, or BOUND, the mode the dispatcher
    resolved from it. 4. (the extension layer comes later.) 5. STANDARD when given, else the manifest default.

    For a guard, a prefix value looser than the default is dropped, and so is an env value the repository's
    own settings put there. Resolution goes on to the next layer, and a note says so once unless QUIET.
    """
    guard = _is_guard(manifest)
    level = profile()
    if guard and level != "strict" and from_repo("LEAN_PROFILE"):
        # a repository cannot loosen a guard with its own LEAN_PROFILE: resolve as if it were strict
        if not quiet:
            guard_note("LEAN_PROFILE", REPO_WHERE)
        level = "strict"
    if level == "off":
        return "off", "profile-off"
    name = switch_name(hook)
    default = _default(manifest, standard, level)

    def looser(found):
        return RANK[found] < RANK[default]

    def drop(key, where):
        if not quiet:
            guard_note(key, where)

    if seg is not None:
        for found, key in _candidates(seg.env, name, quiet):
            if guard and looser(found):
                drop(key, PREFIX_WHERE)
                continue
            return found, "prefix"
    if bound in ("off", "note", "block"):
        if guard and looser(bound) and from_repo("LEAN_HOOK_MODE"):
            drop("LEAN_HOOK_MODE", REPO_WHERE)
        else:
            return bound, "env"
    for found, key in _candidates(os.environ, name, quiet):
        if guard and looser(found) and from_repo(key):
            drop(key, REPO_WHERE)
            continue
        return found, "env"
    return default, "default"


def resolve_mode(seg=None, standard=None):
    """(mode, source) for the running hook. Inside a dispatcher-bound hook, a LEAN_HOOK_MODE tagged with this hook's name stands in for layer 3."""
    bound = _state.mode
    if bound is None and _state.name is None:
        # the dispatcher writes "<NAME>:<mode>"; a bare value or another hook's name is ignored
        who, _, raw = os.environ.get("LEAN_HOOK_MODE", "").partition(":")
        bound = raw if who == switch_name(_hook_name()) and raw in ("off", "note", "block") else None
    return resolve_for(_hook_name(), _manifest(), seg, standard, bound)


def mode(seg=None, standard=None):
    return resolve_mode(seg, standard)[0]


def off(seg=None):
    """True when the user turned this hook off, in the prefix or the environment (a default of off is not that)."""
    found, source = resolve_mode(seg)
    return found == "off" and source != "default"


def refuse(event_name, text, *, standard=None, note=None):
    """Refuse a tool call the way the hook's mode says: block prints TEXT and returns 2, note gives Claude NOTE
    (or TEXT) and returns 0, off returns 0. STANDARD ("note", "nothing", "block") replaces the manifest
    default outside strict, as before; strict blocks unless the user switched the hook.
    """
    found = mode(standard=standard)
    if found == "block":
        print(text, file=sys.stderr)
        return 2
    if found == "note":
        globals()["note"](event_name, note or text)
    return 0


def probe(script, event, **env):
    """Self-test helper: run a hook script on EVENT with env overrides (None removes). Returns (code, stdout, stderr)."""
    full = dict(os.environ)
    for key in [k for k in full if k.startswith(("LEAN_HOOK_", "LEAN_SKIP_"))]:
        full.pop(key)  # the caller's shell must not leak a switch in
    for key, value in env.items():
        if value is None:
            full.pop(key, None)
        else:
            full[key] = value
    p = subprocess.run([sys.executable, script], input=json.dumps(event), capture_output=True, text=True, env=full)
    return p.returncode, p.stdout, p.stderr


def run(main, manifest=None):
    """Run a hook's main and exit with its code; an internal error exits 0.

    Exits 0 at once when the user switched the hook off. A mode the dispatcher bound is kept.
    """
    manifest = manifest or getattr(main, "__globals__", {}).get("HOOK")
    if isinstance(manifest, dict):
        _state.manifest = manifest
    found, source = resolve_mode()
    if found == "off" and source != "default":
        flush_guard(0)
        sys.exit(0)
    try:
        code = main()
    except SystemExit as exc:
        if exc.code == 2:
            _fire("block")
        flush_guard(exc.code if isinstance(exc.code, int) else 0)
        raise
    except Exception as exc:  # a broken hook must not block the tool call
        print(f"lean-agent hook error ignored: {exc!r}", file=sys.stderr)
        code = 0
    if code == 2:
        _fire("block")
    flush_guard(code or 0)
    sys.exit(code or 0)


def read_event():
    try:
        event = json.load(sys.stdin)
    except ValueError:
        event = {}
    _state.event = event if isinstance(event, dict) else {}
    return _state.event


def tool_input(event):
    value = event.get("tool_input")
    return value if isinstance(value, dict) else {}


def _emit(event_name, text):
    _state.emitted = True
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": event_name, "additionalContext": text}}))
    sys.stdout.flush()


def note(event_name, text):
    """Print a non-blocking note that Claude reads with the tool result. Waiting guard notes join it."""
    if _state.guard_text and not _state.emitted:
        waiting = _take_guard()
        text = text + ("\n" + waiting if waiting else "")
    _emit(event_name, text)
    _fire("note")
    _keep_notice(text)


def _keep_notice(text):
    """Keep the note as a notice in brain for the web page. Never raises and never waits more than about a second."""
    if not _RECORD or os.environ.get("LEAN_NOTICES") == "off":
        return
    try:
        from brain import records, store
        con = store.connect(timeout=1)
        records.add_notice(con, _hook_name(), text,
                           str(_state.event.get("session_id") or ""), str(_state.event.get("cwd") or ""))
        con.close()
    except (SystemExit, Exception):  # a locked or newer database must not change the hook's output
        pass


# Each new switch name and the old name that still works, with the value the old name took.
ALIASES = {
    "LEAN_HOOK_COMMIT_AUTHOR": ("LEAN_ALLOW_COMMIT_AUTHOR", "1"),
    "LEAN_HOOK_SCRATCHPAD_ROOT": ("LEAN_ALLOW_SCRATCHPAD_ROOT", "1"),
    "LEAN_SKIP_ATTRIBUTION": ("LEAN_ALLOW_ATTRIBUTION", "1"),
    "LEAN_SKIP_SLOP": ("LEAN_ALLOW_SLOP", "1"),
    "LEAN_SKIP_WIDE_CHARS": ("LEAN_ALLOW_WIDE_CHARS", "1"),
}


def disabled(name, seg=None, values=("1",)):
    """True when NAME, or its old alias, has an accepted value in the environment or in this command's env prefix.

    Inside a guard hook, the prefix never counts (a note says so), and an env value the repository's own
    settings put there does not count either.
    """
    pairs = [(name, tuple(values))]
    if name in ALIASES:
        old, old_value = ALIASES[name]
        pairs.append((old, (old_value,)))
    guard = _is_guard(_manifest())
    for key, accepted in pairs:
        if seg is not None and seg.env.get(key) in accepted:
            if not guard:
                return True
            guard_note(key, PREFIX_WHERE)
        if os.environ.get(key) in accepted:
            if guard and from_repo(key):
                guard_note(key, REPO_WHERE)
                continue
            return True
    return False


def resolve(path, cwd):
    path = os.path.expanduser(path)
    if not os.path.isabs(path):
        path = os.path.join(cwd or os.getcwd(), path)
    return os.path.normpath(path)


def git(cwd, *args, timeout=3):
    """stdout of a git command, stripped, or None when it fails."""
    try:
        result = subprocess.run(["git", "-C", cwd or ".", *args], capture_output=True,
                                text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def test_marker(cwd):
    """Path of the file recording this worktree's last compiled test run, or None outside git."""
    git_dir = git(cwd, "rev-parse", "--absolute-git-dir")
    return os.path.join(git_dir, "lean-last-test-run") if git_dir else None


def _end_word(word):
    """The heredoc end word as bash reads it: quotes and backslashes removed, so <<E"O"F ends at EOF.

    None when the word will not parse; the caller then leaves the text in place for the strict parse to refuse.
    Also None for $ or a backtick: bash drops a backslash before them inside double quotes and shlex does not.
    """
    if "$" in word or "`" in word:
        return None
    try:
        return "".join(shlex.split(word))
    except ValueError:
        return None


def prepare(command):
    """Drop shell comments and cut heredoc bodies out of a command.

    Returns the remaining text, with each heredoc replaced by a marker word, and
    the list of bodies. Without this a `# note` swallows the next line and a
    heredoc line such as `cd /` reads as a command.
    """
    out, bodies, pending = [], [], []
    i, n, quote = 0, len(command), None
    while i < n:
        ch = command[i]
        if quote:
            out.append(ch)
            if quote == '"' and ch == "\\" and i + 1 < n:
                out.append(command[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            out.append(command[i:i + 2])
            i += 2
            continue
        if ch in "'\"":
            quote = ch
            out.append(ch)
            i += 1
            continue
        if ch == "#" and (i == 0 or command[i - 1] in " \t\n;&|()"):
            end = command.find("\n", i)
            i = n if end < 0 else end
            continue
        if command.startswith("<<", i) and not command.startswith("<<<", i):
            match = HEREDOC.match(command, i)
            delim = _end_word(match.group(2)) if match else None
            if delim is not None:
                out.append(f" __lean_heredoc_{len(bodies) + len(pending)}__ ")
                pending.append((delim, bool(match.group(1))))
                i = match.end()
                continue
        if ch == "\n" and pending:
            out.append("\n")
            i += 1
            for delim, strip_tabs in pending:
                lines = []
                while i < n:
                    end = command.find("\n", i)
                    end = n if end < 0 else end
                    line, i = command[i:end], end + 1
                    if (line.lstrip("\t") if strip_tabs else line) == delim:
                        break
                    lines.append(line)
                bodies.append("".join(line + "\n" for line in lines))
            pending = []
            continue
        out.append(ch)
        i += 1
    bodies.extend("" for _ in pending)
    return "".join(out), bodies


def tokens(text, strict=False):
    """Split shell text into words and separators; strict raises on unbalanced quotes."""
    lexer = shlex.shlex(text, posix=True, punctuation_chars=";&|()<>\n")
    lexer.commenters = ""
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    try:
        return list(lexer)
    except ValueError:
        if strict:
            raise
        return text.split()


def safe_dir(target, cwd, base):
    """Resolve a cd or -C target; fall back to base for a shell expression or a missing dir."""
    if "$" in target or "`" in target:
        return base
    path = resolve(target, cwd)
    return path if os.path.isdir(path) else base


def skip_options(words, i, with_value=()):
    while i < len(words) and words[i].startswith("-") and words[i] != "--":
        i += 2 if words[i] in with_value else 1
    return i


def strip_wrappers(words, env):
    """Drop nice, timeout, flock, env, time, command, exec and nohup in front of a command."""
    while words:
        name = os.path.basename(words[0])
        if name == "env":
            i = skip_options(words, 1, ("-u", "--unset", "-C", "--chdir"))
            while i < len(words) and ASSIGNMENT.fullmatch(words[i]):
                key, value = words[i].split("=", 1)
                env[key] = value
                i += 1
        elif name == "nice":
            i = skip_options(words, 1, ("-n", "--adjustment"))
        elif name == "timeout":
            i = skip_options(words, 1, ("-s", "--signal", "-k", "--kill-after")) + 1
        elif name == "flock":
            i = skip_options(words, 1, ("-w", "--timeout", "-E", "--conflict-exit-code")) + 1
            i = skip_options(words, i, ("-w", "--timeout", "-E", "--conflict-exit-code"))
            if i < len(words) and words[i - 1] in ("-c", "--command"):
                return []
        elif name in ("time", "command", "exec", "nohup"):
            i = skip_options(words, 1)
        else:
            return words
        words = words[i:]
    return words


class Segment:
    """One simple command out of a shell line.

    env is its VAR=value prefix (and env wrapper assignments), words the command
    with wrappers removed, cwd where it runs after any earlier `cd`, base the cwd
    the hook was given, heredocs the bodies fed to it, piped whether its output
    goes into a pipe (so its exit code is not the line's).
    """

    def __init__(self, env, words, cwd, base, heredocs=(), piped=False):
        self.env, self.words, self.cwd, self.base = env, words, cwd, base
        self.heredocs, self.piped = list(heredocs), piped

    def __repr__(self):
        return f"Segment({self.env!r}, {self.words!r}, {self.cwd!r})"


def is_separator(tok):
    return tok in SEPARATORS or (tok and set(tok) <= set(";&|()\n"))


def segments(command, cwd="."):
    """Split a shell command into simple commands, following `cd DIR`."""
    text, bodies = prepare(command)
    base = cwd
    out, current = [], []
    for tok in tokens(text) + [";"]:
        if not is_separator(tok):
            current.append(tok)
            continue
        env, heredocs, words = {}, [], []
        for word in current:
            mark = HEREDOC_MARK.fullmatch(word)
            if mark:
                heredocs.append(bodies[int(mark.group(1))] if int(mark.group(1)) < len(bodies) else "")
            else:
                words.append(word)
        while words and ASSIGNMENT.fullmatch(words[0]):
            key, value = words.pop(0).split("=", 1)
            env[key] = value
        words = strip_wrappers(words, env)
        if words and words[0] == "cd":
            cwd = safe_dir(words[1] if len(words) > 1 else "~", cwd, base)
        elif words:
            out.append(Segment(env, words, cwd, base, heredocs, tok in PIPES))
        current = []
    return out


def git_call(seg):
    """For a git segment return (subcommand, args, cwd, config) or None.

    config holds `-c key=value` pairs given before the subcommand.
    """
    words = seg.words
    if not words or os.path.basename(words[0]) != "git":
        return None
    cwd, config, i = seg.cwd, {}, 1
    while i < len(words) and words[i].startswith("-"):
        word = words[i]
        if word in GIT_GLOBAL_WITH_VALUE and i + 1 < len(words):
            value = words[i + 1]
            if word == "-C":
                cwd = safe_dir(value, cwd, seg.base)
            elif word == "-c" and "=" in value:
                key, val = value.split("=", 1)
                config[key.lower()] = val
            i += 2
        else:
            i += 1
    if i >= len(words):
        return None
    return words[i], words[i + 1:], cwd, config


def gh_call(seg):
    """For a gh segment return (words after gh, cwd) or None."""
    words = seg.words
    if not words or os.path.basename(words[0]) != "gh":
        return None
    return words[1:], seg.cwd


# The one table of code host commands the hooks care about. Each host's CLI maps to the words that name a
# command, and what the command does: "create" opens a pull request, "merge" merges one, "ready" takes one out of
# draft, "update" edits one (an Azure update that completes the pull request counts as a merge and one that sets
# --draft false, or a glab update with --ready, counts as ready, see forge_action), "text" writes public
# text without opening or merging anything (comments, reviews, issues, releases).
# To add a host: add its CLI to FORGE_CLIS, its commands to FORGE_COMMANDS, its flags to FORGE_FLAGS.
FORGE_CLIS = {"gh": "github", "glab": "gitlab", "az": "azure"}
FORGE_COMMANDS = {
    "gh": {
        ("pr", "create"): "create", ("pr", "merge"): "merge", ("pr", "ready"): "ready", ("pr", "edit"): "update",
        ("pr", "comment"): "text", ("pr", "review"): "text",
        ("issue", "create"): "text", ("issue", "edit"): "text", ("issue", "comment"): "text",
        ("release", "create"): "text", ("release", "edit"): "text",
    },
    "glab": {
        ("mr", "create"): "create", ("mr", "new"): "create", ("mr", "merge"): "merge", ("mr", "update"): "update",
        ("mr", "comment"): "text", ("issue", "comment"): "text", ("mr", "note"): "text", ("mr", "note", "create"): "text",
        ("issue", "create"): "text", ("issue", "update"): "text", ("issue", "note"): "text",
        ("issue", "note", "create"): "text", ("release", "create"): "text", ("release", "update"): "text",
    },
    "az": {
        ("repos", "pr", "create"): "create", ("repos", "pr", "update"): "update",
        ("boards", "work-item", "create"): "text", ("boards", "work-item", "update"): "text",
    },
}
# Flags that carry public text (value follows), and flags that name a file holding it. "multi" hosts take
# several words after one flag (az: --description "line one" "line two").
FORGE_FLAGS = {
    "gh": {"text": {"--title", "-t", "--body", "-b", "--notes", "-n", "--subject"},
           "file": {"--body-file", "-F", "--notes-file"}, "multi": False},
    "glab": {"text": {"--title", "-t", "--description", "-d", "--message", "-m", "--notes", "-N",
                      "--squash-message"},
             "file": {"--notes-file", "-F"}, "multi": False},
    "az": {"text": {"--title", "--description", "-d", "--discussion"}, "file": set(), "multi": True},
}
FORGE_API = {"gh", "glab"}  # CLIs with an `api` command that takes -f/-F/--field/--raw-field/--input


class ForgeCall:
    """One code host command: `cli` (gh, glab or az), `kind` (github, gitlab, azure), `args` (the words after the
    CLI), `command` (the table key that matched, or None), `action` (create, merge, update, text or None), `rest`
    (the words after the matched command) and `cwd`."""

    def __init__(self, cli, args, cwd):
        self.cli, self.kind, self.args, self.cwd = cli, FORGE_CLIS[cli], args, cwd
        self.command = next((key for key in sorted(FORGE_COMMANDS[cli], key=len, reverse=True)
                             if tuple(args[:len(key)]) == key), None)
        self.action = FORGE_COMMANDS[cli].get(self.command) if self.command else None
        self.rest = args[len(self.command):] if self.command else args

    @property
    def is_api(self):
        return self.cli in FORGE_API and self.args[:1] == ["api"]


def forge_call(seg):
    """A ForgeCall for a gh, glab or az segment, or None."""
    words = seg.words
    if not words or os.path.basename(words[0]) not in FORGE_CLIS:
        return None
    return ForgeCall(os.path.basename(words[0]), words[1:], seg.cwd)


def forge_action(call):
    """create, merge, ready, update, text or None. An Azure `repos pr update` that completes the pull request
    (--status completed) is a merge, one that sets --draft false is ready; a glab `mr update --ready` is ready."""
    if call is None or call.action is None:
        return None
    if call.action == "update" and call.kind in ("azure", "gitlab"):
        found = set()
        for i, word in enumerate(call.rest):
            key, _, value = word.partition("=")
            value = value or (call.rest[i + 1] if i + 1 < len(call.rest) else "")
            value = value.lower()
            if call.kind == "azure" and key == "--status" and value == "completed":
                found.add("merge")
            elif call.kind == "azure" and key == "--draft" and value == "false":
                found.add("ready")
            elif call.kind == "gitlab" and word == "--ready":
                found.add("ready")
        for action in ("merge", "ready"):
            if action in found:
                return action
    return call.action


AZ_SHORT_FLAGS = {"-d", "-t", "-s", "-b", "-p", "-r", "-i", "-o", "-h", "-w"}


def is_az_flag(word):
    """True for a flag word on an az command: --name (also --name=value), or a short flag az knows."""
    return (word.startswith("--") and len(word) > 2) or word in AZ_SHORT_FLAGS


def forge_flag_values(call, flags, args=None):
    """(flag, value) for `--flag value`, `--flag=value` and `-f value` in the call's words, up to `--`.
    For a host whose flags take several words (az), the value is all the words up to the next flag."""
    words = call.rest if args is None else args
    multi = FORGE_FLAGS[call.cli]["multi"]
    for i, word in enumerate(words):
        if word == "--":
            return
        if word in flags:
            if i + 1 >= len(words):
                continue
            if multi:
                # The first word is the value whatever it starts with ("- a fix", "-1 x"); later words belong to it
                # until a real flag (--word, or a short az flag) or a separator.
                j = i + 1
                if words[j] in SEPARATORS:
                    continue
                j += 1
                while j < len(words) and words[j] not in SEPARATORS and not is_az_flag(words[j]):
                    j += 1
                yield word, "\n".join(words[i + 1:j])
            else:
                yield word, words[i + 1]
        elif word.startswith("--") and "=" in word and word.split("=", 1)[0] in flags:
            yield tuple(word.split("=", 1))


def _switch_tests(check, scratch):
    """The LEAN_HOOK_<NAME> resolution order, on a fixture hook with the default block in strict, note in standard."""
    here = os.path.dirname(os.path.realpath(__file__))
    fixture = os.path.join(scratch, "fixture-gate.py")
    marker = os.path.join(scratch, "ran")
    with open(fixture, "w", encoding="utf-8") as handle:
        handle.write(
            "import sys\nsys.path.insert(0, %r)\nimport hookkit\n"
            "HOOK = {'events': [], 'default': {'strict': 'block', 'standard': 'note'}}\n"
            "def main():\n    open(%r, 'a').close()\n    hookkit.read_event()\n"
            "    return hookkit.refuse('PreToolUse', 'REFUSED')\n"
            "if __name__ == '__main__':\n    hookkit.run(main, HOOK)\n" % (here, marker))
    event = {"tool_name": "Bash", "tool_input": {"command": "x"}}

    def fire(**env):
        if os.path.exists(marker):
            os.remove(marker)
        code, out, err = probe(fixture, event, **env)
        return code, bool(out.strip()), err, os.path.exists(marker)

    check("switch_name", switch_name("review-log-check.py"), "REVIEW_LOG_CHECK")
    check("switch_name with a path", switch_name("/x/hooks/commit-author.py"), "COMMIT_AUTHOR")
    check("fixture strict blocks", fire(LEAN_PROFILE="strict")[:2], (2, False))
    check("fixture standard notes", fire(LEAN_PROFILE="standard")[:2], (0, True))
    got = fire(LEAN_PROFILE="strict", LEAN_HOOK_FIXTURE_GATE="note")
    check("env note in strict gives a note", got[:2], (0, True))
    got = fire(LEAN_PROFILE="strict", LEAN_HOOK_FIXTURE_GATE="off")
    check("env off gives nothing and main does not run", (got[0], got[1], got[3]), (0, False, False))
    got = fire(LEAN_PROFILE="strict", LEAN_SKIP_FIXTURE_GATE="1")
    check("LEAN_SKIP_<NAME>=1 is off", (got[0], got[1], got[3]), (0, False, False))
    got = fire(LEAN_PROFILE="standard", LEAN_HOOK_FIXTURE_GATE="block")
    check("env block in standard", got[:2], (2, False))
    got = fire(LEAN_PROFILE="strict", LEAN_HOOK_FIXTURE_GATE="loud")
    check("a bad value is ignored and named", (got[0], "LEAN_HOOK_FIXTURE_GATE=loud" in got[2]), (2, True))
    got = fire(LEAN_PROFILE="off", LEAN_HOOK_FIXTURE_GATE="block")
    check("profile off beats the switch", (got[0], got[1], got[3]), (0, False, False))
    got = fire(LEAN_PROFILE="strict", LEAN_HOOK_MODE="FIXTURE_GATE:off")
    check("LEAN_HOOK_MODE with the hook's own name stands in for the env layer", (got[0], got[3]), (0, False))
    got = fire(LEAN_PROFILE="strict", LEAN_HOOK_MODE="off")
    check("a bare LEAN_HOOK_MODE is ignored", (got[0], got[3]), (2, True))
    got = fire(LEAN_PROFILE="strict", LEAN_HOOK_MODE="SECRET_SCAN:off")
    check("another hook's LEAN_HOOK_MODE is ignored", (got[0], got[2].strip(), got[3]), (2, "REFUSED", True))
    got = fire(LEAN_PROFILE="strict", LEAN_HOOK_MODE="FIXTURE_GATE:loud")
    check("a bad tagged mode is ignored", (got[0], got[3]), (2, True))
    got = fire(LEAN_PROFILE="strict", LEAN_HOOK_MODE="FIXTURE_GATE:off", LEAN_HOOK_FIXTURE_GATE="block")
    check("a hook switch in the env does not beat a bound mode", (got[0], got[3]), (0, False))
    got = fire(LEAN_PROFILE="strict", LEAN_HOOK_FIXTURE_GATE="off", LEAN_HOOK_MODE=None)
    check("probe drops a caller's LEAN_HOOK_MODE", got[3], False)

    class _Seg:
        def __init__(self, **env):
            self.env = env

    saved = {k: os.environ.pop(k, None) for k in ("LEAN_HOOK_FIXTURE_GATE", "LEAN_SKIP_FIXTURE_GATE", "LEAN_HOOK_MODE")}
    saved_name, saved_manifest, saved_mode = _state.name, _state.manifest, _state.mode
    try:
        bind("fixture-gate.py", {"default": {"strict": "block", "standard": "note"}})
        os.environ["LEAN_HOOK_FIXTURE_GATE"] = "off"
        check("prefix block beats env off", resolve_mode(_Seg(LEAN_HOOK_FIXTURE_GATE="block")), ("block", "prefix"))
        check("prefix skip is off", resolve_mode(_Seg(LEAN_SKIP_FIXTURE_GATE="1")), ("off", "prefix"))
        check("env off", resolve_mode(), ("off", "env"))
        os.environ.pop("LEAN_HOOK_FIXTURE_GATE")
        check("default in strict", resolve_mode(), ("block", "default"))
        check("per-call standard beats the default outside strict", with_standard("standard", "nothing"), ("off", "default"))
        check("strict ignores a per-call standard", with_standard("strict", "nothing"), ("block", "default"))
        bind("fixture-gate.py", {"default": {"strict": "block", "standard": "note"}}, "note")
        os.environ["LEAN_HOOK_FIXTURE_GATE"] = "block"
        check("a bound mode stands in for the env layer", resolve_mode(), ("note", "env"))
        check("prefix still beats a bound mode", resolve_mode(_Seg(LEAN_HOOK_FIXTURE_GATE="off")), ("off", "prefix"))
        bind("commit-author.py", {"default": {"strict": "block", "standard": "note"}})
        os.environ.pop("LEAN_HOOK_FIXTURE_GATE")
        os.environ["LEAN_ALLOW_COMMIT_AUTHOR"] = "1"
        check("the old name counts as off in the env", resolve_mode(), ("off", "env"))
        os.environ.pop("LEAN_ALLOW_COMMIT_AUTHOR")
        check("the old name counts as off in the prefix",
              resolve_mode(_Seg(LEAN_ALLOW_COMMIT_AUTHOR="1")), ("off", "prefix"))
        check("the old name with another value does not", resolve_mode(_Seg(LEAN_ALLOW_COMMIT_AUTHOR="0")),
              ("block", "default"))
    finally:
        _state.name, _state.manifest, _state.mode = saved_name, saved_manifest, saved_mode
        os.environ.pop("LEAN_HOOK_FIXTURE_GATE", None)
        os.environ.pop("LEAN_ALLOW_COMMIT_AUTHOR", None)
        for key, val in saved.items():
            if val is not None:
                os.environ[key] = val
    return None


def _guard_tests(check, scratch):
    """Guards: a loosening from a repo's settings or a command prefix is dropped, with one note per session."""
    import contextlib
    import io
    import shutil

    class _Seg:
        def __init__(self, **env):
            self.env = env

    global MANAGED_SETTINGS, _RECORD
    keys = ("CLAUDE_PROJECT_DIR", "LEAN_HOOK_GATE_G", "LEAN_HOOK_GATE_N", "LEAN_SKIP_GATE_G", "LEAN_HOOK_MODE",
            "LEAN_ALLOW_COMMIT_AUTHOR", "LEAN_PROFILE", "LEAN_SKIP_PUBLIC_TEXT")
    saved = {k: os.environ.pop(k, None) for k in keys}
    saved_state = (_state.name, _state.manifest, _state.mode, _state.event)
    saved_managed, saved_record = MANAGED_SETTINGS, _RECORD
    managed = os.path.join(scratch, "no-managed.json")
    MANAGED_SETTINGS, _RECORD = [managed], False
    os.environ["LEAN_PROFILE"] = "strict"
    guard = {"guard": True, "default": {"strict": "block", "standard": "note"}}
    plain = {"guard": False, "default": {"strict": "block", "standard": "note"}}
    counter = [0]

    def new_session():
        counter[0] += 1
        return "guard-test-%d-%d" % (os.getpid(), counter[0])

    def make_repo(settings=None, local=None, git=True):
        root = tempfile.mkdtemp(prefix="repo-", dir=scratch)
        if git:
            subprocess.run(["git", "-C", root, "init", "-q"], check=True, capture_output=True)
        os.makedirs(os.path.join(root, ".claude"))
        for fname, env in (("settings.json", settings), ("settings.local.json", local)):
            if env is not None:
                with open(os.path.join(root, ".claude", fname), "w", encoding="utf-8") as handle:
                    json.dump({"env": env}, handle)
        return root

    def call(name, manifest, root, sid, seg=None, bound=None, **env):
        """(exit code, stdout) of one refusal by the hook NAME, with ENV in the environment."""
        for k in ("LEAN_HOOK_GATE_G", "LEAN_HOOK_GATE_N", "LEAN_SKIP_GATE_G", "LEAN_HOOK_MODE", "LEAN_ALLOW_COMMIT_AUTHOR"):
            os.environ.pop(k, None)
        os.environ["CLAUDE_PROJECT_DIR"] = root
        os.environ.update(env)
        bind(name, manifest, bound)
        _state.event = {"session_id": sid, "cwd": root}
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            if seg is None:
                code = refuse("PreToolUse", "REFUSED")
            else:  # refuse() sees no command prefix, so a per-segment check goes through mode(seg)
                code = 2 if mode(seg) == "block" else 0
            flush_guard(code)
        return code, out.getvalue() + err.getvalue().replace("REFUSED\n", "")

    try:
        sid = new_session()
        root = make_repo({"LEAN_HOOK_GATE_G": "off"})
        code, out = call("gate-g.py", guard, root, sid, LEAN_HOOK_GATE_G="off")
        check("repo_loosen_ignored: blocks", code, 2)
        check("repo_loosen_ignored: names the repo settings",
              "LEAN_HOOK_GATE_G from this repo's .claude/settings.json is ignored: gate-g is a guard" in out, True)
        check("repo_loosen_ignored: the note says to use user settings", "~/.claude/settings.json" in out, True)
        code, out = call("gate-g.py", guard, root, sid, LEAN_HOOK_GATE_G="off")
        check("repo_loosen_ignored: second call blocks without a second note", (code, out), (2, ""))
        code, out = call("gate-g.py", guard, root, new_session(), LEAN_HOOK_GATE_G="off")
        check("repo_loosen_ignored: a new session gets its own note", (code, "is ignored" in out), (2, True))

        root = make_repo({"LEAN_HOOK_GATE_G": "off"}, {"LEAN_HOOK_GATE_G": "off"})
        code, out = call("gate-g.py", guard, root, new_session(), LEAN_HOOK_GATE_G="off")
        check("local_loosen_kept", (code, out), (0, ""))
        root = make_repo({"LEAN_HOOK_GATE_G": "off"})
        with open(managed, "w", encoding="utf-8") as handle:
            json.dump({"env": {"LEAN_HOOK_GATE_G": "off"}}, handle)
        code, out = call("gate-g.py", guard, root, new_session(), LEAN_HOOK_GATE_G="off")
        check("managed_loosen_kept", (code, out), (0, ""))
        os.remove(managed)

        root = make_repo({})
        code, out = call("gate-g.py", guard, root, new_session(), LEAN_HOOK_GATE_G="off")
        check("user_loosen_kept", (code, out), (0, ""))
        root = make_repo({"LEAN_HOOK_GATE_G": "block"})
        code, out = call("gate-g.py", guard, root, new_session(), LEAN_HOOK_GATE_G="off")
        check("a repo value that differs from the env is not the repo's", (code, out), (0, ""))
        root = make_repo(None, git=False)
        code, out = call("gate-g.py", guard, root, new_session(), LEAN_HOOK_GATE_G="off")
        check("no repo settings file", (code, out), (0, ""))

        root = make_repo({})
        code, out = call("gate-g.py", guard, root, new_session(), _Seg(LEAN_HOOK_GATE_G="off"))
        check("prefix_loosen_ignored: blocks", code, 2)
        check("prefix_loosen_ignored: names the command prefix",
              "LEAN_HOOK_GATE_G from the command prefix is ignored" in out, True)
        code, out = call("gate-g.py", guard, root, new_session(), _Seg(LEAN_SKIP_GATE_G="1"))
        check("a prefix LEAN_SKIP_ is ignored too", (code, "LEAN_SKIP_GATE_G from the command prefix" in out), (2, True))
        code, out = call("gate-g.py", guard, root, new_session(), _Seg(LEAN_HOOK_GATE_G="note"))
        check("a prefix note over a block default is ignored", (code, "is ignored" in out), (2, True))
        code, out = call("gate-g.py", guard, root, new_session(), _Seg(LEAN_HOOK_GATE_G="off"), LEAN_HOOK_GATE_G="off")
        check("a dropped prefix falls through to a user env loosening", (code, out.count("is ignored")), (0, 1))

        os.environ["LEAN_PROFILE"] = "standard"
        code, out = call("gate-g.py", guard, root, new_session(), _Seg(LEAN_HOOK_GATE_G="block"))
        check("prefix_tighten_kept", (code, out), (2, ""))
        code, out = call("gate-g.py", guard, make_repo({"LEAN_HOOK_GATE_G": "block"}), new_session(),
                         LEAN_HOOK_GATE_G="block")
        check("a tighter repo value is kept", (code, out), (2, ""))
        os.environ["LEAN_PROFILE"] = "strict"
        code, out = call("gate-n.py", plain, root, new_session(), _Seg(LEAN_HOOK_GATE_N="off"))
        check("non_guard_prefix_kept", (code, out), (0, ""))
        code, out = call("gate-n.py", plain, make_repo({"LEAN_HOOK_GATE_N": "off"}), new_session(),
                         LEAN_HOOK_GATE_N="off")
        check("a non-guard repo loosening is kept", (code, out), (0, ""))

        # the old names are covered too
        root = make_repo({"LEAN_ALLOW_COMMIT_AUTHOR": "1"})
        code, out = call("commit-author.py", guard, root, new_session(), LEAN_ALLOW_COMMIT_AUTHOR="1")
        check("an old name in repo settings does not loosen a guard",
              (code, "LEAN_ALLOW_COMMIT_AUTHOR from this repo" in out), (2, True))
        code, out = call("commit-author.py", guard, make_repo({}), new_session(), _Seg(LEAN_ALLOW_COMMIT_AUTHOR="1"))
        check("an old name in the prefix does not loosen a guard",
              (code, "LEAN_ALLOW_COMMIT_AUTHOR from the command prefix" in out), (2, True))
        code, out = call("commit-author.py", guard, make_repo({}), new_session(), LEAN_ALLOW_COMMIT_AUTHOR="1")
        check("an old name in the user env still loosens", (code, out), (0, ""))

        # a tagged LEAN_HOOK_MODE the repo put there
        root = make_repo({"LEAN_HOOK_MODE": "GATE_G:off"})
        code, out = call("gate-g.py", guard, root, new_session(), bound="off", LEAN_HOOK_MODE="GATE_G:off")
        check("a looser LEAN_HOOK_MODE from the repo is dropped", (code, "LEAN_HOOK_MODE from this repo" in out), (2, True))
        code, out = call("gate-g.py", guard, make_repo({}), new_session(), bound="off", LEAN_HOOK_MODE="GATE_G:off")
        check("a looser LEAN_HOOK_MODE from the user is kept", (code, out), (0, ""))

        # disabled() inside a guard
        for k in ("LEAN_HOOK_MODE", "LEAN_ALLOW_COMMIT_AUTHOR"):
            os.environ.pop(k, None)
        bind("gate-g.py", guard)
        root = make_repo({"LEAN_SKIP_PUBLIC_TEXT": "1"})
        os.environ["CLAUDE_PROJECT_DIR"] = root
        _state.event = {"session_id": new_session(), "cwd": root}
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            check("disabled ignores a prefix in a guard",
                  disabled("LEAN_SKIP_PUBLIC_TEXT", _Seg(LEAN_SKIP_PUBLIC_TEXT="1")), False)
            os.environ["LEAN_SKIP_PUBLIC_TEXT"] = "1"
            check("disabled ignores an env value the repo set", disabled("LEAN_SKIP_PUBLIC_TEXT"), False)
            flush_guard(0)
        check("disabled says so once per session and name", captured.getvalue().count("is ignored"), 1)
        check("disabled names the prefix", "from the command prefix" in captured.getvalue(), True)
        os.environ["CLAUDE_PROJECT_DIR"] = make_repo({})
        check("disabled keeps a user env value in a guard", disabled("LEAN_SKIP_PUBLIC_TEXT"), True)
        bind("gate-n.py", plain)
        check("disabled keeps a prefix in a non-guard",
              disabled("LEAN_SKIP_PUBLIC_TEXT", _Seg(LEAN_SKIP_PUBLIC_TEXT="1")), True)

        # LEAN_PROFILE from the repo
        def profile_call(manifest, repo_env, **env):
            code, out = call("gate-g.py", manifest, make_repo(repo_env), new_session(), **env)
            os.environ["LEAN_PROFILE"] = "strict"
            return code, out
        code, out = profile_call(guard, {"LEAN_PROFILE": "off"}, LEAN_PROFILE="off")
        check("repo LEAN_PROFILE=off does not turn a guard off", (code, "LEAN_PROFILE from this repo" in out), (2, True))
        code, out = profile_call(plain, {"LEAN_PROFILE": "off"}, LEAN_PROFILE="off")
        check("repo LEAN_PROFILE=off turns a non-guard off", (code, out), (0, ""))
        code, out = profile_call(guard, {}, LEAN_PROFILE="off")
        check("a user LEAN_PROFILE=off turns a guard off", (code, out), (0, ""))
        code, out = profile_call(guard, {"LEAN_PROFILE": "standard"}, LEAN_PROFILE="standard")
        check("repo LEAN_PROFILE=standard keeps a guard at its strict default", (code, "LEAN_PROFILE from this repo" in out), (2, True))

        # the project directory below the git root, and a committed settings.local.json
        root = make_repo(None)
        os.makedirs(os.path.join(root, "app", ".claude"))
        with open(os.path.join(root, "app", ".claude", "settings.json"), "w", encoding="utf-8") as handle:
            json.dump({"env": {"LEAN_HOOK_GATE_G": "off"}}, handle)
        code, out = call("gate-g.py", guard, os.path.join(root, "app"), new_session(), LEAN_HOOK_GATE_G="off")
        check("settings in the project dir below the git root count as the repo's", (code, "this repo" in out), (2, True))
        root = make_repo({"LEAN_HOOK_GATE_G": "off"})
        os.makedirs(os.path.join(root, "app"))
        code, out = call("gate-g.py", guard, os.path.join(root, "app"), new_session(), LEAN_HOOK_GATE_G="off")
        check("settings at the git root count when the project dir is below it", (code, "this repo" in out), (2, True))
        root = make_repo({"LEAN_HOOK_GATE_G": "off"}, {"LEAN_HOOK_GATE_G": "off"})
        code, out = call("gate-g.py", guard, root, new_session(), LEAN_HOOK_GATE_G="off")
        check("an untracked settings.local.json is the user's", (code, out), (0, ""))
        subprocess.run(["git", "-C", root, "add", "-f", ".claude/settings.local.json"], check=True, capture_output=True)
        subprocess.run(["git", "-C", root, "-c", "user.email=t@example.org", "-c", "user.name=t", "commit", "-q",
                        "--no-verify", "-m", "x"], check=True, capture_output=True)
        code, out = call("gate-g.py", guard, root, new_session(), LEAN_HOOK_GATE_G="off")
        check("a committed settings.local.json is the repo's", (code, "this repo" in out), (2, True))
        check("a directory git cannot answer for is not the user's", _local_is_user(make_repo(None, git=False)), False)

        # a case variant or a symlink does not pass as the user's local settings
        root = make_repo({"LEAN_HOOK_GATE_G": "off"})
        for fname in ("Settings.Local.json", "settings.local.json"):
            with open(os.path.join(root, ".claude", fname), "w", encoding="utf-8") as handle:
                json.dump({"env": {"LEAN_HOOK_GATE_G": "off"}}, handle)
        subprocess.run(["git", "-C", root, "add", "-f", ".claude/Settings.Local.json"], check=True, capture_output=True)
        subprocess.run(["git", "-C", root, "-c", "user.email=t@example.org", "-c", "user.name=t", "commit", "-q",
                        "--no-verify", "-m", "x"], check=True, capture_output=True)
        code, out = call("gate-g.py", guard, root, new_session(), LEAN_HOOK_GATE_G="off")
        check("a committed case variant of settings.local.json is the repo's", (code, "this repo" in out), (2, True))
        root = make_repo(None)
        elsewhere = tempfile.mkdtemp(prefix="claude-dir-", dir=scratch)
        for fname in ("settings.json", "settings.local.json"):
            with open(os.path.join(elsewhere, fname), "w", encoding="utf-8") as handle:
                json.dump({"env": {"LEAN_HOOK_GATE_G": "off"}}, handle)
        shutil.rmtree(os.path.join(root, ".claude"))
        os.symlink(elsewhere, os.path.join(root, ".claude"))
        code, out = call("gate-g.py", guard, root, new_session(), LEAN_HOOK_GATE_G="off")
        check("a .claude symlink is not the user's", (code, "this repo" in out), (2, True))
        root = make_repo({"LEAN_HOOK_GATE_G": "off"}, {"LEAN_HOOK_GATE_G": "off"})
        marker = os.path.join(scratch, "fsmonitor-ran")
        hook_script = os.path.join(scratch, "fsmonitor.sh")
        with open(hook_script, "w", encoding="utf-8") as handle:
            handle.write("#!/bin/sh\ntouch %s\n" % marker)
        os.chmod(hook_script, 0o755)
        subprocess.run(["git", "-C", root, "config", "core.fsmonitor", hook_script], check=True, capture_output=True)
        code, out = call("gate-g.py", guard, root, new_session(), LEAN_HOOK_GATE_G="off")
        check("the plain untracked settings.local.json still counts as the user's", (code, out), (0, ""))
        check("the check does not run the repo's fsmonitor", os.path.exists(marker), False)

        # .claude as a submodule, or marked as a work tree of its own
        git_id = ["-c", "user.email=t@example.org", "-c", "user.name=t"]
        inner = tempfile.mkdtemp(prefix="claude-sub-", dir=scratch)
        subprocess.run(["git", "-C", inner, "init", "-q"], check=True, capture_output=True)
        for fname in ("settings.json", "settings.local.json"):
            with open(os.path.join(inner, fname), "w", encoding="utf-8") as handle:
                json.dump({"env": {"LEAN_HOOK_GATE_G": "off"}}, handle)
        subprocess.run(["git", "-C", inner, "add", "."], check=True, capture_output=True)
        subprocess.run(["git", "-C", inner] + git_id + ["commit", "-q", "--no-verify", "-m", "x"], check=True, capture_output=True)
        outer = make_repo(None)
        shutil.rmtree(os.path.join(outer, ".claude"))
        added = subprocess.run(["git", "-c", "protocol.file.allow=always", "-C", outer, "submodule", "add", "-q",
                                inner, ".claude"], capture_output=True, text=True)
        check("the submodule fixture was built", added.returncode, 0)
        code, out = call("gate-g.py", guard, outer, new_session(), LEAN_HOOK_GATE_G="off")
        check("a settings.local.json inside a .claude submodule is the repo's", (code, "this repo" in out), (2, True))
        check("a submodule .claude is not the user's", _local_is_user(outer), False)
        root = make_repo({"LEAN_HOOK_GATE_G": "off"}, {"LEAN_HOOK_GATE_G": "off"})
        with open(os.path.join(root, ".claude", ".git"), "w", encoding="utf-8") as handle:
            handle.write("gitdir: ../elsewhere\n")
        code, out = call("gate-g.py", guard, root, new_session(), LEAN_HOOK_GATE_G="off")
        check("a .claude/.git file alone is the repo's", (code, "this repo" in out), (2, True))
        root = make_repo({"LEAN_HOOK_GATE_G": "off"}, {"LEAN_HOOK_GATE_G": "off"})
        check("the plain untracked case is still the user's", _local_is_user(root), True)

        # settings the repo could use to hide a value
        root = make_repo(None)
        with open(os.path.join(root, ".claude", "settings.json"), "wb") as handle:
            handle.write(b"\xef\xbb\xbf" + json.dumps({"env": {"LEAN_HOOK_GATE_G": "off"}}).encode())
        code, out = call("gate-g.py", guard, root, new_session(), LEAN_HOOK_GATE_G="off")
        check("a BOM does not hide a repo setting", (code, "this repo" in out), (2, True))
        root = make_repo(None)
        with open(os.path.join(root, ".claude", "settings.json"), "w", encoding="utf-8") as handle:
            handle.write('{"env": {"LEAN_HOOK_GATE_G": "off",}')
        code, out = call("gate-g.py", guard, root, new_session(), LEAN_HOOK_GATE_G="off")
        check("a broken settings.json counts as setting every name", (code, "this repo" in out), (2, True))
        root = make_repo({"LEAN_SKIP_GATE_G": 1})
        code, out = call("gate-g.py", guard, root, new_session(), LEAN_SKIP_GATE_G="1")
        check("a non-string repo value counts as repo-sourced", (code, "this repo" in out), (2, True))

        # an error while checking a loosening counts as repo-sourced
        real_block = globals()["_repo_env"]

        def broken(path):
            raise RuntimeError("injected")
        globals()["_repo_env"] = broken
        try:
            code, out = call("gate-g.py", guard, make_repo({}), new_session(), LEAN_HOOK_GATE_G="off")
        finally:
            globals()["_repo_env"] = real_block
        check("fail closed: an error in the repo check blocks", (code, "this repo" in out), (2, True))

        # one JSON document on stdout
        fixture = os.path.join(scratch, "fixture-guard.py")
        here = os.path.dirname(os.path.realpath(__file__))
        with open(fixture, "w", encoding="utf-8") as handle:
            handle.write(
                "import sys\nsys.path.insert(0, %r)\nimport hookkit\n"
                "HOOK = {'events': [], 'guard': True, 'default': {'strict': 'block', 'standard': 'note'}}\n"
                "def main():\n    hookkit.read_event()\n    hookkit.guard_note('LEAN_SKIP_X', 'the command prefix')\n"
                "    if len(sys.argv) > 1:\n        return hookkit.refuse('PreToolUse', 'REFUSED')\n"
                "    hookkit.note('PreToolUse', 'HOOKNOTE')\n    return 0\n"
                "if __name__ == '__main__':\n    hookkit.run(main, HOOK)\n" % here)
        ev = {"session_id": new_session(), "tool_name": "Bash", "tool_input": {"command": "x"}}
        done = subprocess.run([sys.executable, fixture], input=json.dumps(ev), capture_output=True, text=True,
                              env=dict(os.environ, LEAN_PROFILE="strict"))
        try:
            doc = json.loads(done.stdout)
            text = doc["hookSpecificOutput"]["additionalContext"]
        except (ValueError, KeyError):
            text = ""
        check("a guard note and the hook's own note are one JSON document", ("HOOKNOTE" in text, "is ignored" in text), (True, True))
        ev["session_id"] = new_session()
        done = subprocess.run([sys.executable, fixture, "block"], input=json.dumps(ev), capture_output=True, text=True,
                              env=dict(os.environ, LEAN_PROFILE="strict"))
        check("on a block the guard note goes to stderr", (done.returncode, done.stdout, "is ignored" in done.stderr), (2, "", True))
        verify = os.path.join(here, "..", "hooks", "verify-claim.py")
        ev = {"session_id": new_session(), "tool_name": "Bash", "cwd": scratch,
              "tool_input": {"command": "LEAN_SKIP_VERIFY_CLAIM=1 gh pr review 4 --approve"}}
        code, out, err = probe(verify, ev, LEAN_PROFILE="strict")
        try:
            text = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        except (ValueError, KeyError):
            text = ""
        check("verify-claim gives one JSON document with both notes", (code, "is ignored" in text, "Thinking" in text or "verify" in text.lower()), (0, True, True))

        # root finding
        base = make_repo(None, git=False)
        os.environ["CLAUDE_PROJECT_DIR"] = os.path.join(base, "a", "b")
        check("no_git_root: falls back to the start directory", repo_root(), os.path.join(base, "a", "b"))
        check("no_git_root: from_repo does not raise", from_repo("LEAN_HOOK_GATE_G"), False)
        repo = make_repo({})
        os.makedirs(os.path.join(repo, "x", "y"))
        os.environ["CLAUDE_PROJECT_DIR"] = os.path.join(repo, "x", "y")
        check("repo_root walks up to .git", repo_root(), repo)
        shutil.rmtree(os.path.join(repo, ".git"))
        with open(os.path.join(repo, ".git"), "w", encoding="utf-8") as handle:
            handle.write("gitdir: elsewhere\n")
        check("repo_root accepts a .git file", repo_root(), repo)
        os.environ.pop("CLAUDE_PROJECT_DIR")
        _state.event = {"cwd": repo}
        check("repo_root falls back to the event cwd", repo_root(), repo)
    finally:
        MANAGED_SETTINGS, _RECORD = saved_managed, saved_record
        _state.name, _state.manifest, _state.mode, _state.event = saved_state
        for key, val in saved.items():
            os.environ.pop(key, None)
            if val is not None:
                os.environ[key] = val
        os.environ["LEAN_PROFILE"] = "strict"


def with_standard(level, standard):
    saved = os.environ.get("LEAN_PROFILE")
    os.environ["LEAN_PROFILE"] = level
    try:
        return resolve_mode(standard=standard)
    finally:
        os.environ["LEAN_PROFILE"] = saved if saved is not None else "strict"


def self_test():
    import contextlib
    import io
    import shutil
    import tempfile

    import leanstats

    global _RECORD

    os.environ["LEAN_PROFILE"] = "strict"
    failures = 0
    saved_brain = os.environ.get("LEAN_BRAIN_DB")  # the scratch brain set at import
    saved_notices = os.environ.get("LEAN_NOTICES")
    scratch = tempfile.mkdtemp(prefix="hookkit-selftest-")
    os.environ["LEAN_BRAIN_DB"] = os.path.join(scratch, "brain.db")
    os.environ.pop("LEAN_NOTICES", None)

    def check(name, got, want):
        nonlocal failures
        if got != want:
            print(f"FAIL {name}: got {got!r}, want {want!r}")
            failures += 1

    class _Seg:
        def __init__(self, **env):
            self.env = env

    for new, (old, old_value) in ALIASES.items():
        value = "1"
        for key in (new, old):
            os.environ.pop(new, None)
            os.environ.pop(old, None)
            check(f"{key} unset is not disabled", disabled(new), False)
            os.environ[key] = value
            check(f"{key}={value} in the environment disables {new}", disabled(new), True)
            os.environ.pop(key)
            check(f"{key}={value} in the env prefix disables {new}", disabled(new, _Seg(**{key: value})), True)
            check(f"{key}=0 in the env prefix does not disable {new}", disabled(new, _Seg(**{key: "0"})), False)
    for new, old, value in (("LEAN_HOOK_COMMIT_AUTHOR", "LEAN_ALLOW_COMMIT_AUTHOR", "off"),
                            ("LEAN_HOOK_SCRATCHPAD_ROOT", "LEAN_ALLOW_SCRATCHPAD_ROOT", "off"),
                            ("LEAN_HOOK_PUBLIC_TEXT", None, "off")):
        os.environ.pop(new, None)
        if old:
            os.environ.pop(old, None)
        check(f"{new} unset is not disabled", disabled(new, values=(value,)), False)
        os.environ[new] = value
        check(f"{new}=off in the environment", disabled(new, values=(value,)), True)
        os.environ[new] = "1"
        check(f"{new}=1 in the environment is not the new value", disabled(new, values=(value,)), False)
        os.environ.pop(new)
        check(f"{new}=off in the env prefix", disabled(new, _Seg(**{new: value}), (value,)), True)
        check(f"{new}=1 in the env prefix is not the new value", disabled(new, _Seg(**{new: "1"}), (value,)), False)
        if old:
            os.environ[old] = "1"
            check(f"{old}=1 in the environment still works for {new}", disabled(new, values=(value,)), True)
            os.environ.pop(old)
            check(f"{old}=1 in the env prefix still works for {new}", disabled(new, _Seg(**{old: "1"}), (value,)), True)

    with tempfile.TemporaryDirectory() as tmp:
        def manifest_error(body):
            target = os.path.join(tmp, "h.py")
            with open(target, "w", encoding="utf-8") as handle:
                handle.write(body)
            try:
                read_manifest(target)
            except ValueError as exc:
                return str(exc)
            return ""

        good = ('HOOK = {"events": [{"event": "PreToolUse", "matcher": "Bash", "group": 1, "order": 0}],\n'
                '        "timeout": 10, "mode": "process", "pack": "core", "guard": False,\n'
                '        "default": {"strict": "block", "standard": "off"}}\n')
        target = os.path.join(tmp, "good.py")
        with open(target, "w", encoding="utf-8") as handle:
            handle.write("import os\n" + good)
        check("read_manifest good", read_manifest(target)["events"][0]["group"], 1)
        check("read_manifest default", read_manifest(target)["default"]["standard"], "off")
        for name, body, want in (
            ("none", "x = 1\n", "no HOOK literal"),
            ("two", good + good, "two HOOK literals"),
            ("call", "HOOK = dict(a=1)\n", "not a literal"),
            ("key", good.replace('"mode"', '"mode2"'), "unknown key 'mode2'"),
            ("event", good.replace('"PreToolUse"', '"PreTool"'), "unknown event 'PreTool'"),
            ("group", good.replace('"group": 1, ', ""), "missing 'group'"),
            ("order", good.replace(', "order": 0', ""), "missing 'order'"),
            ("default", good.replace('"off"', '"warn"'), "default 'warn'"),
        ):
            check("read_manifest " + name, want in manifest_error(body), True)

    with tempfile.TemporaryDirectory() as tmp:
        base, r = os.path.join(tmp, "w"), os.path.join(tmp, "r")
        os.makedirs(os.path.join(r, "sub"))
        os.makedirs(base)
        segs = segments(f'cd {r} && GIT_AUTHOR_EMAIL=a@b git -C sub -c user.email=x@y commit -m "a && b"\ngit push', base)
        check("segment count", len(segs), 2)
        check("env prefix", segs[0].env, {"GIT_AUTHOR_EMAIL": "a@b"})
        check("cd followed", segs[0].cwd, r)
        call = git_call(segs[0])
        check("git subcommand", call[0], "commit")
        check("git -C", call[2], os.path.join(r, "sub"))
        check("git -c", call[3], {"user.email": "x@y"})
        check("quoted && kept", call[1], ["-m", "a && b"])
        check("second segment", git_call(segs[1])[0], "push")
        check("cd to a shell expression keeps the base", segments('cd "$(git rev-parse --show-toplevel)" && git status', base)[0].cwd, base)
        check("cd to $VAR keeps the base", segments('cd "$WT" && git status', base)[0].cwd, base)
        check("cd to a missing dir keeps the base", segments("cd nowhere && git status", base)[0].cwd, base)
        check("git -C $VAR keeps the base", git_call(segments('git -C "$WT" commit', base)[0])[2], base)
        os.makedirs(os.path.join(base, "$WT"))
        check("cd $VAR ignores a dir literally named $WT", segments('cd "$WT" && git status', base)[0].cwd, base)
    check("echo is not git", git_call(segments('echo "git commit"')[0]), None)
    check("grep is not git", [git_call(s) for s in segments("git log --grep 'git commit'")][0][0], "log")
    check("gh call", gh_call(segments("gh pr create --fill | cat")[0])[0][:2], ["pr", "create"])
    for cmd, kind, action in (("gh pr create --fill", "github", "create"), ("gh pr merge 3", "github", "merge"),
                              ("glab mr create -d x", "gitlab", "create"), ("glab mr merge 3", "gitlab", "merge"),
                              ("az repos pr create --title t", "azure", "create"),
                              ("az repos pr update --id 3 --status completed", "azure", "merge"),
                              ("az repos pr update --id 3 --status=completed", "azure", "merge"),
                              ("az repos pr update --id 3 --title t", "azure", "update"),
                              ("az repos pr update --id 3 --status Completed", "azure", "merge"),
                              ("az repos pr update --id 3 --draft false", "azure", "ready"),
                              ("az repos pr update --id 3 --draft=false", "azure", "ready"),
                              ("az repos pr update --id 3 --draft true", "azure", "update"),
                              ("gh pr ready 3", "github", "ready"), ("glab mr update 3 --ready", "gitlab", "ready"),
                              ("glab mr update 3 --title t", "gitlab", "update"),
                              ("glab mr new -d x", "gitlab", "create"), ("glab mr comment 3 -m hi", "gitlab", "text"),
                              ("glab issue comment 3 -m hi", "gitlab", "text"),
                              ("glab mr note 3 -m hi", "gitlab", "text"), ("glab mr note create 3 -m hi", "gitlab", "text"),
                              ("/usr/bin/glab mr merge 3", "gitlab", "merge"), ("GITLAB_HOST=x glab mr merge 3", "gitlab", "merge")):
        fc = forge_call(segments(cmd)[0])
        check(f"forge table {cmd}", (fc.kind, forge_action(fc)) if fc else None, (kind, action))
    for cmd in ("glab mr list", "az repos pr list", "az repos list", "gh pr view 3", "glab api projects/1", "echo glab mr merge 3",
                "az account show"):
        fc = forge_call(segments(cmd)[0])
        check(f"forge table ignores {cmd}", forge_action(fc), None)
    check("forge call on git", forge_call(segments("git status")[0]), None)
    check("note create keeps its rest", forge_call(segments("glab mr note create 3 -m hi")[0]).rest, ["3", "-m", "hi"])
    az = forge_call(segments("az repos pr create --title 'a b' --description 'one' 'two words' --auto-complete true")[0])
    check("az description takes every word", list(forge_flag_values(az, FORGE_FLAGS["az"]["text"])),
          [("--title", "a b"), ("--description", "one\ntwo words")])
    def az_values(command, args=None):
        return list(forge_flag_values(forge_call(segments(command)[0]), FORGE_FLAGS["az"]["text"], args))

    check("az bullet body", az_values("az repos pr create --title t --description '- a rob" + "ust fix'"),
          [("--title", "t"), ("--description", "- a rob" + "ust fix")])
    check("az first word then bullet", az_values("az repos pr create --description 'first line' '- rob" + "ust'"),
          [("--description", "first line\n- rob" + "ust")])
    check("az body starting with a number", az_values("az repos pr create --description '-1 x'"), [("--description", "-1 x")])
    check("az equals form with a bullet", az_values("az repos pr create --description='- x'"), [("--description", "- x")])
    check("az body stops at a long flag", az_values("az repos pr create --description a b --auto-complete true"),
          [("--description", "a\nb")])
    check("az body stops at a short flag", az_values("az repos pr create --description a b -s feat"), [("--description", "a\nb")])
    check("az flag with no value", az_values("az repos pr create --description"), [])
    az_call = forge_call(segments("az repos pr create")[0])
    check("az body stops at a separator",
          list(forge_flag_values(az_call, FORGE_FLAGS["az"]["text"], ["--title", "t", "&&", "echo", "-", "x"])),
          [("--title", "t")])
    check("az separator right after the flag", list(forge_flag_values(az_call, FORGE_FLAGS["az"]["text"], ["--title", "&&", "x"])), [])
    check("glab squash message is text", list(forge_flag_values(forge_call(segments("glab mr merge 3 --squash-message 'a b'")[0]),
                                                                FORGE_FLAGS["glab"]["text"])), [("--squash-message", "a b")])
    glab = forge_call(segments("glab mr create --title=t -d 'body here'")[0])
    check("glab flag forms", list(forge_flag_values(glab, FORGE_FLAGS["glab"]["text"])), [("--title", "t"), ("-d", "body here")])
    check("unbalanced quote falls back", len(segments('git commit -m "oops')) >= 1, True)
    check("resolve relative", resolve("a/b", "/x"), "/x/a/b")
    check("comment ends at the newline", [s.words for s in segments("git add . # stage\ngit commit -m x")],
          [["git", "add", "."], ["git", "commit", "-m", "x"]])
    check("hash inside a word is not a comment", segments("echo a#b")[0].words, ["echo", "a#b"])
    check("quoted hash is not a comment", segments("git commit -m '# 1'")[0].words[-1], "# 1")
    heredoc = segments("cd / && git commit -F - <<'EOF'\nsubject\n\ncd /tmp\nEOF\ngit push", "/")
    check("heredoc body is not a command", [s.words[:2] for s in heredoc], [["git", "commit"], ["git", "push"]])
    check("heredoc body attached", heredoc[0].heredocs, ["subject\n\ncd /tmp\n"])
    check("heredoc <<- strips tabs", segments("cat <<-X\n\tbody\n\tX\necho done")[0].heredocs, ["\tbody\n"])
    wrapped = segments("flock -o /tmp/l nice -n 5 timeout 60 dotnet test 2>&1 | tail -5")
    check("wrappers stripped", wrapped[0].words[:2], ["dotnet", "test"])
    check("piped segment", [s.piped for s in wrapped], [True, False])
    check("env wrapper", segments("env LEAN_X=1 nice git commit -m x")[0].env, {"LEAN_X": "1"})
    check("time wrapper", git_call(segments("time git commit -m x")[0])[0], "commit")
    check("inline off switch", disabled("LEAN_NOT_SET_ANYWHERE", segments("LEAN_NOT_SET_ANYWHERE=1 git commit")[0]), True)
    check("off switch not set", disabled("LEAN_NOT_SET_ANYWHERE", segments("git commit")[0]), False)
    def with_profile(value, standard, note_text=None):
        saved_profile = os.environ.get("LEAN_PROFILE")
        if value is None:
            os.environ.pop("LEAN_PROFILE", None)
        else:
            os.environ["LEAN_PROFILE"] = value
        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = refuse("PreToolUse", "T", standard=standard, note=note_text)
            return code, out.getvalue(), err.getvalue(), profile()
        finally:
            if saved_profile is None:
                os.environ.pop("LEAN_PROFILE", None)
            else:
                os.environ["LEAN_PROFILE"] = saved_profile

    check("profile unset is standard", with_profile(None, "nothing")[3], "standard")
    check("profile empty is strict", with_profile("", "nothing")[3], "strict")
    check("profile blank is strict", with_profile("  ", "nothing")[3], "strict")
    check("profile typo is strict", with_profile("stnadard", "nothing")[3], "strict")
    check("profile is case-insensitive", with_profile(" OFF ", "nothing")[3], "off")
    check("strict prints and returns 2", with_profile("strict", "nothing")[:3], (2, "", "T\n"))
    check("standard nothing", with_profile("standard", "nothing")[:3], (0, "", ""))
    check("standard nothing, no manifest, no output", with_profile("standard", "nothing")[:2], (0, ""))
    check("standard block", with_profile("standard", "block")[:3], (2, "", "T\n"))
    check("standard note uses the note", json.loads(with_profile("standard", "note", "N")[1])["hookSpecificOutput"]["additionalContext"], "N")
    check("standard note falls back to the text", json.loads(with_profile("standard", "note")[1])["hookSpecificOutput"]["additionalContext"], "T")
    check("a mistyped standard value blocks", with_profile("standard", "nothin")[:3], (2, "", "T\n"))
    want_note = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": "hello"}}

    def noted():
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            note("PreToolUse", "hello")
        return json.loads(out.getvalue())

    def exit_code(main):
        try:
            run(main)
        except SystemExit as exc:
            return exc.code
        return "no exit"

    def bail():
        sys.exit(2)

    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "lean.db")
        _RECORD, _state.event = True, {"session_id": "s1", "cwd": "/w"}
        try:
            with leanstats.temp_env(LEAN_DB=db, LEAN_STATS=None):
                check("note output", noted(), want_note)
                check("exit 2 kept", exit_code(lambda: 2), 2)
                check("exit 0 kept", exit_code(lambda: 0), 0)
                check("exit 1 kept", exit_code(lambda: 1), 1)
                check("sys.exit(2) inside main kept", exit_code(bail), 2)
                conn = leanstats.connect()
                fires = [tuple(r) for r in conn.execute(
                    "SELECT hook, outcome, session_id, cwd FROM hook_fires ORDER BY rowid")]
                conn.close()
                check("a note is one note row, an exit 2 is one block row", fires, [
                    ("hookkit.py", "note", "s1", "/w"),
                    ("hookkit.py", "block", "s1", "/w"),
                    ("hookkit.py", "block", "s1", "/w"),
                ])
            blocker = os.path.join(tmp, "file")
            open(blocker, "w").close()
            with leanstats.temp_env(LEAN_DB=os.path.join(blocker, "lean.db"), LEAN_STATS=None):
                check("unwritable database, same note output", noted(), want_note)
                check("unwritable database, same exit code", exit_code(lambda: 2), 2)
            _RECORD = False
            quiet = os.path.join(tmp, "quiet.db")
            with leanstats.temp_env(LEAN_DB=quiet, LEAN_STATS=None):
                noted()
                check("a self-test run records nothing", os.path.exists(quiet), False)
        finally:
            _RECORD, _state.event = False, {}
    saved_env = {k: os.environ.get(k) for k in ("CLAUDE_ENV_FILE", "PATH", "HOME")}
    saved_event = _state.event
    home = tempfile.mkdtemp(prefix="hookkit-home-")
    try:
        os.environ.pop("CLAUDE_ENV_FILE", None)
        os.environ["PATH"] = "/usr/bin"
        os.environ["HOME"] = home
        _state.event = {}
        check("tool_path is absolute and exists", (os.path.isabs(tool_path("brain")), os.path.exists(TOOLS + "/brain")), (True, True))
        os.environ["CLAUDE_ENV_FILE"] = os.path.join(home, "no-such-dir", "env")  # session-env cannot write it
        _state.event = {"hook_event_name": "SessionStart", "session_id": "m-1"}
        check("SessionStart with an unwritable env file and no marker gives the absolute path",
              tool_path("brain"), shlex.quote(os.path.join(TOOLS, "brain")))
        _state.event = {"hook_event_name": "PreToolUse", "session_id": "m-1"}
        check("CLAUDE_ENV_FILE alone does not count outside SessionStart", tools_on_path(), False)
        os.environ.pop("CLAUDE_ENV_FILE")
        marker = marker_file("m-1")
        os.makedirs(os.path.dirname(marker))
        with open(marker, "w") as fh:
            fh.write(TOOLS)
        check("the session's marker puts the tools on PATH", tool_path("brain"), "brain")
        _state.event = {"hook_event_name": "PreToolUse", "session_id": "m-2"}
        check("another session has no marker", tool_path("brain").startswith("/"), True)
        with open(marker, "w") as fh:
            fh.write("/old/tools")
        _state.event = {"hook_event_name": "PreToolUse", "session_id": "m-1"}
        check("a marker for another folder does not count", tools_on_path(), False)
        _state.event = {}
        os.environ["PATH"] = "/usr/bin" + os.pathsep + TOOLS
        check("tool_path is the bare name when PATH names the tools", tool_path("brain"), "brain")
        check("tool_path with a base stays a path", tool_path("brain", "/a/tools"), "/a/tools/brain")
    finally:
        _state.event = saved_event
        shutil.rmtree(home, True)
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    check("tool_path quotes a path with a space", tool_path("brain", "/a b/tools"), "'/a b/tools/brain'")
    check("tool_path leaves a plain path bare", tool_path("brain", "/a/tools"), "/a/tools/brain")

    import sqlite3
    import time

    def notice_rows(path):
        if not os.path.exists(path):
            return None
        con = sqlite3.connect(path)
        try:
            return [tuple(r) for r in con.execute("SELECT hook, text, session_id, cwd FROM notices")]
        except sqlite3.Error:
            return None
        finally:
            con.close()

    try:
        with tempfile.TemporaryDirectory() as tmp:
            bdb = os.path.join(tmp, "brain.db")
            os.environ["LEAN_BRAIN_DB"] = bdb
            os.environ.pop("LEAN_NOTICES", None)
            with leanstats.temp_env(LEAN_DB=os.path.join(tmp, "lean.db"), LEAN_STATS=None):
                _RECORD, _state.event = True, {"session_id": "s1", "cwd": "/w"}
                check("note output with notices on", noted(), want_note)
                check("note leaves one notice", notice_rows(bdb), [("hookkit.py", "hello", "s1", "/w")])
                os.environ["LEAN_NOTICES"] = "off"
                os.environ["LEAN_BRAIN_DB"] = off_db = os.path.join(tmp, "off.db")
                noted()
                check("LEAN_NOTICES=off records nothing", os.path.exists(off_db), False)
                os.environ.pop("LEAN_NOTICES")
                _RECORD = False
                os.environ["LEAN_BRAIN_DB"] = quiet_db = os.path.join(tmp, "quiet-brain.db")
                noted()
                check("a self-test run keeps no notice", os.path.exists(quiet_db), False)
                _RECORD = True
                locked = os.path.join(tmp, "locked.db")
                os.environ["LEAN_BRAIN_DB"] = locked
                from brain import store
                store.connect().close()
                holder = sqlite3.connect(locked, isolation_level=None)
                holder.execute("BEGIN EXCLUSIVE")
                t0 = time.time()
                check("locked brain, same note output", noted(), want_note)
                check("locked brain returns within 2 s", time.time() - t0 < 2, True)
                holder.close()
                check("locked brain kept nothing", notice_rows(locked), [])

        # a child of a self-test, with no LEAN_BRAIN_DB given, lands in the scratch brain and not the real one
        import subprocess
        child_env = dict(os.environ)
        child_env.pop("LEAN_BRAIN_DB", None)
        child_env.pop("LEAN_NOTICES", None)
        code = ("import os, sys; sys.path.insert(0, sys.argv[1]); import hookkit; hookkit._RECORD = True;"
                " hookkit.note('PreToolUse', 'child'); db = os.environ['LEAN_BRAIN_DB']; import sqlite3;"
                " print(db); print(sqlite3.connect(db).execute('SELECT hook, text FROM notices').fetchall())")
        done = subprocess.run([sys.executable, "-c", code, os.path.dirname(os.path.realpath(__file__)), "--self-test"],
                              env=child_env, capture_output=True, text=True)
        lines = done.stdout.strip().splitlines()
        child_db, child_rows = (lines[-2], lines[-1]) if len(lines) >= 2 else ("", "")
        real_db = os.path.realpath(os.path.expanduser("~/.local/share/lean/brain.db"))
        check("child gets a scratch brain", bool(child_db) and os.path.realpath(child_db) != real_db, True)
        check("child kept its notice in the scratch brain", child_rows, "[('-c', 'child')]")
        _switch_tests(check, scratch)
        _guard_tests(check, scratch)
        os.environ["LEAN_PROFILE"] = "strict"
    finally:
        _RECORD, _state.event = False, {}
        shutil.rmtree(scratch, ignore_errors=True)
        for key, val in (("LEAN_BRAIN_DB", saved_brain), ("LEAN_NOTICES", saved_notices)):
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val
    print("self-test: ok" if failures == 0 else f"self-test: {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        sys.exit(self_test())
    print(__doc__)

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


def refuse(event_name, text, *, standard="note", note=None):
    """Refuse a tool call the way the profile says. Strict prints TEXT and returns 2, as ever.

    Standard: "note" gives Claude NOTE (or TEXT) and lets the call run, "nothing" lets it run silently,
    "block" or any other value is the same as strict. Returns the hook's exit code.
    """
    if profile() == "strict" or standard not in ("note", "nothing"):
        print(text, file=sys.stderr)
        return 2
    if standard == "note":
        globals()["note"](event_name, note or text)
    return 0


def probe(script, event, **env):
    """Self-test helper: run a hook script on EVENT with env overrides (None removes). Returns (code, stdout, stderr)."""
    full = dict(os.environ)
    for key, value in env.items():
        if value is None:
            full.pop(key, None)
        else:
            full[key] = value
    p = subprocess.run([sys.executable, script], input=json.dumps(event), capture_output=True, text=True, env=full)
    return p.returncode, p.stdout, p.stderr


def run(main):
    """Run a hook's main and exit with its code; an internal error exits 0."""
    if profile() == "off":
        sys.exit(0)
    try:
        code = main()
    except SystemExit as exc:
        if exc.code == 2:
            _fire("block")
        raise
    except Exception as exc:  # a broken hook must not block the tool call
        print(f"lean-agent hook error ignored: {exc!r}", file=sys.stderr)
        code = 0
    if code == 2:
        _fire("block")
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


def note(event_name, text):
    """Print a non-blocking note that Claude reads with the tool result."""
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": event_name, "additionalContext": text}}))
    sys.stdout.flush()
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
    """True when NAME, or its old alias, has an accepted value in the environment or in this command's env prefix."""
    pairs = [(name, tuple(values))]
    if name in ALIASES:
        old, old_value = ALIASES[name]
        pairs.append((old, (old_value,)))
    for key, accepted in pairs:
        if os.environ.get(key) in accepted or bool(seg and seg.env.get(key) in accepted):
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

#!/usr/bin/env python3
"""Point a hand-written command at the lean-agent script that already does it.

PreToolUse hook on Bash. It adds a note, never a deny, in two cases:
  - the command matches a family in lib/script_families.json that has a covering script:
    "`<script>` does this: <usage>". Once per family per session.
  - the command calls a lean-agent script by its .../plugins/cache/lean-agent/lean-agent/
    <version>/bin/ path: call it by name, since bin is on PATH. Once per session.
A family whose covering script is a hook (wait-loop, pgrep) gets no note here, and neither
does a command that already names the covering script.

The family patterns are not copied. bin/script-repeats owns them and its load_families and
classify do the matching, so the retro's count and this note read the same file. Each
family may carry a `usage` line in that file; no usage, no note.

State: one small JSON file per session under ~/.local/share/lean/script-note (mode 0700, not
a symlink, owned by the user), written with mkstemp then os.replace. State files older than
14 days are deleted when one is written. A session id outside [A-Za-z0-9-]{1,100} or a
folder that cannot be trusted means no note. A hook error never fails the session.

  LEAN_SCRIPT_NOTE=off   turn it off (environment or settings env)
  script-note.py --self-test
"""
import importlib.machinery
import importlib.util
import json
import os
import re
import stat
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "lib"))
import hookkit  # noqa: E402

REPEATS = os.path.join(HERE, "..", "bin", "script-repeats")
PATH_FAMILY = "versioned-plugin-path"
BIN_PATH = re.compile(r"lean-agent/lean-agent/\d+\.\d+\.\d+/bin/([A-Za-z0-9][\w.-]*)")
SID = re.compile(r"[A-Za-z0-9-]{1,100}")
MAX_AGE = 14 * 86400
_REPEATS = None


def repeats():
    """bin/script-repeats as a module, loaded once per process without writing bytecode."""
    global _REPEATS
    if _REPEATS is None:
        sys.dont_write_bytecode = True
        loader = importlib.machinery.SourceFileLoader("script_repeats", os.path.realpath(REPEATS))
        spec = importlib.util.spec_from_loader("script_repeats", loader)
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
        _REPEATS = module
    return _REPEATS


def usages():
    """Family name to its usage line, read from the file script-repeats reads."""
    with open(repeats().FAMILIES_FILE) as f:
        raw = json.load(f)["families"]
    return {fam["name"]: fam for fam in raw}


def candidates(command):
    """(family, note) for each family the command belongs to, before the once-per-session cut."""
    module = repeats()
    fams = module.load_families()
    hits = module.classify(command, fams)
    if not hits:
        return []
    meta = usages()
    code = None
    out = []
    for name in hits:
        fam = meta[name]
        if name == PATH_FAMILY:
            m = BIN_PATH.search(command[:module.CAP])
            if m:
                out.append((name, "Call `%s` by name, not by its plugin cache path. The plugin's bin "
                            "is on PATH and the path breaks after an upgrade." % m.group(1)))
            continue
        script = fam.get("covered_by", "none")
        usage = fam.get("usage")
        if script == "none" or "/" in script or not usage or fam.get("kind", "script") != "script":
            continue
        if code is None:
            code = module.strip_prose(command[:module.CAP])
        if re.search(r"(?<![\w.-])" + re.escape(script) + r"(?![\w-])", code):
            continue
        out.append((name, "`%s` does this: %s" % (script, usage)))
    return out


def state_dir():
    return os.path.abspath(os.path.join(os.path.expanduser("~"), ".local", "share", "lean", "script-note"))


def trusted_dir(path):
    """Create path with mode 0700 if missing. False for a symlink, a non-directory or a foreign owner."""
    if not os.path.lexists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            os.mkdir(path, 0o700)
        except FileExistsError:
            pass
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        return False
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        return False
    if stat.S_IMODE(info.st_mode) != 0o700:
        os.chmod(path, 0o700)
    return True


def prune(path):
    cutoff = time.time() - MAX_AGE
    for entry in os.listdir(path):
        full = os.path.join(path, entry)
        try:
            info = os.lstat(full)
            if stat.S_ISREG(info.st_mode) and entry.endswith(".json") and info.st_mtime < cutoff:
                os.unlink(full)
        except OSError:
            pass


def read_seen(file):
    try:
        with open(file) as f:
            seen = json.load(f)
    except (OSError, ValueError):
        return []
    return [s for s in seen if isinstance(s, str)] if isinstance(seen, list) else []


def write_seen(path, file, seen):
    fd, tmp = tempfile.mkstemp(dir=path, prefix=".tmp-", suffix=".part")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(seen, f)
        os.replace(tmp, file)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def fresh(session, found):
    """The notes in found not shown yet in this session, and remember them."""
    if not isinstance(session, str) or not SID.fullmatch(session):
        return []
    path = state_dir()
    if not trusted_dir(path):
        return []
    file = os.path.join(path, session + ".json")
    seen = read_seen(file)
    new = [(name, text) for name, text in found if name not in seen]
    if new:
        write_seen(path, file, seen + [name for name, _ in new])
        prune(path)
    return [text for _, text in new]


def main():
    if os.environ.get("LEAN_SCRIPT_NOTE", "").lower() == "off":
        return 0
    event = hookkit.read_event()
    command = hookkit.tool_input(event).get("command")
    if not isinstance(command, str) or not command.strip():
        return 0
    found = candidates(command)
    if not found:
        return 0
    texts = fresh(event.get("session_id"), found)
    if texts:
        hookkit.note("PreToolUse", "\n".join(texts))
    return 0


def self_test():
    import contextlib
    import io
    import subprocess

    global _REPEATS
    failures = 0
    import pwd

    if os.environ.get("HOME", "") == pwd.getpwuid(os.getuid()).pw_dir:
        print("self-test: set HOME to a temporary directory first")
        return 1
    os.environ.pop("LEAN_SCRIPT_NOTE", None)

    def check(label, ok, detail=""):
        nonlocal failures
        if not ok:
            print("FAIL %s %s" % (label, detail))
            failures += 1

    def fire(command, session="sess-1"):
        old = sys.stdin
        sys.stdin = io.StringIO(json.dumps({"tool_input": {"command": command}, "session_id": session}))
        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = main()
        finally:
            sys.stdin = old
        text = ""
        if out.getvalue():
            text = json.loads(out.getvalue())["hookSpecificOutput"]["additionalContext"]
        return rc, text, err.getvalue()

    dash = chr(0x2014)
    slop = "compre" + "hensive"
    covered = [
        ("style-slop-grep", "style-scan", "grep -rn '%s' docs" % dash),
        ("mutation-check", "holdout.sh", "./mutate src/app.py && echo done"),
        ("curl-smoke-check", "check-deployed-sha.sh", "curl -s https://example.com/health"),
        ("inline-python-replace", "replace", "python3 - <<'X'\ns=open(p).read()\ns=s.replace(a,b)\nX"),
        ("direct-test-run", "test-phase", "pytest -q tests"),
        ("package-version-lookup", "pkg-version", "npm view left-pad version"),
        ("worktree-status-sweep", "where", "for d in a b; do git -C $d status; done"),
        ("graphql-review-threads", "pr-blockers.sh",
         "gh api graphql -f query='{ repository { pullRequest { reviewThreads(first: 5) { nodes { id } } } } }'"),
    ]
    near = {
        "style-slop-grep": "grep -rn foo docs",
        "mutation-check": 'echo "mutation"',
        "curl-smoke-check": "curl https://example.com/data.json -o data.json",
        "inline-python-replace": "python3 build.py",
        "direct-test-run": "pip install pytest",
        "package-version-lookup": "npm install left-pad",
        "worktree-status-sweep": "git status",
        "graphql-review-threads": "gh api repos/a/b/pulls",
    }
    meta = usages()
    for name, script, command in covered:
        rc, text, err = fire(command, "fam-" + name)
        check(name + " note", rc == 0 and text.startswith("`%s` does this: " % script) and not err, repr(text))
        check(name + " usage from the shared file", meta[name]["usage"] in text, repr(text))
        rc, again, _ = fire(command, "fam-" + name)
        check(name + " once per session", rc == 0 and again == "", repr(again))
        rc, other, _ = fire(command, "fam-" + name + "-b")
        check(name + " again in a new session", other == text, repr(other))
        rc, text, _ = fire(near[name], "near-" + name)
        check(name + " near miss", rc == 0 and text == "", repr(text))
    check("slop word grep", fire("rg %s notes" % slop, "slop")[1].startswith("`style-scan`"))

    check("unrelated command", fire("ls -la && echo hi", "plain")[1] == "")
    check("wait-loop family gets no note here", fire("pgrep -f dotnet", "pg")[1] == "")
    check("a command naming the script gets no note", fire("holdout.sh --spec x.md  # mutation", "named")[1] == "")
    rc, text, _ = fire("~/.claude/plugins/cache/lean-agent/lean-agent/1.14.1/bin/style-scan --help", "vp")
    check("versioned path note", "`style-scan`" in text and "by name" in text, repr(text))
    check("versioned path once", fire("~/.claude/plugins/cache/lean-agent/lean-agent/1.14.1/bin/replace x", "vp")[1] == "")
    check("hooks path is not noted",
          fire("python3 ~/.claude/plugins/cache/lean-agent/lean-agent/1.14.1/hooks/wait-loop.py --self-test", "vh")[1] == "")
    rc, text, _ = fire("pytest -q; npm view left-pad version", "two")
    check("two families, one note each", text.count("does this:") == 2, repr(text))

    os.environ["LEAN_SCRIPT_NOTE"] = "off"
    check("off switch", fire("pytest -q", "offsw") == (0, "", ""))
    os.environ["LEAN_SCRIPT_NOTE"] = "OFF"
    check("off switch, any case", fire("pytest -q", "offsw") == (0, "", ""))
    del os.environ["LEAN_SCRIPT_NOTE"]
    check("off switch leaves no state", not os.path.exists(os.path.join(state_dir(), "offsw.json")))

    for bad in (None, "", "a/b", "../x", "a b", "x" * 101, "sess_1", 5):
        check("bad session id %r gets no note" % (bad,), fire("pytest -q", bad)[1] == "")
    check("a 100 character session id works", fire("pytest -q", "a" * 100)[1] != "")
    for junk in ("not json", "{}", '{"tool_input":{"command":5}}', '{"tool_input":{"command":"  "}}'):
        old = sys.stdin
        sys.stdin = io.StringIO(junk)
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                rc = main()
        finally:
            sys.stdin = old
        check("junk input " + junk[:12], rc == 0 and not out.getvalue())

    # state folder
    path = state_dir()
    check("state folder is 0700", stat.S_IMODE(os.lstat(path).st_mode) == 0o700)
    os.chmod(path, 0o755)
    fire("pytest -q", "chmod-1")
    check("state folder mode repaired", stat.S_IMODE(os.lstat(path).st_mode) == 0o700)
    check("no temp files left", not [f for f in os.listdir(path) if f.endswith(".part")])
    old_file = os.path.join(path, "ancient.json")
    with open(old_file, "w") as f:
        f.write("[]")
    past = time.time() - 15 * 86400
    os.utime(old_file, (past, past))
    young_file = os.path.join(path, "young.json")
    with open(young_file, "w") as f:
        f.write("[]")
    os.utime(young_file, (time.time() - 13 * 86400,) * 2)
    fire("pytest -q", "prune-1")
    check("state older than 14 days is deleted", not os.path.exists(old_file))
    check("state younger than 14 days stays", os.path.exists(young_file))

    saved = os.environ["HOME"]
    for kind in ("symlink", "foreign", "file"):
        other = tempfile.mkdtemp(prefix="script-note-home-")
        os.environ["HOME"] = other
        base = os.path.dirname(state_dir())
        os.makedirs(base)
        target = tempfile.mkdtemp(prefix="script-note-target-")
        if kind == "symlink":
            os.symlink(target, state_dir())
        elif kind == "file":
            open(state_dir(), "w").close()
        else:
            os.mkdir(state_dir(), 0o700)
        real_uid = getattr(os, "getuid", None)
        if kind == "foreign":
            if real_uid is None:
                continue
            os.getuid = lambda: os.lstat(state_dir()).st_uid + 1
        try:
            check("untrusted state folder (%s) gives no note" % kind, fire("pytest -q", "untrusted")[1] == "")
        finally:
            if kind == "foreign":
                os.getuid = real_uid
        check("nothing written through the %s" % kind, not os.listdir(target))
    os.environ["HOME"] = saved

    # one family list: the hook and script-repeats read the same file and the hook holds no pattern
    module = repeats()
    check("script-repeats reads lib/script_families.json",
          os.path.realpath(module.FAMILIES_FILE) == os.path.realpath(os.path.join(HERE, "..", "lib", "script_families.json")))
    check("the hook loads families through script-repeats", [f["name"] for f in module.load_families()] == list(usages()))
    with open(os.path.realpath(__file__)) as f:
        source = f.read()
    check("no second copy of a family pattern", "inline-python-replace" not in source.split("def self_test")[0]
          and "reviewThreads" not in source.split("def self_test")[0])

    # speed: a 20000 character command, families loaded fresh each time
    filler = "echo 'step' && ls -la src/app/components && git diff --stat | head -20\n"
    big = ("cat > notes.md <<'EOF'\n" + filler * 100 + "EOF\n" + "for x in 1 2 3; do grep -c foo f; done\n" * 20)
    big = (big + filler * 300)[:20000]
    best = 1.0
    for _ in range(5):
        _REPEATS = None
        started = time.perf_counter()
        candidates(big)
        best = min(best, time.perf_counter() - started)
    print("20000 character command: %.1f ms (limit 50 ms, best of 5)" % (best * 1000))
    check("under 50ms on a 20000 character command", best < 0.05, "%.1f ms" % (best * 1000))
    _REPEATS = None

    p = subprocess.run([sys.executable, "-I", os.path.realpath(__file__)], input=json.dumps(
        {"tool_input": {"command": "pytest -q"}, "session_id": "cli-1"}), capture_output=True, text=True)
    check("runs under python3 -I", p.returncode == 0 and "test-phase" in p.stdout and not p.stderr, p.stderr)
    check("no bytecode written next to bin/script-repeats",
          not [f for f in os.listdir(os.path.dirname(os.path.realpath(REPEATS))) if f.endswith(".pyc")])

    print("self-test: ok" if failures == 0 else "self-test: %d failed" % failures)
    return 1 if failures else 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        sys.exit(self_test())
    hookkit.run(main)

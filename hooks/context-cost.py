#!/usr/bin/env python3
"""Tell a coordinator while it happens when its context or its own lookups are costing the most.

PostToolUse hook on every tool. For the main session only, never a subagent, it adds a note:
  - once context passes LEAN_CONTEXT_WARN tokens (default 100000), and again at each further
    50000: the size, what a turn costs at list price, and to end the batch and resume from brain
  - once per session after LEAN_LOOKUP_WARN read-only lookups (default 10) by an Opus
    coordinator: send them to lean:search on the fast model
It only ever notes, never blocks. It reads the tail of the transcript, not the whole file, and
keeps a small counter file per session in a context-cost folder next to the lean-stats database
(~/.lean/context-cost, 0700). The counters stay when a session is resumed, so a resumed session
carries its count and the steps it was already told about.

  LEAN_CONTEXT_WARN=off   turn off the context note (a number sets the threshold)
  LEAN_LOOKUP_WARN=off    turn off the lookup note (a number sets the count)
  context-cost.py --self-test
"""
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time

try:
    import fcntl
except ImportError:  # no flock on this platform: the counter can lose an update, nothing else changes
    fcntl = None

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "lib"))
import hookkit  # noqa: E402
import leanstats  # noqa: E402

STEP = 50000
CONTEXT_DEFAULT = 100000
LOOKUP_DEFAULT = 10


def setting(name, default):
    """The number in an environment variable, default when unset or odd, None when it is off or 0."""
    value = (os.environ.get(name) or "").strip().lower()
    if not value:
        return default
    if value == "off":
        return None
    try:
        number = int(value)
    except ValueError:
        return default
    return number if number > 0 else None


SESSION_ID = re.compile(r"[A-Za-z0-9-]{1,100}")


def state_dir():
    """The counter folder, or None when it is not safe to use: a link, or not ours."""
    path = os.path.join(os.path.dirname(os.path.abspath(leanstats.db_path())), "context-cost")
    try:
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        try:
            os.mkdir(path, 0o700)
        except FileExistsError:
            pass
        info = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISDIR(info.st_mode):
        return None
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        return None
    if hasattr(os, "getuid") and info.st_mode & 0o077:
        try:
            os.chmod(path, 0o700)
        except OSError:
            return None
    return path


def state_path(sid, suffix=".json"):
    if not SESSION_ID.fullmatch(sid):
        raise ValueError("bad session id")
    folder = state_dir()
    if folder is None:
        raise OSError("no safe state folder")
    return os.path.join(folder, sid + suffix)


def load(sid):
    try:
        fd = os.open(state_path(sid), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd) as fh:
            state = json.load(fh)
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


KEEP_DAYS = 14


def prune(folder, sid):
    """Remove the counter and lock files of sessions untouched for KEEP_DAYS, never the current one."""
    cutoff = time.time() - KEEP_DAYS * 86400
    try:
        names = os.listdir(folder)
    except OSError:
        return
    for name in names:
        stem, dot, ext = name.rpartition(".")
        if not dot or ext not in ("json", "lock") or stem == sid:
            continue
        try:
            if os.lstat(os.path.join(folder, name)).st_mtime < cutoff:
                os.unlink(os.path.join(folder, name))
        except OSError:
            pass


def save(sid, state):
    path = state_path(sid)
    prune(os.path.dirname(path), sid)
    fd, temp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(state, fh)
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


class Locked:
    """Holds the session's lock file for the whole load, change and save, so parallel hooks do not lose updates."""

    def __init__(self, sid):
        self.path = state_path(sid, ".lock")
        self.fd = None

    def __enter__(self):
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        if fcntl:
            fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        os.close(self.fd)


def is_subagent(event, path):
    return bool(event.get("agent_id")) or f"{os.sep}subagents{os.sep}" in path


def context_note(last, state, limit):
    """The context note when this turn passes a new step, else None. Updates state."""
    size = leanstats.prompt_size(last)
    if limit is None or size < limit:
        state["step"] = -1
        return None
    step = (size - limit) // STEP
    if step <= state.get("step", -1):
        return None
    state["step"] = step
    return (f"Context is {size // 1000}k tokens. A turn at this size costs about "
            f"{leanstats.message_cost(last):.2f} USD at list price, and every later turn costs more. "
            "End the batch and resume from brain: file what is open with `" + os.path.normpath(os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "tools", "brain")) + " add`, then continue "
            "in a fresh session. LEAN_CONTEXT_WARN=off turns this note off.")


def lookup_note(event, last, state, limit):
    """Count a read-only lookup by an Opus coordinator and return the note once the count is reached."""
    if limit is None or "opus" not in last["model"]:
        return None
    if leanstats.tool_kind(event.get("tool_name"), event.get("tool_input")) != "lookup":
        return None
    state["lookups"] = state.get("lookups", 0) + 1
    if state["lookups"] < limit or state.get("lookup_noted"):
        return None
    state["lookup_noted"] = True
    return (f"This session has run {state['lookups']} read-only lookups (grep, cat, git log, gh view, "
            "Read, Grep, Glob) on Opus. Send them to a lean:search agent on the fast model "
            "instead. LEAN_LOOKUP_WARN=off turns this note off.")


def main():
    event = hookkit.read_event()
    sid = str(event.get("session_id") or "")
    path = event.get("transcript_path")
    if not SESSION_ID.fullmatch(sid) or not isinstance(path, str) or is_subagent(event, path):
        return 0
    limit, lookups = setting("LEAN_CONTEXT_WARN", CONTEXT_DEFAULT), setting("LEAN_LOOKUP_WARN", LOOKUP_DEFAULT)
    if limit is None and lookups is None:
        return 0
    last = leanstats.last_usage(path)
    if not last:
        return 0
    try:
        with Locked(sid):
            state = load(sid)
            notes = [n for n in (context_note(last, state, limit), lookup_note(event, last, state, lookups)) if n]
            save(sid, state)
    except (OSError, ValueError):
        return 0
    if notes:
        hookkit.note("PostToolUse", " ".join(notes))
    return 0


def self_test():
    import contextlib
    import io
    import time

    failures = []

    def check(name, got, want):
        if got != want:
            failures.append(f"{name}: got {got!r}, want {want!r}")

    with tempfile.TemporaryDirectory() as tmp:
        names = ("LEAN_CONTEXT_WARN", "LEAN_LOOKUP_WARN", "LEAN_DB", "LEAN_STATS", "HOME")
        saved = {k: os.environ.get(k) for k in names}
        for k in names[:4]:
            os.environ.pop(k, None)
        os.environ["HOME"] = os.path.join(tmp, "home")
        os.environ["LEAN_DB"] = os.path.join(tmp, "lean.db")
        try:
            transcript = os.path.join(tmp, "s.jsonl")

            def turn(size, model="claude-opus-5-5"):
                usage = {"input_tokens": 1, "cache_read_input_tokens": size - 1, "output_tokens": 10}
                return json.dumps({"type": "assistant", "message": {"id": "m%d" % size, "model": model, "usage": usage}}) + "\n"

            def fire(size, name="Edit", tool_input=None, sid="sess-1", extra=None, path=None, model="claude-opus-5-5"):
                with open(path or transcript, "a") as fh:
                    fh.write(turn(size, model))
                event = {"session_id": sid, "transcript_path": path or transcript, "tool_name": name,
                         "tool_input": tool_input or {}, "hook_event_name": "PostToolUse"}
                event.update(extra or {})
                old = sys.stdin
                sys.stdin = io.StringIO(json.dumps(event))
                out, err = io.StringIO(), io.StringIO()
                try:
                    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                        code = main()
                finally:
                    sys.stdin = old
                text = ""
                if out.getvalue():
                    text = json.loads(out.getvalue())["hookSpecificOutput"]["additionalContext"]
                return code, text, err.getvalue()

            check("below the threshold is quiet", fire(60000), (0, "", ""))
            check("just under it is quiet", fire(99999), (0, "", ""))
            code, text, err = fire(100000)
            check("at the threshold it notes", (code, err, "100k tokens" in text, "brain" in text, "USD at list price" in text),
                  (0, "", True, True, True))
            cost = leanstats.message_cost(leanstats.usage_of({}, {"model": "claude-opus-5-5", "usage": {
                "input_tokens": 1, "cache_read_input_tokens": 99999, "output_tokens": 10}}))
            check("it names the turn's list price", "%.2f USD" % cost in text, True)
            check("the same step is not noted twice", fire(120000)[1], "")
            check("nor the end of the step", fire(149999)[1], "")
            check("the next 50k step notes again", "150k tokens" in fire(150000)[1], True)
            check("and once only", fire(160000)[1], "")
            check("a further step notes", "200k tokens" in fire(200000)[1], True)
            check("a compaction below the threshold is quiet", fire(30000)[1], "")
            check("and the threshold notes again after it", "110k tokens" in fire(110000)[1], True)

            os.environ["LEAN_CONTEXT_WARN"] = "50000"
            check("the threshold is a setting", "60k tokens" in fire(60000, sid="sess-2")[1], True)
            os.environ["LEAN_CONTEXT_WARN"] = "off"
            check("and can be turned off", fire(300000, sid="sess-3")[1], "")
            os.environ["LEAN_CONTEXT_WARN"] = "junk"
            check("a junk value is the default", fire(50000, sid="sess-4")[1], "")
            del os.environ["LEAN_CONTEXT_WARN"]

            sub = os.path.join(tmp, "s", "subagents")
            os.makedirs(sub)
            sub_path = os.path.join(sub, "agent-a.jsonl")
            with open(sub_path, "w") as fh:
                fh.write(turn(300000))
            for label, kwargs in (("a subagent by its path", {"sid": "sess-5", "path": sub_path}),
                                  ("a subagent by its id", {"sid": "sess-6", "extra": {"agent_id": "a1"}})):
                code, text, err = fire(300000, **kwargs)
                check(label + " never gets the coordinator warning", (code, text, err), (0, "", ""))
            check("and its counter is not touched", os.path.exists(state_path("sess-5")), False)

            lookup = {"name": "Bash", "tool_input": {"command": "git log --oneline"}}
            for i in range(9):
                check("lookup %d is quiet" % (i + 1), fire(1000 + i, sid="sess-7", **lookup)[1], "")
            code, text, err = fire(2000, sid="sess-7", **lookup)
            check("the tenth lookup notes", (code, "10 read-only lookups" in text, "lean:search" in text, "fast model" in text),
                  (0, True, True, True))
            check("once", fire(2001, sid="sess-7", **lookup)[1], "")
            check("counting goes on after the note", load("sess-7")["lookups"], 11)
            fire(2002, sid="sess-8", name="Edit")
            fire(2003, sid="sess-8", name="Bash", tool_input={"command": "git push"})
            check("edits and writes do not count", load("sess-8").get("lookups", 0), 0)
            for i in range(12):
                fire(1000, sid="sess-9", name="Read", model="claude-sonnet-5-5")
            check("a Sonnet coordinator is not counted", load("sess-9").get("lookups", 0), 0)
            os.environ["LEAN_LOOKUP_WARN"] = "2"
            fire(1000, sid="sess-10", name="Grep")
            check("the count is a setting", "2 read-only lookups" in fire(1000, sid="sess-10", name="Glob")[1], True)
            os.environ["LEAN_LOOKUP_WARN"] = "off"
            check("and can be turned off", fire(1000, sid="sess-11", name="Grep")[1], "")
            del os.environ["LEAN_LOOKUP_WARN"]

            os.environ["LEAN_LOOKUP_WARN"] = "1"
            code, text, err = fire(120000, sid="sess-12", name="Read")
            check("both notes can arrive together", ("120k tokens" in text, "lean:search" in text), (True, True))
            del os.environ["LEAN_LOOKUP_WARN"]

            for value, want in (("0", None), ("-5", None), ("1", 1), (" 7 ", 7), ("off", None), ("", 9), ("3.5", 9)):
                os.environ["LEAN_CONTEXT_WARN"] = value
                check("setting %r" % value, setting("LEAN_CONTEXT_WARN", 9), want)
            del os.environ["LEAN_CONTEXT_WARN"]
            os.environ["LEAN_CONTEXT_WARN"] = "0"
            check("a setting of 0 turns the context note off", fire(300000, sid="sess-16")[1], "")
            del os.environ["LEAN_CONTEXT_WARN"]

            folder = os.path.join(tmp, "context-cost")
            check("the counters sit next to the database, private", (os.path.dirname(state_path("sess-7")) == folder,
                                                                    stat.S_IMODE(os.lstat(folder).st_mode)), (True, 0o700))
            check("no temp file is left behind", [n for n in os.listdir(folder) if n.endswith(".tmp")], [])
            check("a counter file is not shared with the temp directory", os.path.exists(
                os.path.join(tempfile.gettempdir(), "lean-context-cost")), False)
            for bad in ("a/../b", "../x", "a.b", "", "x" * 101, "a b"):
                try:
                    state_path(bad)
                    check("session id %r is refused" % bad, "accepted", "refused")
                except ValueError:
                    pass

            here = os.path.join(tmp, "parallel")
            os.makedirs(here)
            log = os.path.join(here, "s.jsonl")
            with open(log, "w") as fh:
                fh.write(turn(1000))
            env = dict(os.environ, LEAN_DB=os.path.join(here, "lean.db"), HOME=here)
            event = json.dumps({"session_id": "par-1", "transcript_path": log, "tool_name": "Grep", "tool_input": {},
                                "hook_event_name": "PostToolUse"})
            procs = [subprocess.Popen([sys.executable, os.path.realpath(__file__)], stdin=subprocess.PIPE,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env) for _ in range(20)]
            outs = [p.communicate(event) for p in procs]
            check("parallel hooks all succeed", [p.returncode for p in procs], [0] * 20)
            noted = [o for o, _ in outs if "read-only lookups" in o]
            check("parallel hooks lose no count", json.load(open(os.path.join(here, "context-cost", "par-1.json")))["lookups"], 20)
            check("and exactly one of them gives the lookup note", len(noted), 1)

            os.environ["LEAN_DB"] = env["LEAN_DB"]
            try:
                with Locked("par-2"):
                    held = subprocess.Popen([sys.executable, os.path.realpath(__file__)], stdin=subprocess.PIPE,
                                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
                    held.stdin.write(event.replace("par-1", "par-2"))
                    held.stdin.close()
                    try:
                        held.wait(timeout=0.7)
                        check("a hook waits for the session's lock", "finished", "waiting")
                    except subprocess.TimeoutExpired:
                        pass
                check("and goes on when it is released", held.wait(timeout=10), 0)
                held.stdout.close()
                held.stderr.close()
                check("having counted once", json.load(open(os.path.join(here, "context-cost", "par-2.json")))["lookups"], 1)
            finally:
                os.environ["LEAN_DB"] = os.path.join(tmp, "lean.db")

            linked = os.path.join(tmp, "linked")
            elsewhere = os.path.join(tmp, "elsewhere")
            os.makedirs(linked)
            os.makedirs(elsewhere)
            os.symlink(elsewhere, os.path.join(linked, "context-cost"))
            os.environ["LEAN_DB"] = os.path.join(linked, "lean.db")
            check("a state folder that is a link is refused", (fire(300000, sid="sess-17")[:1], os.listdir(elsewhere)),
                  ((0,), []))
            os.environ["LEAN_DB"] = os.path.join(tmp, "lean.db")
            real_uid = os.getuid
            os.getuid = lambda: real_uid() + 1
            try:
                check("a state folder that is not ours is refused", state_dir(), None)
            finally:
                os.getuid = real_uid
            old = time.time() - 15 * 86400
            for name in ("gone-1.json", "gone-1.lock", "fresh-1.json", "sess-18.lock"):
                with open(os.path.join(folder, name), "w") as fh:
                    fh.write("{}")
            for name in ("gone-1.json", "gone-1.lock", "sess-18.lock"):
                os.utime(os.path.join(folder, name), (old, old))
            fire(1000, sid="sess-18")
            check("counters untouched for 14 days are removed", sorted(n for n in os.listdir(folder) if n.startswith(("gone", "fresh"))),
                  ["fresh-1.json"])
            check("the current session's old lock is kept", os.path.exists(os.path.join(folder, "sess-18.lock")), True)

            os.mkdir(os.path.join(tmp, "rel"))
            cwd_before = os.getcwd()
            os.chdir(os.path.join(tmp, "rel"))
            os.environ["LEAN_DB"] = "rel.db"
            try:
                check("a relative LEAN_DB still keeps state", "300k tokens" in fire(300000, sid="sess-19")[1], True)
                check("beside the database", os.path.exists(os.path.join(tmp, "rel", "context-cost", "sess-19.json")), True)
            finally:
                os.chdir(cwd_before)
                os.environ["LEAN_DB"] = os.path.join(tmp, "lean.db")

            link = os.path.join(folder, "sess-20.json")
            os.symlink(os.path.join(elsewhere, "x.json"), link)
            with open(os.path.join(elsewhere, "x.json"), "w") as fh:
                fh.write('{"lookups": 99}')
            check("a state file that is a link is not read", load("sess-20"), {})
            os.unlink(link)

            real_getuid = os.getuid
            del os.getuid
            try:
                check("without getuid the owner check is skipped", state_dir(), folder)
                check("and the hook still notes", "300k tokens" in fire(300000, sid="sess-21")[1], True)
            finally:
                os.getuid = real_getuid
            os.chmod(folder, 0o755)
            check("a loose folder is made private again", (state_dir() == folder, stat.S_IMODE(os.lstat(folder).st_mode)),
                  (True, 0o700))

            for label, event in (("bad json", "not json"), ("a bad session id", {"session_id": "a/../b", "transcript_path": transcript}),
                                 ("no transcript", {"session_id": "sess-13"}),
                                 ("a missing transcript", {"session_id": "sess-14", "transcript_path": os.path.join(tmp, "no.jsonl")})):
                old = sys.stdin
                sys.stdin = io.StringIO(event if isinstance(event, str) else json.dumps(event))
                out = io.StringIO()
                try:
                    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                        code = main()
                finally:
                    sys.stdin = old
                check(label + " is quiet", (code, out.getvalue()), (0, ""))

            big = os.path.join(tmp, "big.jsonl")
            with open(big, "w") as fh:
                filler = json.dumps({"type": "user", "message": {"role": "user", "content": "x" * 1000000}}) + "\n"
                for _ in range(50):
                    fh.write(filler)
                fh.write(turn(150000))
            began = time.monotonic()
            code, text, err = fire(170000, sid="sess-15", path=big)
            took = time.monotonic() - began
            check("a 50 MB transcript is read from the tail", (code, "170k" in text or "150k" in text, took < 1.0),
                  (0, True, True))
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
    print("self-test: ok" if not failures else "self-test failed:\n  " + "\n  ".join(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        sys.exit(self_test())
    hookkit.run(main)

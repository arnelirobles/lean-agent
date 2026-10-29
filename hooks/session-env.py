#!/usr/bin/env python3
"""Tie brain items to the Claude session that filed them, so an answer can go back to that session.

SessionStart: writes LEAN_SESSION_ID and LEAN_SESSION_CWD to $CLAUDE_ENV_FILE, so `brain add` in this
session stores them, and records the session as open in brain. SessionEnd: records it as ended, and
starts `lean-stats collect --transcript` for that session in the background, so the numbers collect
themselves. It also appends the tools folder to PATH there, once. The extension resumes only an ended session, so it never starts a second copy of a live one.
UserPromptSubmit: hands the session the answers to its questions that it has not had yet.
Never fails the session. LEAN_SESSION_ENV=off turns off the brain parts, not the PATH line; LEAN_STATS=off stops the collect.

  session-env.py --self-test
"""
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "lib"))
import hookkit  # noqa: E402

BRAIN = os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "tools", "brain")
LEAN_STATS = os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "tools", "lean-stats")


def brain(*args):
    try:
        r = subprocess.run([sys.executable, BRAIN, *args], capture_output=True, text=True, timeout=5)
    except (subprocess.SubprocessError, OSError):  # a locked database makes brain wait past the timeout
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


PATH_LINE = re.compile(r'export PATH=(?:"\$PATH":|"\$\{PATH:\+\$PATH:\}")(.+)')
SESSION_LINE = re.compile(r"export LEAN_SESSION_(?:ID|CWD)=")


def path_line():
    """The PATH line for the current tools folder. An empty PATH gets no leading colon, which would put the
    current folder on PATH."""
    return f'export PATH="${{PATH:+$PATH:}}"{shlex.quote(hookkit.TOOLS)}'


def ours(line):
    """True for a PATH line this hook wrote: for this tools folder, or for another lean-agent version's in the
    plugin cache (<...>/plugins/cache/<marketplace>/lean-agent/<version>/tools). Any other folder, a dev
    checkout included, belongs to someone else and stays."""
    m = PATH_LINE.fullmatch(line)
    if not m:
        return False
    try:
        parts = shlex.split(m.group(1))
    except ValueError:
        return False
    if len(parts) != 1:
        return False
    folder = os.path.normpath(parts[0])
    if folder == hookkit.TOOLS:
        return True
    p = folder.split(os.sep)
    return len(p) >= 7 and p[-1] == "tools" and p[-3] == "lean-agent" and p[-5] == "cache" and p[-6] == "plugins"


def _locked(env_file):
    """Open env_file (creating it) and hold an exclusive flock on it, so two copies of this hook do not
    lose each other's lines. Returns the fd, or None where flock does not exist. Retries when the file was
    replaced while we waited, since the lock is on the old inode then."""
    try:
        import fcntl
    except ImportError:
        return None
    for _ in range(10):
        fd = os.open(env_file, os.O_RDONLY | os.O_CREAT, 0o666)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            if os.fstat(fd).st_ino == os.stat(env_file).st_ino:
                return fd
        except OSError:
            pass
        os.close(fd)
    raise OSError("env file keeps changing")


def _replace(env_file, text, mode):
    """Write text to a temp file beside env_file and rename it over, so a crash never leaves half a file."""
    fd, tmp = tempfile.mkstemp(prefix=".lean-env-", suffix=".tmp", dir=os.path.dirname(os.path.abspath(env_file)))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, env_file)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_env(env_file, sid=None, cwd="", session=True):
    """Write our lines to the env file and return True, or False when it cannot be written. Our earlier lines
    go first (on resume and compact SessionStart runs again on the same file), so each appears once and
    the PATH line comes last: a PowerShell session reads the file only if every line is a plain assignment,
    and the newest tools folder must win over an old version's. The PATH line appends, so a tool named like a
    system command does not shadow it. With no sid, or session False (LEAN_SESSION_ENV=off), only the PATH
    line is written and any LEAN_SESSION lines stay. The read and the write happen under a lock, the write
    goes through a temp file and a rename, and the file keeps its mode and its line endings.
    The marker, for hooks that never see the env file, is written only when the PATH line landed."""
    new = []
    session = bool(sid and session)
    if session:
        new.append(f"export LEAN_SESSION_ID={shlex.quote(sid)}")
        if cwd:
            new.append(f"export LEAN_SESSION_CWD={shlex.quote(cwd)}")
    new.append(path_line())
    existed = os.path.exists(env_file)
    if existed and not os.access(env_file, os.W_OK):
        return False  # a read-only file is someone's choice, a rename would get around it
    try:
        lock = _locked(env_file)
    except OSError:
        return False
    try:
        try:
            with open(env_file, encoding="utf-8", newline="") as f:
                content = f.read()
            st_mode = os.stat(env_file).st_mode & 0o7777
        except FileNotFoundError:  # no flock here, so nothing created it
            umask = os.umask(0)
            os.umask(umask)
            content, st_mode = "", 0o666 & ~umask
        except (OSError, UnicodeDecodeError):
            return False  # never overwrite a file we could not read
        nl = "\r\n" if "\r\n" in content else "\n"
        lines = content.split(nl)
        if lines[-1] == "":
            lines.pop()
        kept = [ln for ln in lines if not ours(ln) and not (session and SESSION_LINE.match(ln))]
        text = "".join(ln + nl for ln in kept + new)
        if text != content:
            try:
                _replace(env_file, text, st_mode)
            except OSError:
                return False
    finally:
        if lock is not None:
            os.close(lock)
    marker = hookkit.marker_file(sid)
    if marker:
        try:
            os.makedirs(os.path.dirname(marker), mode=0o700, exist_ok=True)
            with open(marker, "w", encoding="utf-8") as f:
                f.write(hookkit.TOOLS)
        except OSError:
            pass
    return True


def collect_session(transcript):
    """Start `lean-stats collect --transcript` detached and return the process, or None. Never waits, never raises."""
    if os.environ.get("LEAN_STATS") == "off" or not isinstance(transcript, str) or not transcript.endswith(".jsonl"):
        return None
    try:
        return subprocess.Popen([sys.executable, LEAN_STATS, "collect", "--transcript", transcript],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                start_new_session=True)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


STARTED = []  # the collects this process started, so a test can wait on them


def main():
    event = hookkit.read_event()
    name = event.get("hook_event_name")
    sid, cwd = hookkit.session_id(event.get("session_id")), str(event.get("cwd") or "")
    sid_ok = sid is not None
    off = (os.environ.get("LEAN_SESSION_ENV") or "").lower() == "off"
    if name == "SessionEnd":
        proc = collect_session(event.get("transcript_path"))
        if proc:
            STARTED.append(proc)
        marker = hookkit.marker_file(sid)
        if marker and os.path.exists(marker):
            os.unlink(marker)
    if name == "SessionStart" and os.environ.get("CLAUDE_ENV_FILE"):
        write_env(os.environ["CLAUDE_ENV_FILE"], sid, cwd, session=not off)
    if off or not sid_ok:
        return 0
    if name == "SessionStart":
        brain("session", "start", sid, "--cwd", cwd)
    elif name == "SessionEnd":
        brain("session", "end", sid)
    elif name == "UserPromptSubmit":
        answers = brain("answers", "--session", sid)
        if answers:
            hookkit.note("UserPromptSubmit", "Answers to questions you filed in brain (already closed as answered):\n"
                         + answers)
    return 0


def self_test():
    fails = []
    me = os.path.realpath(__file__)
    with tempfile.TemporaryDirectory() as d:
        env_file = os.path.join(d, "env")
        home = os.path.join(d, "home")
        os.mkdir(home)
        env = dict(os.environ, LEAN_BRAIN_DB=os.path.join(d, "brain.db"), CLAUDE_ENV_FILE=env_file, HOME=home,
                   LEAN_DB=os.path.join(d, "hooks.db"))
        env.pop("LEAN_SESSION_ENV", None)
        env.pop("LEAN_SESSION_ID", None)
        env.pop("LEAN_SESSION_CWD", None)
        cwd = os.path.join(d, "it's a dir")
        os.mkdir(cwd)

        def hook(name, sid="abc-123", raw=None):
            event = raw if raw is not None else '{"hook_event_name": "%s", "session_id": "%s", "cwd": %s}' % (
                name, sid, json_str(cwd))
            r = subprocess.run([sys.executable, me], input=event, env=env, capture_output=True, text=True)
            return r.returncode, r.stdout, r.stderr

        def brain_out(*args):
            return subprocess.run([sys.executable, BRAIN, *args], env=env, capture_output=True, text=True).stdout.strip()

        rc, out, err = hook("SessionStart")
        if rc != 0 or out:
            fails.append(f"start must be quiet: {rc} {out!r} {err!r}")
        written = open(env_file).read() if os.path.exists(env_file) else ""
        sourced = subprocess.run(["bash", "-c", f". {shlex.quote(env_file)}; printf '%s|%s' \"$LEAN_SESSION_ID\" "
                                  "\"$LEAN_SESSION_CWD\""], capture_output=True, text=True).stdout
        if sourced != f"abc-123|{cwd}":
            fails.append(f"env file must export the id and a quoted cwd: {written!r} gives {sourced!r}")
        path_lines = [ln for ln in written.splitlines() if ln.startswith("export PATH=")]
        want = f'export PATH="${{PATH:+$PATH:}}"{shlex.quote(hookkit.TOOLS)}'
        if written.splitlines()[-1] != want:
            fails.append(f"the PATH line must come after the LEAN_SESSION lines: {written!r}")
        if path_lines != [want]:
            fails.append(f"start must add the tools folder to PATH once: {path_lines!r}")
        marker = os.path.join(home, ".local", "share", "lean", "tools-on-path", "abc-123")
        if not os.path.exists(marker) or open(marker).read() != hookkit.TOOLS:
            fails.append("start must leave a marker naming the tools folder for the hooks that never see the env file")
        shown = subprocess.run(["bash", "-c", f'PATH=/usr/bin:/bin; . {shlex.quote(env_file)}; command -v brain'],
                               capture_output=True, text=True).stdout.strip()
        if shown != os.path.join(hookkit.TOOLS, "brain"):
            fails.append(f"a shell sourcing the env file must find brain: {shown!r}")
        hook("SessionStart")
        hook("SessionStart", raw=json_str({"hook_event_name": "SessionStart", "session_id": "abc-123", "cwd": cwd,
                                           "source": "compact"}))
        again = open(env_file).read().splitlines()
        if again != [f"export LEAN_SESSION_ID=abc-123", f"export LEAN_SESSION_CWD={shlex.quote(cwd)}", want]:
            fails.append(f"a repeat SessionStart must leave each of our lines once, PATH last: {again!r}")
        empty = subprocess.run(["bash", "-c", f'PATH=; . {shlex.quote(env_file)}; printf %s "$PATH"'],
                               capture_output=True, text=True).stdout
        if empty != hookkit.TOOLS:
            fails.append(f"an empty PATH must not gain an empty element (the current folder): {empty!r}")
        glued = os.path.join(d, "glued-env")
        with open(glued, "w") as fh:
            fh.write("export A=1")
        env["CLAUDE_ENV_FILE"] = glued
        hook("SessionStart", sid="glue-1")
        got = subprocess.run(["bash", "-c", f'. {shlex.quote(glued)}; printf "%s|%s" "$A" "$LEAN_SESSION_ID"'],
                             capture_output=True, text=True).stdout
        if got != "1|glue-1":
            fails.append(f"a file with no final newline must keep its last line apart from ours: {got!r}")
        cache = os.path.join(d, "plugins", "cache", "mkt", "lean-agent")
        old_tools = os.path.join(cache, "1.16.0", "tools")
        os.makedirs(old_tools)
        with open(os.path.join(old_tools, "brain"), "w") as fh:
            fh.write("#!/bin/sh\necho old\n")
        os.chmod(os.path.join(old_tools, "brain"), 0o755)
        upgraded = os.path.join(d, "upgraded-env")
        with open(upgraded, "w") as fh:
            fh.write(f'export LEAN_SESSION_ID=up-1\nexport PATH="$PATH":{shlex.quote(old_tools)}\n')
        env["CLAUDE_ENV_FILE"] = upgraded
        hook("SessionStart", sid="up-1")
        wins = subprocess.run(["bash", "-c", f'PATH=/usr/bin:/bin; . {shlex.quote(upgraded)}; command -v brain'],
                              capture_output=True, text=True).stdout.strip()
        if wins != os.path.join(hookkit.TOOLS, "brain"):
            fails.append(f"after an upgrade bare brain must run the current version, not {wins!r}: "
                         f"{open(upgraded).read()!r}")
        others = [os.path.join(d, "repos", "lean-agent", "tools"), os.path.join(d, "lean-agent", "foo", "tools"),
                  os.path.join(d, "dev", "tools"), os.path.join(d, "plugins", "cache", "lean-agent", "tools")]
        os.makedirs(os.path.join(d, "dev", "hooks"))
        open(os.path.join(d, "dev", "hooks", "session-env.py"), "w").close()
        mixed = os.path.join(d, "mixed-env")
        with open(mixed, "w") as fh:
            fh.write("".join(f'export PATH="$PATH":{shlex.quote(o)}\n' for o in others + [old_tools]))
        write_env(mixed)
        if open(mixed).read() != "".join(f'export PATH="$PATH":{shlex.quote(o)}\n' for o in others) + want + "\n":
            fails.append(f"only the old plugin-cache tools line may go; a dev checkout, an arbitrary tools folder and "
                         f"one with hooks/session-env.py beside it stay: {open(mixed).read()!r}")
        atomic = os.path.join(d, "atomic-env")
        with open(atomic, "w") as fh:
            fh.write("export FOREIGN=1\r\nexport B=2\r\n")
        os.chmod(atomic, 0o640)
        inode = os.stat(atomic).st_ino
        write_env(atomic)
        if os.stat(atomic).st_ino == inode:
            fails.append("the env file must be written to a temp file and renamed over, never truncated in place")
        if open(atomic, newline="").read() != f"export FOREIGN=1\r\nexport B=2\r\n{want}\r\n":
            fails.append(f"foreign lines must survive and CRLF must stay CRLF: {open(atomic, newline='').read()!r}")
        if os.stat(atomic).st_mode & 0o777 != 0o640:
            fails.append(f"the env file must keep its mode: {oct(os.stat(atomic).st_mode & 0o777)}")
        if [n for n in os.listdir(d) if n.endswith(".tmp")]:
            fails.append("no temp file may be left beside the env file")
        import fcntl
        raced = os.path.join(d, "raced-env")
        with open(raced, "w") as fh:
            fh.write("export A=1\n")
        env["CLAUDE_ENV_FILE"] = raced
        held = os.open(raced, os.O_RDONLY)
        fcntl.flock(held, fcntl.LOCK_EX)
        child = subprocess.Popen([sys.executable, me], stdin=subprocess.PIPE, env=env, text=True,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        child.stdin.write('{"hook_event_name": "SessionStart", "session_id": "race-1", "cwd": "/w"}')
        child.stdin.close()
        time.sleep(0.5)
        waited = open(raced).read() == "export A=1\n"
        with open(raced, "a") as fh:
            fh.write("export OTHER=1\n")  # another hook appends while we hold the lock
        os.close(held)
        child.wait()
        if not waited or "export OTHER=1\n" not in open(raced).read():
            fails.append(f"a second writer must wait for the lock and keep the line added meanwhile: "
                         f"{open(raced).read()!r}")
        for label, bad in (("a missing folder", os.path.join(d, "no-such-dir", "env")),
                           ("a read-only file", os.path.join(d, "ro-env"))):
            if label == "a read-only file":
                with open(bad, "w") as fh:
                    fh.write("export A=1\n")
                os.chmod(bad, 0o444)
            env["CLAUDE_ENV_FILE"] = bad
            sid = "ro-1" if label == "a read-only file" else "nodir-1"
            rc, out, err = hook("SessionStart", sid=sid)
            if rc != 0 or err or brain_out("session", "status", sid) != f"open {cwd}":
                fails.append(f"{label} as the env file must not stop the session start: {rc} {err!r}")
            if os.path.exists(os.path.join(home, ".local", "share", "lean", "tools-on-path", sid)):
                fails.append(f"{label} as the env file must leave no marker, the tools are not on PATH")
        numeric = os.path.join(d, "numeric-env")
        env["CLAUDE_ENV_FILE"] = numeric
        hook("SessionStart", raw='{"hook_event_name": "SessionStart", "session_id": 12345, "cwd": "/w"}')
        wrote = os.path.exists(os.path.join(home, ".local", "share", "lean", "tools-on-path", "12345"))
        if wrote != (hookkit.marker_file(12345) is not None):
            fails.append("a numeric session id must get a marker only if the hooks read one for it")
        env["CLAUDE_ENV_FILE"] = env_file
        spaced = os.path.join(d, "a dir", "tools")
        spaced_file = os.path.join(d, "spaced-env")
        saved_tools = hookkit.TOOLS
        try:
            hookkit.TOOLS = spaced
            write_env(spaced_file)
        finally:
            hookkit.TOOLS = saved_tools
        os.makedirs(spaced)
        with open(os.path.join(spaced, "hello"), "w") as fh:
            fh.write("#!/bin/sh\necho hi\n")
        os.chmod(os.path.join(spaced, "hello"), 0o755)
        got = subprocess.run(["bash", "-c", f'PATH=/usr/bin:/bin; . {shlex.quote(spaced_file)}; hello'],
                             capture_output=True, text=True).stdout.strip()
        if got != "hi":
            fails.append(f"a tools folder with a space must be quoted: {got!r}")
        loose = os.path.join(d, "loose-env")
        with open(loose, "w") as fh:
            fh.write('export PATH=/other:"$PATH"')
        for _ in range(3):
            write_env(loose)
        text = open(loose).read()
        if text != f'export PATH=/other:"$PATH"\n{want}\n':
            fails.append(f"a file with no final newline must get one line of ours on its own line: {text!r}")
        if brain_out("session", "status", "abc-123") != f"open {cwd}":
            fails.append("start must record the session as open")
        hook("SessionEnd")
        if brain_out("session", "status", "abc-123") != f"ended {cwd}":
            fails.append("end must record the session as ended")

        subprocess.run([sys.executable, BRAIN, "add", "--project", "p", "--kind", "question", "--ref", "q",
                        "--title", "which port", "--session", "abc-123"], env=env, capture_output=True)
        subprocess.run([sys.executable, BRAIN, "close", "1", "--resolution", "answered", "--note", "8080"],
                       env=env, capture_output=True)
        rc, out, _ = hook("UserPromptSubmit")
        if rc != 0 or "#1 which port: 8080" not in out or "UserPromptSubmit" not in out:
            fails.append(f"a prompt must carry the new answer: {rc} {out!r}")
        if hook("UserPromptSubmit")[1]:
            fails.append("an answer is handed over once")

        before = open(env_file).read()
        if hook("SessionStart", sid="x; rm -rf ~")[0] != 0 or open(env_file).read() != before:
            fails.append("an odd session id must be ignored")
        hook("SessionEnd")
        if os.path.exists(marker):
            fails.append("SessionEnd must remove the marker")
        if hook("", raw="not json")[0] != 0:
            fails.append("a bad event must not fail")
        env["LEAN_SESSION_ENV"] = "off"
        off_file = os.path.join(d, "off-env")
        env["CLAUDE_ENV_FILE"] = off_file
        hook("SessionStart", sid="off-1")
        off_text = open(off_file).read() if os.path.exists(off_file) else ""
        if "off-1" in off_text:
            fails.append("LEAN_SESSION_ENV=off must turn it off")
        if off_text != want + "\n" or not os.path.exists(os.path.join(home, ".local", "share", "lean",
                                                                      "tools-on-path", "off-1")):
            fails.append(f"LEAN_SESSION_ENV=off must still put the tools on PATH, as mode.py says: {off_text!r}")
        del env["LEAN_SESSION_ENV"]
        odd_file = os.path.join(d, "odd-env")
        env["CLAUDE_ENV_FILE"] = odd_file
        hook("SessionStart", sid="x; rm -rf ~")
        odd_text = open(odd_file).read() if os.path.exists(odd_file) else ""
        if odd_text != want + "\n":
            fails.append(f"an odd session id must still get the PATH line and nothing else: {odd_text!r}")
        env["CLAUDE_ENV_FILE"] = env_file
        env["LEAN_SESSION_ENV"] = "off"
        began = time.monotonic()
        rc, out, err = hook("SessionEnd", raw=json_str({"hook_event_name": "SessionEnd", "session_id": "gone-1",
                                                        "transcript_path": os.path.join(d, "none", "x.jsonl")}))
        if rc != 0 or out or time.monotonic() - began > 5:
            fails.append(f"SessionEnd with a missing transcript must be quick and quiet: {rc} {out!r} {err!r}")

        import io
        import json
        import leanstats
        db = os.path.join(d, "lean.db")
        proj = os.path.join(d, "projects", "proj")
        os.makedirs(os.path.join(proj, "s1", "subagents"))
        usage = {"input_tokens": 7, "output_tokens": 1}

        def line(sid):
            return json.dumps({"type": "assistant", "sessionId": sid, "cwd": d, "timestamp": "2026-10-02T01:00:00.000Z",
                               "message": {"id": "m1", "model": "claude-sonnet-5-5", "usage": usage}}) + "\n"

        for path, sid in ((os.path.join(proj, "s1.jsonl"), "s1"), (os.path.join(proj, "s2.jsonl"), "s2"),
                          (os.path.join(proj, "s1", "subagents", "agent-a1.jsonl"), "s1")):
            with open(path, "w") as fh:
                fh.write(line(sid))
        keys = ("LEAN_DB", "LEAN_STATS", "HOME", "LEAN_BRAIN_DB")
        saved = {k: os.environ.get(k) for k in keys}
        os.environ.update(LEAN_DB=db, HOME=home, LEAN_BRAIN_DB=os.path.join(d, "brain.db"))
        os.environ.pop("LEAN_STATS", None)

        def send(event):
            old = sys.stdin
            sys.stdin = io.StringIO(json.dumps(event))
            try:
                return main()
            finally:
                sys.stdin = old

        def stored():
            conn = leanstats.connect(db)
            try:
                return sorted(os.path.basename(r[0]) for r in conn.execute("SELECT source_path FROM agent_runs"))
            finally:
                conn.close()

        try:
            proc = collect_session(os.path.join(proj, "s1.jsonl"))
            if proc is None or proc.wait() != 0:
                fails.append("SessionEnd must start a collect for the session's transcript")
            if stored() != ["agent-a1.jsonl", "s1.jsonl"]:
                fails.append(f"collect must import that session and its agents only: {stored()}")
            if send({"hook_event_name": "SessionEnd", "session_id": "s2", "cwd": d,
                     "transcript_path": os.path.join(proj, "s2.jsonl")}) != 0 or not STARTED:
                fails.append("a SessionEnd event through main must start a collect")
            else:
                STARTED[-1].wait()
                if stored() != ["agent-a1.jsonl", "s1.jsonl", "s2.jsonl"]:
                    fails.append(f"a SessionEnd event through main must import its transcript: {stored()}")
            started = len(STARTED)
            for odd in (None, 5, "notes.txt", "dir/x.json"):
                send({"hook_event_name": "SessionEnd", "session_id": "odd-1", "cwd": d, "transcript_path": odd})
            if len(STARTED) != started:
                fails.append("a SessionEnd with a transcript path that is not a .jsonl must start nothing")
            send({"hook_event_name": "SessionEnd", "session_id": "odd-2", "cwd": d})
            if len(STARTED) != started:
                fails.append("a SessionEnd with no transcript path must start nothing")
            os.environ["LEAN_STATS"] = "off"
            if collect_session(os.path.join(proj, "s2.jsonl")) is not None:
                fails.append("LEAN_STATS=off must stop the collect")
            send({"hook_event_name": "SessionEnd", "session_id": "off-2", "cwd": d,
                  "transcript_path": os.path.join(proj, "s2.jsonl")})
            if len(STARTED) != started:
                fails.append("LEAN_STATS=off must stop the collect through main too")
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        if os.path.exists(os.path.join(home, ".lean", "lean.db")):
            fails.append("a self-test must not touch the default database")
    print("self-test: ok" if not fails else "self-test failed:\n  " + "\n  ".join(fails))
    return 1 if fails else 0


def json_str(s):
    import json
    return json.dumps(s)


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        sys.exit(self_test())
    hookkit.run(main)

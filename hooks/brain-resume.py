#!/usr/bin/env python3
"""Hand the session its open brain items at startup and after a compaction.

SessionStart hook (matcher startup|compact). Runs `brain resume` for the project of the session's
directory and adds the digest as context: blockers, running agents and pending steps survive a
compaction that way. Silent and fast when brain has no database or nothing is open, and it never fails
the session. LEAN_BRAIN_RESUME=off turns it off.

  brain-resume.py --self-test
"""
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "lib"))
import hookkit  # noqa: E402

BRAIN = os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "bin", "brain")


def db_path():
    return os.environ.get("LEAN_BRAIN_DB") or os.path.expanduser("~/.local/share/lean/brain.db")


def digest(cwd):
    if (os.environ.get("LEAN_BRAIN_RESUME") or "").lower() == "off" or not os.path.exists(db_path()):
        return ""
    if not os.path.isdir(cwd or ""):
        cwd = os.getcwd()
    try:
        r = subprocess.run([sys.executable, BRAIN, "resume"], cwd=cwd, capture_output=True, text=True, timeout=5)
    except (subprocess.SubprocessError, OSError):  # a locked database makes brain wait past the timeout
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def main():
    event = hookkit.read_event()
    text = digest(str(event.get("cwd") or ""))
    if text:
        hookkit.note("SessionStart", "Open brain items for this project (full list: brain list --status open). "
                     "Do not work around a blocker.\n" + text)
    return 0


def self_test():
    fails = []
    me = os.path.realpath(__file__)
    with tempfile.TemporaryDirectory() as d:
        proj = os.path.join(d, "myproj")
        os.mkdir(proj)
        subprocess.run(["git", "init", "-q", proj], check=True)
        env = dict(os.environ, LEAN_BRAIN_DB=os.path.join(d, "none", "brain.db"))
        env.pop("LEAN_BRAIN_RESUME", None)

        def hook(event='{"cwd": "%s"}' % proj):
            r = subprocess.run([sys.executable, me], input=event, env=env, capture_output=True, text=True)
            return r.returncode, r.stdout, r.stderr

        if hook() != (0, "", ""):
            fails.append(f"no database must be silent: {hook()}")
        subprocess.run([sys.executable, BRAIN, "add", "--project", "myproj", "--kind", "blocker", "--ref", "b",
                        "--title", "waiting on the user"], env=env, capture_output=True)
        rc, out, err = hook()
        if rc != 0 or "waiting on the user" not in out or "SessionStart" not in out:
            fails.append(f"open item not in context: {rc} {out!r} {err!r}")
        subprocess.run([sys.executable, BRAIN, "close", "1", "--resolution", "fixed"], env=env, capture_output=True)
        if hook() != (0, "", ""):
            fails.append("nothing open must be silent")
        if hook("not json")[0] != 0:
            fails.append("a bad event must not fail")
        # A database locked by another writer: brain waits past the hook's timeout, the hook prints nothing.
        import sqlite3
        env["LEAN_BRAIN_DB"] = os.path.join(d, "none", "brain.db")
        locker = sqlite3.connect(env["LEAN_BRAIN_DB"], isolation_level=None)
        locker.execute("BEGIN EXCLUSIVE")
        if hook() != (0, "", ""):
            fails.append(f"a locked database must print nothing and exit 0: {hook()}")
        locker.execute("ROLLBACK")
        locker.close()
        env["LEAN_BRAIN_DB"] = d  # a directory, not a database: brain errors, the hook still exits 0
        if hook()[0] != 0:
            fails.append("a broken database must not fail the session")
    print("self-test: ok" if not fails else "self-test failed:\n  " + "\n  ".join(fails))
    return 1 if fails else 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        sys.exit(self_test())
    hookkit.run(main)

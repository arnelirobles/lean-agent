#!/usr/bin/env python3
"""Hand the session a digest of its open brain items at startup and after a compaction.

SessionStart hook (matcher startup|compact). Runs `brain digest` for the project of the session's
directory and adds the digest as context: blockers, running agents and pending steps survive a
compaction that way. Silent and fast when brain has no database or nothing is open, and it never fails
the session. LEAN_BRAIN_RESUME=off turns it off.

  brain-resume.py --self-test
"""
import os
import sqlite3
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
        r = subprocess.run([sys.executable, BRAIN, "digest", "--quiet"], cwd=cwd, capture_output=True, text=True, timeout=5)
    except (subprocess.SubprocessError, OSError):  # a locked database makes brain wait past the timeout
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def main():
    event = hookkit.read_event()
    text = digest(str(event.get("cwd") or ""))
    if text:
        hookkit.note("SessionStart", "Brain digest for this project (full list: brain list --project NAME --status open). "
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
        subprocess.run([sys.executable, BRAIN, "add", "--project", "MYPROJ", "--kind", "question", "--ref", "q",
                        "--title", "which port"], env=env, capture_output=True)
        subprocess.run([sys.executable, BRAIN, "add", "--project", "elsewhere", "--kind", "blocker", "--ref", "x",
                        "--title", "not for this folder"], env=env, capture_output=True)
        rc, out, err = hook()
        if "which port" not in out or "not for this folder" in out:
            fails.append(f"the digest covers this project only, whatever the case of its name: {out!r}")
        for i in range(300):
            subprocess.run([sys.executable, BRAIN, "add", "--project", "myproj", "--kind", "blocker",
                            "--ref", f"many{i}", "--title", "x" * 90], env=env, capture_output=True)
        out = hook()[1]
        if len(out.encode()) > 2600:
            fails.append(f"the digest must stay small with 300 blockers: {len(out.encode())} bytes")
        subprocess.run([sys.executable, BRAIN, "close", "1"], env=env, capture_output=True)
        subprocess.run([sys.executable, BRAIN, "close", "2"], env=env, capture_output=True)
        con = sqlite3.connect(env["LEAN_BRAIN_DB"], isolation_level=None)
        con.execute("UPDATE items SET status='closed' WHERE project IN ('myproj', 'MYPROJ')")
        con.close()
        if hook() != (0, "", ""):
            fails.append("nothing open must be silent")
        if hook("not json")[0] != 0:
            fails.append("a bad event must not fail")
        # A database locked by another writer: brain waits past the hook's timeout, the hook prints nothing.
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

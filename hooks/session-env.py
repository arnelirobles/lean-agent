#!/usr/bin/env python3
"""Tie brain items to the Claude session that filed them, so an answer can go back to that session.

SessionStart: writes LEAN_SESSION_ID and LEAN_SESSION_CWD to $CLAUDE_ENV_FILE, so `brain add` in this
session stores them, and records the session as open in brain. SessionEnd: records it as ended. The
extension resumes only an ended session, so it never starts a second copy of a live one.
UserPromptSubmit: hands the session the answers to its questions that it has not had yet.
Never fails the session. LEAN_SESSION_ENV=off turns it off.

  session-env.py --self-test
"""
import os
import re
import shlex
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "lib"))
import hookkit  # noqa: E402

BRAIN = os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "bin", "brain")


def brain(*args):
    try:
        r = subprocess.run([sys.executable, BRAIN, *args], capture_output=True, text=True, timeout=5)
    except (subprocess.SubprocessError, OSError):  # a locked database makes brain wait past the timeout
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def main():
    event = hookkit.read_event()
    if (os.environ.get("LEAN_SESSION_ENV") or "").lower() == "off":
        return 0
    sid, cwd = str(event.get("session_id") or ""), str(event.get("cwd") or "")
    if not re.fullmatch(r"[A-Za-z0-9-]{1,100}", sid):
        return 0
    name = event.get("hook_event_name")
    if name == "SessionStart":
        env_file = os.environ.get("CLAUDE_ENV_FILE")
        if env_file:
            with open(env_file, "a", encoding="utf-8") as f:
                f.write(f"export LEAN_SESSION_ID={shlex.quote(sid)}\n")
                if cwd:
                    f.write(f"export LEAN_SESSION_CWD={shlex.quote(cwd)}\n")
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
        env = dict(os.environ, LEAN_BRAIN_DB=os.path.join(d, "brain.db"), CLAUDE_ENV_FILE=env_file)
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
        if hook("", raw="not json")[0] != 0:
            fails.append("a bad event must not fail")
        env["LEAN_SESSION_ENV"] = "off"
        hook("SessionStart", sid="off-1")
        if "off-1" in open(env_file).read():
            fails.append("LEAN_SESSION_ENV=off must turn it off")
    print("self-test: ok" if not fails else "self-test failed:\n  " + "\n  ".join(fails))
    return 1 if fails else 0


def json_str(s):
    import json
    return json.dumps(s)


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        sys.exit(self_test())
    hookkit.run(main)

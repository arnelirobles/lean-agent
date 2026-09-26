#!/usr/bin/env python3
"""Log commits that look like something went wrong, for the retro to read.

PostToolUse hook on Bash, after a git commit. A lesson is worth most at the
moment it is learned, which is the moment nobody wants to stop and write it
down. When the new commit's subject matches fix, revert, flak(y), silent,
gate or regress, this appends one line to .lean/lessons.tsv at the root of the
repository and tells the agent the retro reads it. It never blocks.

File format, .lean/lessons.tsv: UTF-8, one commit per line, no header, three
tab-separated fields:

  date     YYYY-MM-DD, the day the commit was logged (local time)
  sha      the full commit hash; a hash already in the file is not added again
  subject  the commit subject, with tabs and newlines turned into spaces

Only a commit made in the last five minutes is logged, so a `git commit` that
did nothing never logs an older one.

  LEAN_SKIP_LESSON_LOG=1   turn it off
  lesson-log.py --self-test
"""
import datetime
import os
import re
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "lib"))
import hookkit  # noqa: E402

LESSON = re.compile(r"\b(fix|revert|flak|silent|gate|regress)", re.I)
FRESH_SECONDS = 300
LOG = os.path.join(".lean", "lessons.tsv")


def committed_repos(command, cwd):
    repos = []
    for seg in hookkit.segments(command, cwd):
        call = hookkit.git_call(seg)
        if call and call[0] == "commit" and "--dry-run" not in call[1] and call[2] not in repos:
            repos.append(call[2])
    return repos


def log_lesson(repo_cwd, now=None):
    """Append the latest commit if it reads like a lesson; return the logged line or None."""
    root = hookkit.git(repo_cwd, "rev-parse", "--show-toplevel")
    head = hookkit.git(repo_cwd, "log", "-1", "--format=%H%x09%ct%x09%s")
    if not root or not head:
        return None
    sha, stamp, subject = (head.split("\t", 2) + ["", ""])[:3]
    now = time.time() if now is None else now
    if not LESSON.search(subject) or now - int(stamp or 0) > FRESH_SECONDS:
        return None
    path = os.path.join(root, LOG)
    try:
        with open(path, encoding="utf-8") as fh:
            if any(line.split("\t")[1:2] == [sha] for line in fh):
                return None
    except OSError:
        pass
    os.makedirs(os.path.dirname(path), exist_ok=True)
    line = "\t".join([datetime.date.today().isoformat(), sha, re.sub(r"[\t\r\n]+", " ", subject)])
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    return line


def main():
    if hookkit.disabled("LEAN_SKIP_LESSON_LOG"):
        return 0
    event = hookkit.read_event()
    command = hookkit.tool_input(event).get("command") or ""
    if "commit" not in command:
        return 0
    logged = [line for repo in committed_repos(command, event.get("cwd") or os.getcwd())
              for line in [log_lesson(repo)] if line]
    if logged:
        hookkit.note("PostToolUse", (
            f"Logged to {LOG}: " + "; ".join(logged) + ". The lean retro reads that file. "
            "While it is fresh: what did you believe that turned out false, and what went green "
            "that should have gone red? If a check would have caught it, say which. "
            "LEAN_SKIP_LESSON_LOG=1 turns this off."))
    return 0


def self_test():
    import subprocess
    import tempfile

    failures = 0

    def check(name, got, want):
        nonlocal failures
        if bool(got) != want:
            print(f"FAIL {name}: got {got!r}")
            failures += 1

    with tempfile.TemporaryDirectory() as tmp:
        def commit(subject):
            subprocess.run(["git", "-C", tmp, "-c", "user.email=t@example.org", "-c", "user.name=t",
                            "commit", "-q", "--allow-empty", "-m", subject], check=True)

        subprocess.run(["git", "init", "-q", tmp], check=True)
        commit("add the export button")
        check("plain feature", log_lesson(tmp), False)
        commit("fix the retry that\tpassed silently")
        line = log_lesson(tmp)
        check("fix is logged", line, True)
        check("logged again", log_lesson(tmp), False)
        commit("Revert the cache change")
        check("old commit not logged", log_lesson(tmp, now=time.time() + 3600), False)
        check("revert is logged", log_lesson(tmp), True)
        with open(os.path.join(tmp, LOG), encoding="utf-8") as fh:
            rows = [r.rstrip("\n").split("\t") for r in fh]
        check("two rows", len(rows) == 2, True)
        check("three fields", all(len(r) == 3 for r in rows), True)
        check("tab in subject flattened", rows[0][2] == "fix the retry that passed silently", True)
        check("commit found in chain", committed_repos(f"cd {tmp} && git add -A && git commit -m x", "/"), True)
        check("log is not a commit", committed_repos("git log --grep fix", tmp), False)
        check("outside a repo", log_lesson(os.path.dirname(tmp)), False)
    print("self-test: ok" if failures == 0 else f"self-test: {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        sys.exit(self_test())
    hookkit.run(main)

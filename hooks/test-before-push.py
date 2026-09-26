#!/usr/bin/env python3
"""Warn before pushing source that changed after the last passing test run.

PreToolUse hook on Bash, for git push, gh pr create and gh pr merge. A suite
that passed before a change is not evidence about the change. The usual way
this goes wrong: the fix is verified, then changed after review, then pushed
after re-running only the fast suite, or nothing at all.

It lists the changed tracked source files (working tree, index, and commits
not yet on the upstream or default branch) whose modification time is newer
than the run record-test-run.py keeps, or says no run is recorded.
It never blocks.

  LEAN_SKIP_TEST_BEFORE_PUSH=1   turn it off (and record-test-run.py), in the
                                 environment, settings env, or inline
  test-before-push.py --self-test
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "lib"))
import hookkit  # noqa: E402

SOURCE_EXT = {".c", ".cc", ".cpp", ".cs", ".cjs", ".fs", ".go", ".h", ".hpp", ".java", ".js", ".jsx",
              ".kt", ".mjs", ".php", ".py", ".rb", ".rs", ".svelte", ".swift", ".ts", ".tsx", ".vue"}
MAX_LISTED = 10


def is_push(seg):
    call = hookkit.git_call(seg)
    if call and call[0] == "push":
        return True
    gh = hookkit.gh_call(seg)
    return bool(gh) and gh[0][:1] == ["pr"] and gh[0][1:2] in (["create"], ["merge"])


def changed_source(root):
    names = set()
    for args in (("diff", "--name-only", "HEAD"), ("diff", "--name-only", "--cached")):
        names.update((hookkit.git(root, *args) or "").splitlines())
    base = None
    for ref in ("@{upstream}", "origin/HEAD", "origin/main", "origin/master"):
        base = hookkit.git(root, "merge-base", ref, "HEAD")
        if base:
            break
    if base:
        names.update((hookkit.git(root, "diff", "--name-only", base, "HEAD") or "").splitlines())
    return sorted(n for n in names if n and os.path.splitext(n)[1].lower() in SOURCE_EXT
                  and os.path.isfile(os.path.join(root, n)))


def message(command, cwd):
    for seg in hookkit.segments(command, cwd):
        if not is_push(seg) or hookkit.disabled("LEAN_SKIP_TEST_BEFORE_PUSH", seg):
            continue
        root = hookkit.git(seg.cwd, "rev-parse", "--show-toplevel")
        if not root:
            continue
        changed = changed_source(root)
        if not changed:
            return None
        marker = hookkit.test_marker(root)
        if not marker or not os.path.exists(marker):
            return ("No passing test run is recorded in this repository, and this pushes source "
                    "changes. Which test can observe this change? Run that one first (a --no-build "
                    "run does not count). LEAN_SKIP_TEST_BEFORE_PUSH=1, inline or in settings env, "
                    "turns this note off.")
        ran_at = os.path.getmtime(marker)
        stale = [n for n in changed if os.path.getmtime(os.path.join(root, n)) > ran_at]
        if not stale:
            return None
        with open(marker, encoding="utf-8", errors="replace") as fh:
            last = fh.read().strip() or "unknown"
        listed = "\n  ".join(stale[:MAX_LISTED]) + ("\n  ..." if len(stale) > MAX_LISTED else "")
        return (f"These files changed after the last passing test run ({last}):\n  {listed}\n"
                "A suite that passed before this edit is not evidence about it. Rerun the suite "
                "that can observe the change, not only the fast one. "
                "LEAN_SKIP_TEST_BEFORE_PUSH=1, inline or in settings env, turns this note off.")
    return None


def main():
    if hookkit.disabled("LEAN_SKIP_TEST_BEFORE_PUSH"):
        return 0
    event = hookkit.read_event()
    command = hookkit.tool_input(event).get("command") or ""
    if "push" not in command and "gh" not in command:
        return 0
    text = message(command, event.get("cwd") or os.getcwd())
    if text:
        hookkit.note("PreToolUse", text)
    return 0


def self_test():
    import subprocess
    import tempfile
    import time

    failures = 0

    def check(name, got, want):
        nonlocal failures
        if bool(got) != want:
            print(f"FAIL {name}: got {got!r}")
            failures += 1

    with tempfile.TemporaryDirectory() as tmp:
        def git(*args):
            subprocess.run(["git", "-C", tmp, "-c", "user.email=t@example.org", "-c", "user.name=t", *args],
                           check=True, capture_output=True)
        git("init", "-q")
        with open(os.path.join(tmp, "app.py"), "w") as fh:
            fh.write("x = 1\n")
        with open(os.path.join(tmp, "README.md"), "w") as fh:
            fh.write("docs\n")
        git("add", ".")
        git("commit", "-q", "-m", "init")
        check("clean tree is quiet", message("git push", tmp), False)
        with open(os.path.join(tmp, "README.md"), "a") as fh:
            fh.write("more\n")
        check("docs-only change is quiet", message("git push", tmp), False)
        with open(os.path.join(tmp, "app.py"), "a") as fh:
            fh.write("y = 2\n")
        check("no run recorded", message("git push origin HEAD", tmp), True)
        check("not a push", message("git status && echo git push", tmp), False)
        marker = hookkit.test_marker(tmp)
        with open(marker, "w") as fh:
            fh.write("pytest\n")
        past = time.time() - 60
        os.utime(os.path.join(tmp, "app.py"), (past, past))
        check("run after the edit is quiet", message("gh pr create --fill", tmp), False)
        with open(os.path.join(tmp, "app.py"), "a") as fh:
            fh.write("z = 3\n")
        stale = message("cd / && gh pr merge 3 --squash", tmp)
        check("wrong cwd after cd is quiet", stale, False)
        stale = message("gh pr merge 3 --squash", tmp)
        check("edit after the run warns", stale, True)
        check("stale file is named", stale and "app.py" in stale, True)
        check("inline off switch", message("LEAN_SKIP_TEST_BEFORE_PUSH=1 git push", tmp), False)
    print("self-test: ok" if failures == 0 else f"self-test: {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        sys.exit(self_test())
    hookkit.run(main)

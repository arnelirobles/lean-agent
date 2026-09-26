#!/usr/bin/env python3
"""Block generic file names written straight into a shared scratchpad root.

PreToolUse hook on Write. When several agents share one scratchpad, a file
like pr-body.md or notes.md at its root is overwritten by whichever agent
writes last, and a pull request goes out with another agent's body. The fix
is one subdirectory per task. This blocks pr-body.md, body*.md, notes.md and
plan.md written directly into a directory named "scratchpad", or into the
directory in $CLAUDE_SCRATCHPAD when that is set. The same names one level
down (scratchpad/<task>/pr-body.md) pass.

  LEAN_ALLOW_SCRATCHPAD_ROOT=1   turn it off (environment or the settings env block)
  scratchpad-root.py --self-test
"""
import fnmatch
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "lib"))
import hookkit  # noqa: E402

SHARED_NAMES = ("pr-body.md", "body*.md", "notes.md", "plan.md")


def problem(path, cwd, scratchpad=None):
    path = hookkit.resolve(path, cwd)
    name = os.path.basename(path).lower()
    if not any(fnmatch.fnmatchcase(name, pattern) for pattern in SHARED_NAMES):
        return None
    parent = os.path.dirname(path)
    at_root = os.path.basename(parent) == "scratchpad"
    if scratchpad:
        at_root = at_root or os.path.realpath(parent) == os.path.realpath(hookkit.resolve(scratchpad, cwd))
    if not at_root:
        return None
    return (f"{path} sits at the scratchpad root, where another agent writing the same name "
            f"overwrites it. Write it under a per-task subdirectory instead, for example "
            f"{os.path.join(parent, '<task>', os.path.basename(path))}.")


def main():
    if hookkit.disabled("LEAN_ALLOW_SCRATCHPAD_ROOT"):
        return 0
    event = hookkit.read_event()
    path = hookkit.tool_input(event).get("file_path") or ""
    if not path:
        return 0
    found = problem(path, event.get("cwd") or os.getcwd(), os.environ.get("CLAUDE_SCRATCHPAD"))
    if found:
        print("Blocked by lean-agent: " + found
              + " LEAN_ALLOW_SCRATCHPAD_ROOT=1 in the env block of Claude Code settings turns this check off.", file=sys.stderr)
        return 2
    return 0


def self_test():
    failures = 0
    cases = [
        ("/tmp/s/scratchpad/pr-body.md", "/", None, True),
        ("/tmp/s/scratchpad/body-42.md", "/", None, True),
        ("/tmp/s/scratchpad/notes.md", "/", None, True),
        ("/tmp/s/scratchpad/PLAN.md", "/", None, True),
        ("pr-body.md", "/tmp/s/scratchpad", None, True),
        ("/tmp/pad/notes.md", "/", "/tmp/pad", True),
        ("notes.md", "/tmp/pad", "/tmp/pad", True),
        ("/tmp/s/scratchpad/t1/pr-body.md", "/", None, False),
        ("/tmp/s/scratchpad/findings.md", "/", None, False),
        ("/tmp/s/scratchpad/body.txt", "/", None, False),
        ("/repo/docs/notes.md", "/", None, False),
        ("/tmp/pad/t2/plan.md", "/", "/tmp/pad", False),
        ("/repo/antibody.md", "/", None, False),
    ]
    for path, cwd, pad, want in cases:
        if bool(problem(path, cwd, pad)) != want:
            print(f"FAIL {path!r} cwd={cwd!r} pad={pad!r}: want blocked={want}")
            failures += 1
    print("self-test: ok" if failures == 0 else f"self-test: {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        sys.exit(self_test())
    hookkit.run(main)

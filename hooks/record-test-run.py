#!/usr/bin/env python3
"""Record when a compiled test run passed in this repository.

PostToolUse hook on Bash, paired with test-before-push.py. That hook needs to
know whether any test run happened after the last source edit, and nothing
else in a session knows that. Claude Code runs PostToolUse only when the tool
call succeeded, so a recorded run is a passing one.

Counted: dotnet test, npm/pnpm/yarn test (and `run test...`), go test, pytest
(also python -m pytest), cargo test. Not counted: dotnet test --no-build and
cargo test --no-run, because one passes against stale output and the other
runs nothing.

The record is <git dir>/lean-last-test-run (per worktree, never committed):
its modification time is the run, its one line is the command.

  LEAN_SKIP_TEST_BEFORE_PUSH=1   turn off this hook and test-before-push.py
  record-test-run.py --self-test
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "lib"))
import hookkit  # noqa: E402


def is_test_run(words):
    if not words:
        return False
    tool = os.path.basename(words[0])
    rest = words[1:]
    if tool in ("python", "python3") and rest[:2] == ["-m", "pytest"]:
        return True
    if tool == "pytest":
        return True
    if tool in ("dotnet", "go") and rest[:1] == ["test"]:
        return "--no-build" not in rest
    if tool == "cargo" and rest[:1] == ["test"]:
        return "--no-run" not in rest
    if tool in ("npm", "pnpm", "yarn"):
        if rest[:1] in (["test"], ["t"]):
            return True
        return rest[:1] == ["run"] and len(rest) > 1 and rest[1].split(":")[0] == "test"
    return False


def test_runs(command, cwd):
    """(cwd, command words) for each counted test run in a shell command."""
    return [(seg.cwd, seg.words) for seg in hookkit.segments(command, cwd) if is_test_run(seg.words)]


def record(command, cwd):
    recorded = []
    for run_cwd, words in test_runs(command, cwd):
        marker = hookkit.test_marker(run_cwd)
        if marker:
            with open(marker, "w", encoding="utf-8") as fh:
                fh.write(" ".join(words)[:500] + "\n")
            recorded.append(marker)
    return recorded


def main():
    if hookkit.disabled("LEAN_SKIP_TEST_BEFORE_PUSH"):
        return 0
    event = hookkit.read_event()
    command = hookkit.tool_input(event).get("command") or ""
    if "test" in command:
        record(command, event.get("cwd") or os.getcwd())
    return 0


def self_test():
    import subprocess
    import tempfile

    failures = 0
    counted = ["dotnet test", "dotnet test Api.Tests/Api.Tests.csproj", "npm test", "npm run test:unit",
               "pnpm test", "yarn test", "go test ./...", "pytest -q", "python3 -m pytest tests",
               "cargo test", "cd sub && npm t"]
    ignored = ["dotnet test --no-build", "cargo test --no-run", "npm run build", "echo pytest",
               "git commit -m 'go test'", "npm install", "dotnet build"]
    for command in counted:
        if not test_runs(command, "/"):
            print(f"FAIL not counted: {command!r}")
            failures += 1
    for command in ignored:
        if test_runs(command, "/"):
            print(f"FAIL counted: {command!r}")
            failures += 1
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(["git", "init", "-q", tmp], check=True)
        if not record("go test ./...", tmp) or not os.path.exists(os.path.join(tmp, ".git", "lean-last-test-run")):
            print("FAIL marker not written")
            failures += 1
        if record("go test ./...", os.path.dirname(tmp) + "/does-not-exist"):
            print("FAIL marker written outside a repository")
            failures += 1
    print("self-test: ok" if failures == 0 else f"self-test: {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        sys.exit(self_test())
    hookkit.run(main)

"""Shared plumbing for the lean-agent hooks.

Every hook reads one JSON event on stdin and must never break the tool call it
watches because of its own bug. So `run` turns any internal error into exit 0,
and every git call has a short timeout. Standard library only, Python 3.9+.

  python3 lib/hookkit.py --self-test
"""
import json
import os
import re
import shlex
import subprocess
import sys

SEPARATORS = {";", "&&", "||", "|", "&", "\n", "(", ")", ";;", "|&"}
GIT_GLOBAL_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}


def run(main):
    """Run a hook's main and exit with its code; an internal error exits 0."""
    try:
        code = main()
    except SystemExit:
        raise
    except Exception as exc:  # a broken hook must not block the tool call
        print(f"lean-agent hook error ignored: {exc!r}", file=sys.stderr)
        code = 0
    sys.exit(code or 0)


def read_event():
    try:
        event = json.load(sys.stdin)
    except ValueError:
        return {}
    return event if isinstance(event, dict) else {}


def tool_input(event):
    value = event.get("tool_input")
    return value if isinstance(value, dict) else {}


def note(event_name, text):
    """Print a non-blocking note that Claude reads with the tool result."""
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": event_name, "additionalContext": text}}))


def disabled(name):
    return os.environ.get(name) == "1"


def resolve(path, cwd):
    path = os.path.expanduser(path)
    if not os.path.isabs(path):
        path = os.path.join(cwd or os.getcwd(), path)
    return os.path.normpath(path)


def git(cwd, *args, timeout=3):
    """stdout of a git command, stripped, or None when it fails."""
    try:
        result = subprocess.run(["git", "-C", cwd or ".", *args], capture_output=True,
                                text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def test_marker(cwd):
    """Path of the file recording this worktree's last compiled test run, or None outside git."""
    git_dir = git(cwd, "rev-parse", "--absolute-git-dir")
    return os.path.join(git_dir, "lean-last-test-run") if git_dir else None


def tokens(command):
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()<>\n")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    try:
        return list(lexer)
    except ValueError:
        return command.split()


class Segment:
    """One simple command out of a shell line: its env prefix, words and cwd."""

    def __init__(self, env, words, cwd):
        self.env, self.words, self.cwd = env, words, cwd

    def __repr__(self):
        return f"Segment({self.env!r}, {self.words!r}, {self.cwd!r})"


def segments(command, cwd="."):
    """Split a shell command into simple commands, following `cd DIR`."""
    out, current = [], []
    for tok in tokens(command) + [";"]:
        if tok in SEPARATORS or set(tok) <= set(";&|()\n") and tok:
            if current:
                env = {}
                while current and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", current[0], re.S):
                    key, value = current.pop(0).split("=", 1)
                    env[key] = value
                if current and current[0] == "cd":
                    target = current[1] if len(current) > 1 else os.path.expanduser("~")
                    cwd = resolve(target, cwd)
                elif current:
                    out.append(Segment(env, current, cwd))
            current = []
        else:
            current.append(tok)
    return out


def git_call(seg):
    """For a git segment return (subcommand, args, cwd, config) or None.

    config holds `-c key=value` pairs given before the subcommand.
    """
    words = seg.words
    if not words or os.path.basename(words[0]) != "git":
        return None
    cwd, config, i = seg.cwd, {}, 1
    while i < len(words) and words[i].startswith("-"):
        word = words[i]
        if word in GIT_GLOBAL_WITH_VALUE and i + 1 < len(words):
            value = words[i + 1]
            if word == "-C":
                cwd = resolve(value, cwd)
            elif word == "-c" and "=" in value:
                key, val = value.split("=", 1)
                config[key.lower()] = val
            i += 2
        else:
            i += 1
    if i >= len(words):
        return None
    return words[i], words[i + 1:], cwd, config


def gh_call(seg):
    """For a gh segment return (words after gh, cwd) or None."""
    words = seg.words
    if not words or os.path.basename(words[0]) != "gh":
        return None
    return words[1:], seg.cwd


def self_test():
    failures = 0

    def check(name, got, want):
        nonlocal failures
        if got != want:
            print(f"FAIL {name}: got {got!r}, want {want!r}")
            failures += 1

    segs = segments('cd /r && GIT_AUTHOR_EMAIL=a@b git -C sub -c user.email=x@y commit -m "a && b"\ngit push', "/w")
    check("segment count", len(segs), 2)
    check("env prefix", segs[0].env, {"GIT_AUTHOR_EMAIL": "a@b"})
    check("cd followed", segs[0].cwd, "/r")
    call = git_call(segs[0])
    check("git subcommand", call[0], "commit")
    check("git -C", call[2], "/r/sub")
    check("git -c", call[3], {"user.email": "x@y"})
    check("quoted && kept", call[1], ["-m", "a && b"])
    check("second segment", git_call(segs[1])[0], "push")
    check("echo is not git", git_call(segments('echo "git commit"')[0]), None)
    check("grep is not git", [git_call(s) for s in segments("git log --grep 'git commit'")][0][0], "log")
    check("gh call", gh_call(segments("gh pr create --fill | cat")[0])[0][:2], ["pr", "create"])
    check("unbalanced quote falls back", len(segments('git commit -m "oops')) >= 1, True)
    check("resolve relative", resolve("a/b", "/x"), "/x/a/b")
    print("self-test: ok" if failures == 0 else f"self-test: {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        sys.exit(self_test())
    print(__doc__)

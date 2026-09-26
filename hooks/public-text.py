#!/usr/bin/env python3
"""Block agent attribution and AI slop in commits, pull requests, issues and releases.

Runs as a Claude Code PreToolUse hook on Bash. Reads the hook JSON on stdin,
and when the command is a git commit or tag or a gh pr/issue/release write,
checks the command text and any message file it passes. Exit 2 blocks the call
and tells the agent what to fix.

  LEAN_ALLOW_ATTRIBUTION=1   skip the attribution check
  LEAN_ALLOW_SLOP=1          skip the slop check

gh api calls count as writes when they send fields (-f, -F, --field,
--raw-field), a body (--input FILE) or use POST, PATCH or PUT. A GET, or a
graphql query that is not a mutation, is a read and passes. The files named by
-F key=@FILE and --input FILE are read and checked too. The word and pattern
lists live in lib/house_style.py.
  public-text.py --self-test
"""
import json
import os
import re
import shlex
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "lib"))
import hookkit  # noqa: E402
from house_style import scan  # noqa: E402

FILE_FLAGS = {"-F", "--file", "--body-file", "--notes-file"}
API_FIELD_FLAGS = {"-f", "-F", "--field", "--raw-field"}
WRITES = [
    re.compile(r"\bgit\s+(?:-C\s+\S+\s+)?(commit|tag|notes)\b"),
    re.compile(r"\bgh\s+(pr|issue|release)\s+(create|edit|comment|review|merge)\b"),
]


def gh_api_write(command):
    """True when a gh api call sends fields or a body, so it can post public text."""
    try:
        words = shlex.split(command, posix=True)
    except ValueError:
        words = command.split()
    for i in range(len(words) - 1):
        if os.path.basename(words[i]) != "gh" or words[i + 1] != "api":
            continue
        args = words[i + 2:]
        method = ""
        for j, word in enumerate(args):
            if word in ("-X", "--method") and j + 1 < len(args):
                method = args[j + 1].upper()
            elif word.startswith("--method="):
                method = word.split("=", 1)[1].upper()
        if method == "GET":
            continue
        if "graphql" in args:
            if re.search(r"\bmutation\b", command):
                return True
            continue
        if method in ("POST", "PATCH", "PUT") or any(
                w in API_FIELD_FLAGS or w == "--input" or w.startswith(("--input=", "--field=", "--raw-field="))
                or re.fullmatch(r"-[fF]\S+", w) for w in args):
            return True
    return False


def message_files(command):
    try:
        words = shlex.split(command, posix=True)
    except ValueError:
        return []
    files = []
    for i, word in enumerate(words):
        nxt = words[i + 1] if i + 1 < len(words) else None
        if word in FILE_FLAGS and nxt:
            files.append(nxt)
        if word in API_FIELD_FLAGS and nxt and re.fullmatch(r"[\w.\[\]]+=@.+", nxt):
            files.append(nxt.split("=@", 1)[1])
        for flag in FILE_FLAGS | {"--input"}:
            if flag.startswith("--") and word.startswith(flag + "="):
                files.append(word.split("=", 1)[1])
        if word == "--input" and nxt:
            files.append(nxt)
        if re.fullmatch(r"-F\S+", word):
            files.append(word[2:])
    return [f for f in files if f != "-" and "=" not in f.split("/")[0]]


def public_texts(command, cwd):
    texts = [("the command", command)]
    for name in message_files(command):
        path = name if os.path.isabs(name) else os.path.join(cwd, name)
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                texts.append((name, fh.read()))
        except OSError:
            pass
    return texts


def git_write(command):
    """git commit/tag/notes, also behind global options such as -c user.email=..."""
    for seg in hookkit.segments(command):
        call = hookkit.git_call(seg)
        if call and call[0] in ("commit", "tag", "notes"):
            return True
    return False


def problems(command, cwd="."):
    if not (any(p.search(command) for p in WRITES) or gh_api_write(command) or git_write(command)):
        return []
    attribution = os.environ.get("LEAN_ALLOW_ATTRIBUTION") != "1"
    slop = os.environ.get("LEAN_ALLOW_SLOP") != "1"
    return [f"{kind}: {where} contains {what}"
            for where, text in public_texts(command, cwd)
            for kind, what in scan(text, attribution=attribution, slop=slop)]


def self_test():
    import tempfile

    blocked = [
        'git commit -m "fix it\n\nCo-Authored-By: Claude <noreply@anthropic.com>"',
        'git commit -m "x" -m "Claude-Session: https://claude.ai/code/session_1"',
        'gh pr create --title t --body "done\n\nGenerated with [Claude Code](https://x)"',
        'gh issue comment 5 --body "see https://claude.ai/code/session_abc"',
        'git commit -m "add a robust retry"',
        'gh pr create --title t --body "fast \u2014 and safe"',
        'gh release create v1 --notes "old \u2192 new"',
        'gh issue create --title t --body "Done. Additionally, it logs."',
        'git tag -a v1 -m "a comprehensive release"',
        "git -c user.name='A B' -c user.email=a@example.org commit -m 'a rob" + "ust retry'",
        "gh api repos/o/r/issues/1/comments -f body='a seam" + "less fix'",
        "gh api repos/o/r/issues/1/comments --field 'body=old \u2192 new'",
        "gh api -X PATCH repos/o/r/pulls/2 --raw-field 'body=Done. Addition" + "ally, it logs.'",
        "gh api graphql -f query='mutation { addComment(body: \"see claude.ai/code/" + "session_x\") }'",
    ]
    allowed = [
        'git commit -m "wait for database health before the integration suite"',
        'git commit -m "x\n\nCo-Authored-By: Jane Doe <jane@example.com>"',
        'git commit -m "log the retry count and add it to the summary"',
        'echo "Claude-Session: x robust \u2014"',
        'git log --grep "Co-Authored-By: Claude"',
        'gh pr create --body "the hook blocks a `Claude-Session:` trailer"',
        "gh api repos/o/r/pulls --jq '.[] | select(.body | test(\"rob" + "ust\"))'",
        "gh api -X GET search/issues -f q='rob" + "ust in:body'",
        "gh api graphql -f query='{ search(query: \"rob" + "ust\", type: ISSUE) { issueCount } }'",
    ]
    failures = 0
    for cmd in blocked:
        if not problems(cmd):
            print(f"FAIL not blocked: {cmd!r}")
            failures += 1
    for cmd in allowed:
        if problems(cmd):
            print(f"FAIL blocked: {cmd!r}: {problems(cmd)}")
            failures += 1
    with tempfile.TemporaryDirectory() as tmp:
        body = os.path.join(tmp, "body.md")
        with open(body, "w") as fh:
            fh.write("Fixes #1\n\nhttps://claude.ai/code/session_abc\n")
        slop = os.path.join(tmp, "notes.md")
        with open(slop, "w") as fh:
            fh.write("This release is seamless.\n")
        for cmd in (f"gh pr create --body-file {body}", f"git commit -F {body}",
                    f"gh release create v1 --notes-file={slop}", "git commit -F body.md",
                    "gh api repos/o/r/issues/1/comments -F body=@body.md",
                    f"gh api repos/o/r/issues/1/comments --input {slop}",
                    "gh api repos/o/r/issues/1/comments --input=notes.md"):
            if not problems(cmd, cwd=tmp):
                print(f"FAIL file not checked: {cmd!r}")
                failures += 1
    print("self-test: ok" if failures == 0 else f"self-test: {failures} failed")
    return 1 if failures else 0


def main():
    if sys.argv[1:] == ["--self-test"]:
        return self_test()
    try:
        event = json.load(sys.stdin)
    except ValueError:
        return 0
    if not isinstance(event, dict):
        return 0
    command = (event.get("tool_input") or {}).get("command") or ""
    found = problems(command, event.get("cwd") or ".")
    if found:
        print("Blocked by lean-agent, public text needs fixing:\n  "
              + "\n  ".join(found)
              + "\nRewrite it plainly (commas, periods or parentheses for dashes, "
              "words for arrows, no attribution) and run the command again.",
              file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except Exception as exc:  # a crash here must not block every Bash call
        print(f"public-text.py error ignored: {exc!r}", file=sys.stderr)
        code = 0
    sys.exit(code)

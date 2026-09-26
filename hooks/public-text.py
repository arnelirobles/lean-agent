#!/usr/bin/env python3
"""Block agent attribution and AI slop in commits, pull requests, issues and releases.

Runs as a Claude Code PreToolUse hook on Bash. Reads the hook JSON on stdin,
and when the command is a git commit or tag or a gh pr/issue/release write,
checks the command text and any message file it passes. Exit 2 blocks the call
and tells the agent what to fix.

  LEAN_ALLOW_ATTRIBUTION=1   skip the attribution check
  LEAN_ALLOW_SLOP=1          skip the slop check
  public-text.py --self-test
"""
import json
import os
import re
import shlex
import sys

ATTRIBUTION = [
    r"co-authored-by:[^\n]*(claude|anthropic|copilot|cursor|codex|gemini|\[bot\])",
    r"claude-session:",
    r"claude\.ai/code/session_",
    r"generated with \[?claude code",
    r"noreply@anthropic\.com",
]
SLOP = [
    "\u2014",
    "\u2013",
    "\u2192",
    r"\bcomprehensive\b",
    r"\brobust\b",
    r"\bseamless(ly)?\b",
    r"\bdelv(e|es|ing)\b",
    r"\bleverag(e|es|ed|ing)\b",
    r"\bstreamlin(e|es|ed|ing)\b",
    r"\bcutting-edge\b",
    r"\belevat(e|es|ed|ing)\b",
    r"\bsupercharg(e|es|ed|ing)\b",
    r"\bgame-changing\b",
    r"\bit'?s important to note\b",
    r"\bin conclusion\b",
    r"\bfurthermore\b",
    r"(^|[.!?]\s+|[\"'])additionally\b",
]
SLOP_NAMES = {"\u2014": "an em dash", "\u2013": "an en dash", "\u2192": "an arrow glyph"}
FILE_FLAGS = {"-F", "--file", "--body-file", "--notes-file"}
WRITES = [
    re.compile(r"\bgit\s+(?:-C\s+\S+\s+)?(commit|tag|notes)\b"),
    re.compile(r"\bgh\s+(pr|issue|release)\s+(create|edit|comment|review|merge)\b"),
]


def message_files(command):
    try:
        words = shlex.split(command, posix=True)
    except ValueError:
        return []
    files = []
    for i, word in enumerate(words):
        if word in FILE_FLAGS and i + 1 < len(words):
            files.append(words[i + 1])
        for flag in FILE_FLAGS:
            if flag.startswith("--") and word.startswith(flag + "="):
                files.append(word.split("=", 1)[1])
        if re.fullmatch(r"-F\S+", word):
            files.append(word[2:])
    return [f for f in files if f != "-"]


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


def problems(command, cwd="."):
    if not any(p.search(command) for p in WRITES):
        return []
    checks = []
    if os.environ.get("LEAN_ALLOW_ATTRIBUTION") != "1":
        checks += [("attribution", p) for p in ATTRIBUTION]
    if os.environ.get("LEAN_ALLOW_SLOP") != "1":
        checks += [("slop", p) for p in SLOP]
    found = []
    for where, text in public_texts(command, cwd):
        for kind, pattern in checks:
            match = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
            if match:
                what = SLOP_NAMES.get(pattern) or f"'{match.group(0).strip()}'"
                found.append(f"{kind}: {where} contains {what}")
    return found


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
    ]
    allowed = [
        'git commit -m "wait for database health before the integration suite"',
        'git commit -m "x\n\nCo-Authored-By: Jane Doe <jane@example.com>"',
        'git commit -m "log the retry count and add it to the summary"',
        'echo "Claude-Session: x robust \u2014"',
        'git log --grep "Co-Authored-By: Claude"',
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
                    f"gh release create v1 --notes-file={slop}", "git commit -F body.md"):
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
    command = (event.get("tool_input") or {}).get("command") or ""
    found = problems(command, event.get("cwd") or ".")
    if found:
        print("Blocked by lean-agent-method, public text needs fixing:\n  "
              + "\n  ".join(found)
              + "\nRewrite it plainly (commas, periods or parentheses for dashes, "
              "words for arrows, no attribution) and run the command again.",
              file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

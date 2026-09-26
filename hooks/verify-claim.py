#!/usr/bin/env python3
"""Ask for the claim to be checked before a public review or comment posts.

PreToolUse hook on Bash, for gh issue comment, gh pr comment and gh pr review.
The costly public mistakes are confident claims nobody traced: recommending an
endpoint change without reading its auth config, or saying a background task
can crash startup when its caller swallows the error. Someone may act on the
comment. It never blocks, and it stays quiet for a short body (under 30 words
passed with --body/-b or --body-file/-F), since an acknowledgement makes no case.

  LEAN_SKIP_VERIFY_CLAIM=1   turn it off (environment, settings env, or inline)
  verify-claim.py --self-test
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "lib"))
import hookkit  # noqa: E402

SHORT_WORDS = 30
NOTE = ("Before this posts: is every claim in it checked? If it says something fails, blocks or "
        "is slow, find the caller (is it awaited, is the error swallowed?). If it recommends a "
        "change, read the config that governs it (auth, flags, defaults) first. Anything not "
        "checked gets \"I think\" and a line on what is unverified. "
        "LEAN_SKIP_VERIFY_CLAIM=1, inline or in settings env, turns this note off.")


def body_text(args, cwd):
    for i, word in enumerate(args):
        value = args[i + 1] if i + 1 < len(args) else None
        if word.startswith(("--body=", "--body-file=")):
            word, value = word.split("=", 1)
        if word in ("--body", "-b") and value is not None:
            return value
        if word in ("--body-file", "-F") and value and value != "-":
            try:
                with open(hookkit.resolve(value, cwd), encoding="utf-8", errors="replace") as fh:
                    return fh.read()
            except OSError:
                return None
    return None


def message(command, cwd):
    for seg in hookkit.segments(command, cwd):
        gh = hookkit.gh_call(seg)
        if not gh or hookkit.disabled("LEAN_SKIP_VERIFY_CLAIM", seg):
            continue
        args = gh[0]
        if args[:2] not in (["issue", "comment"], ["pr", "comment"], ["pr", "review"]):
            continue
        body = body_text(args[2:], seg.cwd)
        if body is not None and len(body.split()) < SHORT_WORDS:
            continue
        return NOTE
    return None


def main():
    if hookkit.disabled("LEAN_SKIP_VERIFY_CLAIM"):
        return 0
    event = hookkit.read_event()
    command = hookkit.tool_input(event).get("command") or ""
    if "gh" not in command:
        return 0
    text = message(command, event.get("cwd") or os.getcwd())
    if text:
        hookkit.note("PreToolUse", text)
    return 0


def self_test():
    import tempfile

    failures = 0
    long_body = " ".join(["word"] * 40)
    with tempfile.TemporaryDirectory() as tmp:
        with open(os.path.join(tmp, "long.md"), "w") as fh:
            fh.write(long_body)
        with open(os.path.join(tmp, "short.md"), "w") as fh:
            fh.write("Thanks, merged.")
        noted = [f"gh pr comment 4 --body '{long_body}'", "gh pr review 4 --approve",
                 "gh issue comment 9 --body-file long.md", f"gh pr review 4 --comment -b '{long_body}'",
                 "gh pr comment 4 --body-file=long.md", "gh issue comment 9 --body-file missing.md"]
        quiet = ["gh pr comment 4 --body 'Thanks, merged.'", "gh issue comment 9 -F short.md",
                 "gh pr view 4 --comments", "gh issue list", "echo gh pr comment",
                 "gh pr create --body 'x'", "LEAN_SKIP_VERIFY_CLAIM=1 gh pr review 4 --approve"]
        for command in noted:
            if not message(command, tmp):
                print(f"FAIL no note: {command[:60]!r}")
                failures += 1
        for command in quiet:
            if message(command, tmp):
                print(f"FAIL noted: {command!r}")
                failures += 1
    print("self-test: ok" if failures == 0 else f"self-test: {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        sys.exit(self_test())
    hookkit.run(main)

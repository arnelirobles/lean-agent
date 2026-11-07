#!/usr/bin/env python3
"""Ask for the claim to be checked before a public review or comment posts.

PreToolUse hook on Bash, for issue comments and pull request comments and reviews on any host
(gh issue comment, gh pr comment, gh pr review, glab mr note, glab issue note).
The costly public mistakes are confident claims nobody traced: recommending an
endpoint change without reading its auth config, or saying a background task
can crash startup when its caller swallows the error. Someone may act on the
comment. It never blocks, and it stays quiet for a short body (under 30 words
passed with --body/-b or --body-file/-F, or glab's --message/-m), since an acknowledgement makes no case.

  LEAN_SKIP_VERIFY_CLAIM=1   turn it off (environment or your user settings env;
                              inline and repository settings are ignored: a guard)
  verify-claim.py --self-test
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "lib"))
import hookkit  # noqa: E402

HOOK = {
    "events": [
        {"event": "PreToolUse", "matcher": "Bash", "if": "Bash(gh *)", "group": 1, "order": 10},
    ],
    "timeout": 10,
    "mode": "process",
    "pack": "core",
    "guard": True,
    "default": {"strict": "note", "standard": "note"},
}

SHORT_WORDS = 30
# Commands that post a comment or review, from the table in lib/hookkit.py, and the flags that hold the body.
COMMENTS = {("issue", "comment"), ("pr", "comment"), ("pr", "review"),
            ("mr", "note"), ("mr", "note", "create"), ("issue", "note"), ("issue", "note", "create")}
BODY_FLAGS = {"gh": ({"--body", "-b"}, {"--body-file", "-F"}), "glab": ({"--message", "-m"}, set())}
NOTE = ("Before this posts: is every claim in it checked? If it says something fails, blocks or "
        "is slow, find the caller (is it awaited, is the error swallowed?). If it recommends a "
        "change, read the config that governs it (auth, flags, defaults) first. Anything not "
        "checked gets \"I think\" and a line on what is unverified. "
        "Set LEAN_SKIP_VERIFY_CLAIM=1 in the env block of your own settings (~/.claude/settings.json) to turn this note off.")


def body_text(call):
    text_flags, file_flags = BODY_FLAGS[call.cli]
    for _flag, value in hookkit.forge_flag_values(call, text_flags | file_flags):
        if _flag in text_flags:
            return value
        if value != "-":
            try:
                with open(hookkit.resolve(value, call.cwd), encoding="utf-8", errors="replace") as fh:
                    return fh.read()
            except OSError:
                return None
    return None


def message(command, cwd):
    for seg in hookkit.segments(command, cwd):
        call = hookkit.forge_call(seg)
        if not call or call.command not in COMMENTS or call.cli not in BODY_FLAGS \
                or hookkit.disabled("LEAN_SKIP_VERIFY_CLAIM", seg):
            continue
        body = body_text(call)
        if body is not None and len(body.split()) < SHORT_WORDS:
            continue
        return NOTE
    return None


def main():
    if hookkit.disabled("LEAN_SKIP_VERIFY_CLAIM"):
        return 0
    event = hookkit.read_event()
    command = hookkit.tool_input(event).get("command") or ""
    if not any(cli in command for cli in hookkit.FORGE_CLIS):
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
                 "gh pr comment 4 --body-file=long.md", "gh issue comment 9 --body-file missing.md",
                 f"glab mr note 4 -m '{long_body}'", f"glab mr note create 4 --message '{long_body}'",
                 f"glab issue note 9 --message='{long_body}'", "glab mr note 4",
                 # a guard: the command's own prefix cannot turn it off (the guard note is printed too)
                 "LEAN_SKIP_VERIFY_CLAIM=1 gh pr review 4 --approve"]
        quiet = ["gh pr comment 4 --body 'Thanks, merged.'", "gh issue comment 9 -F short.md",
                 "gh pr view 4 --comments", "gh issue list", "echo gh pr comment",
                 "gh pr create --body 'x'",
                 "glab mr note 4 -m 'Thanks, merged.'", "glab mr view 4 --comments", "glab mr create -d 'x'",
                 "az repos pr create --description 'x'", "echo glab mr note 4"]
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

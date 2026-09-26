#!/usr/bin/env python3
"""Note house-style problems in prose the agent just wrote to a file.

PostToolUse hook on Write, Edit and MultiEdit. Release notes, pull request
bodies and docs are usually drafted in a file first, and the style rules get
forgotten there because they feel like chat rules. This scans only the text
the tool call wrote (the new content of a Write, the new_string of an Edit or
of each MultiEdit edit) with the lists in lib/house_style.py, and tells the
agent what it found. It never blocks: a file may quote outside text on purpose.

Files it looks at: .md, .markdown, .txt, .rst, .adoc, git message files
(COMMIT_EDITMSG, TAG_EDITMSG, MERGE_MSG) and names that look like a message or
body (commit-msg, pr-body, release-notes and similar) with no extension or .msg.

  LEAN_SKIP_STYLE_NOTE=1   turn it off (environment or the settings env block)
  style-note.py --self-test
"""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "lib"))
import hookkit  # noqa: E402
from house_style import scan  # noqa: E402

PROSE_EXT = {".md", ".markdown", ".txt", ".rst", ".adoc"}
GIT_MESSAGES = {"commit_editmsg", "tag_editmsg", "merge_msg", "squash_msg"}
MESSAGE_NAME = re.compile(r"(commit|message|msg|body|notes|release|changelog)", re.I)
MAX_HITS = 8


def is_prose(path):
    name = os.path.basename(path)
    stem, ext = os.path.splitext(name)
    if ext.lower() in PROSE_EXT or name.lower() in GIT_MESSAGES:
        return True
    return ext.lower() in ("", ".msg") and bool(MESSAGE_NAME.search(stem))


def written_text(tool_input):
    parts = [tool_input.get("content"), tool_input.get("new_string")]
    for edit in tool_input.get("edits") or []:
        if isinstance(edit, dict):
            parts.append(edit.get("new_string"))
    return "\n".join(p for p in parts if isinstance(p, str))


def hits(text):
    found = []
    for number, line in enumerate(text.splitlines(), 1):
        for kind, what in scan(line):
            found.append(f"line {number} of the new text: {kind}, {what}")
        if len(found) >= MAX_HITS:
            break
    return found


def message(event):
    tool_input = hookkit.tool_input(event)
    path = tool_input.get("file_path") or ""
    if not path or not is_prose(path):
        return None
    found = hits(written_text(tool_input))
    if not found:
        return None
    return (f"House style check on {path}: " + "; ".join(found)
            + ". Rewrite it plainly unless it is quoted outside text. "
            "LEAN_SKIP_STYLE_NOTE=1 in the env block of Claude Code settings turns this note off.")


def main():
    if hookkit.disabled("LEAN_SKIP_STYLE_NOTE"):
        return 0
    text = message(hookkit.read_event())
    if text:
        hookkit.note("PostToolUse", text)
    return 0


def self_test():
    failures = 0
    word = "seam" + "less"
    noted = [
        {"tool_input": {"file_path": "/r/README.md", "content": f"A {word} fix.\n"}},
        {"tool_input": {"file_path": "/r/docs/a.txt", "new_string": "old \u2192 new"}},
        {"tool_input": {"file_path": "/s/t1/pr-body", "content": "fast \u2014 safe"}},
        {"tool_input": {"file_path": "/r/.git/COMMIT_EDITMSG", "content": f"{word}"}},
        {"tool_input": {"file_path": "/r/notes.md", "edits": [
            {"old_string": "a", "new_string": "fine"}, {"old_string": "b", "new_string": word}]}},
    ]
    quiet = [
        {"tool_input": {"file_path": "/r/src/app.py", "content": f"# {word}\n"}},
        {"tool_input": {"file_path": "/r/README.md", "content": "Plain words.\n"}},
        {"tool_input": {"file_path": "/r/README.md", "old_string": word, "new_string": "plain"}},
        {"tool_input": {}},
        {},
    ]
    for event in noted:
        if not message(event):
            print(f"FAIL no note for {event}")
            failures += 1
    for event in quiet:
        if message(event):
            print(f"FAIL noted {event}: {message(event)}")
            failures += 1
    many = message({"tool_input": {"file_path": "a.md", "content": (word + "\n") * 50}})
    if not many or many.count("line ") != MAX_HITS:
        print("FAIL hits are not capped")
        failures += 1
    print("self-test: ok" if failures == 0 else f"self-test: {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        sys.exit(self_test())
    hookkit.run(main)

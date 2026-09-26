#!/usr/bin/env python3
"""Block agent attribution and AI slop in commits, pull requests, issues and releases.

Runs as a Claude Code PreToolUse hook on Bash. Reads the hook JSON on stdin,
and when the command is a git commit, tag or notes write, a gh pr/issue/release
write, or a gh api write, checks the text it would publish. Exit 2 blocks the
call and tells the agent what to fix.

What is checked:
  attribution  the whole command, plus every message it publishes
  slop         only the message values: git -m/--message and -F/--file, gh
               --title/--body/--notes/--subject and --body-file/--notes-file,
               gh api field values, @file and --input files, and a heredoc fed
               to `-F -`. Fenced and inline code spans are removed first, so a
               quoted word or a pasted log does not count. A `Revert "..."`
               subject line is skipped: it quotes an old commit, and the revert
               is the fix.

gh api calls count as writes when they send fields (-f, -F, --field,
--raw-field), a body (--input FILE) or use POST, PATCH or PUT. A GET, or a
graphql query that is not a mutation (read from an @file too), passes.
Message files are resolved against the directory the command runs in, after
any `cd`. The word and pattern lists live in lib/house_style.py.

  LEAN_ALLOW_ATTRIBUTION=1   skip the attribution check
  LEAN_ALLOW_SLOP=1          skip the slop check
Either can be set in the environment, in the `env` block of Claude Code
settings, or inline in front of the command (LEAN_ALLOW_SLOP=1 git commit ...).

  public-text.py --self-test
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "lib"))
import hookkit  # noqa: E402
from house_style import scan  # noqa: E402

GIT_WRITES = {"commit", "tag", "notes"}
GH_GROUPS = {"pr", "issue", "release"}
GH_WRITES = {"create", "edit", "comment", "review", "merge"}
GH_TEXT_FLAGS = {"--title", "-t", "--body", "-b", "--notes", "-n", "--subject"}
GH_FILE_FLAGS = {"--body-file", "-F", "--notes-file"}
API_FIELD_FLAGS = {"-f", "-F", "--field", "--raw-field"}
FENCE = re.compile(r"(```|~~~).*?(\1|\Z)", re.S)
INLINE_CODE = re.compile(r"`[^`\n]*`")
REVERT_SUBJECT = re.compile(r"^\s*Revert\s+\".*\"\s*$", re.M)


def read_text(name, seg):
    if name == "-":
        return "".join(seg.heredocs)
    try:
        with open(hookkit.resolve(name, seg.cwd), encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def flag_values(args, flags):
    """(flag, value) for `--flag value`, `--flag=value` and `-f value` in args, up to `--`."""
    for i, word in enumerate(args):
        if word == "--":
            return
        if word in flags and i + 1 < len(args):
            yield word, args[i + 1]
        elif "=" in word and word.split("=", 1)[0] in flags and word.startswith("--"):
            yield tuple(word.split("=", 1))


def git_texts(args, seg):
    """Messages from git commit/tag/notes: -m, --message, -F, --file, also in a cluster like -am."""
    texts = []
    for i, word in enumerate(args):
        if word == "--":
            break
        nxt = args[i + 1] if i + 1 < len(args) else None
        kind = value = None
        if word in ("-m", "--message", "-F", "--file"):
            kind, value = word, nxt
        elif word.startswith(("--message=", "--file=")):
            kind, value = word.split("=", 1)
        elif re.fullmatch(r"-[a-zA-Z]+", word) and re.search(r"[mF]", word[1:]):
            at = re.search(r"[mF]", word[1:]).start() + 1
            kind = "-" + word[at]
            value = word[at + 1:] or nxt
        if value is None:
            continue
        if kind in ("-F", "--file"):
            text = read_text(value, seg)
            if text is not None:
                texts.append((value if value != "-" else "the heredoc", text))
        else:
            texts.append(("the message", value))
    return texts


def gh_texts(args, seg):
    texts = [("the " + flag.lstrip("-"), value) for flag, value in flag_values(args, GH_TEXT_FLAGS)]
    for _flag, value in flag_values(args, GH_FILE_FLAGS):
        text = read_text(value, seg)
        if text is not None:
            texts.append((value if value != "-" else "the heredoc", text))
    return texts


def api_texts(args, seg):
    """Texts a gh api call would send, or None when it is a read."""
    method = ""
    for flag, value in flag_values(args, {"-X", "--method"}):
        method = value.upper()
    if method == "GET":
        return None
    fields = []
    for flag, value in flag_values(args, API_FIELD_FLAGS):
        key, _, val = value.partition("=")
        if flag in ("-F", "--field") and val.startswith("@"):
            text = read_text(val[1:], seg)
            fields.append((key, val[1:], text or ""))
        else:
            fields.append((key, "the " + key + " field", val))
    for flag, value in flag_values(args, {"--input"}):
        fields.append(("", value if value != "-" else "the heredoc", read_text(value, seg) or ""))
    if "graphql" in args:
        query = " ".join(text for key, _where, text in fields if key in ("query", ""))
        if not re.search(r"\bmutation\b", query):
            return None
    elif not fields and method not in ("POST", "PATCH", "PUT"):
        return None
    return [(where, text) for _key, where, text in fields]


def published(seg):
    """Texts this segment would publish, or None when it writes nothing public."""
    call = hookkit.git_call(seg)
    if call and call[0] in GIT_WRITES:
        return git_texts(call[1], seg)
    gh = hookkit.gh_call(seg)
    if not gh:
        return None
    args = gh[0]
    if args[:1] and args[0] in GH_GROUPS and args[1:2] and args[1] in GH_WRITES:
        return gh_texts(args[2:], seg)
    if args[:1] == ["api"]:
        return api_texts(args[1:], seg)
    return None


def prose(text):
    text = FENCE.sub("", text)
    text = INLINE_CODE.sub("", text)
    return REVERT_SUBJECT.sub("", text)


def problems(command, cwd="."):
    found = []
    for seg in hookkit.segments(command, cwd):
        texts = published(seg)
        if texts is None:
            continue
        if not hookkit.disabled("LEAN_ALLOW_ATTRIBUTION", seg):
            for where, text in [("the command", command)] + texts:
                found += [f"{kind}: {where} contains {what}" for kind, what in scan(text, slop=False)]
        if not hookkit.disabled("LEAN_ALLOW_SLOP", seg):
            for where, text in texts:
                found += [f"{kind}: {where} contains {what}"
                          for kind, what in scan(prose(text), attribution=False)]
    return list(dict.fromkeys(found))


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
        "git commit -F - <<'EOF'\nadd a rob" + "ust retry\nEOF",
        "git commit -F - <<'EOF'\nfix\n\nClaude-" + "Session: x\nEOF",
        "git commit -am 'a rob" + "ust retry'",
        "/usr/bin/git commit -m 'a rob" + "ust retry'",
        "nice git commit -m 'a rob" + "ust retry'",
        "LEAN_ALLOW_ATTRIBUTION=1 git commit -m 'a rob" + "ust retry'",
        "LEAN_ALLOW_SLOP=1 git commit -m 'x' -m 'Claude-" + "Session: x'",
        "gh pr create --title t --body 'a rob" + "ust fix, see `code`'",
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
        "git status; grep rob" + "ust CHANGELOG.md; git commit -m x",
        "git commit -m x -- docs/rob" + "ust.md",
        "git commit -m x && echo '\u2014'",
        "git commit -m 'the hook blocks the word `rob" + "ust`'",
        "gh pr create --title t --body 'output:\n```\nstep 1 \u2014 ok\n```'",
        "git commit -m 'Revert \"add a rob" + "ust retry\"' -m 'This reverts commit abc.'",
        "LEAN_ALLOW_SLOP=1 git commit -m 'a rob" + "ust retry'",
        "git add . # a rob" + "ust comment\ngit commit -m x",
        "git tag -l 'rob" + "ust*'",
        "gh api repos/o/r/issues -F per_page=100 --jq '.[].title | select(test(\"rob" + "ust\"))'",
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
        os.makedirs(os.path.join(tmp, "sub"))
        with open(os.path.join(tmp, "sub", "msg.txt"), "w") as fh:
            fh.write("a rob" + "ust retry\n")
        with open(os.path.join(tmp, "q.graphql"), "w") as fh:
            fh.write('mutation { addComment(body: "a seam' + 'less fix") }\n')
        with open(os.path.join(tmp, "read.graphql"), "w") as fh:
            fh.write('{ search(query: "rob' + 'ust") { issueCount } }\n')
        for cmd in (f"gh pr create --body-file {body}", f"git commit -F {body}",
                    f"gh release create v1 --notes-file={slop}", "git commit -F body.md",
                    "gh api repos/o/r/issues/1/comments -F body=@body.md",
                    f"gh api repos/o/r/issues/1/comments --input {slop}",
                    "gh api repos/o/r/issues/1/comments --input=notes.md",
                    "cd sub && git commit -F msg.txt",
                    "gh api graphql -F query=@q.graphql",
                    "gh api graphql --input q.graphql"):
            if not problems(cmd, cwd=tmp):
                print(f"FAIL file not checked: {cmd!r}")
                failures += 1
        if problems("gh api graphql -F query=@read.graphql", cwd=tmp):
            print("FAIL a graphql query read from a file was treated as a write")
            failures += 1
    print("self-test: ok" if failures == 0 else f"self-test: {failures} failed")
    return 1 if failures else 0


def main():
    if sys.argv[1:] == ["--self-test"]:
        return self_test()
    if hookkit.disabled("LEAN_ALLOW_ATTRIBUTION") and hookkit.disabled("LEAN_ALLOW_SLOP"):
        return 0
    event = hookkit.read_event()
    command = hookkit.tool_input(event).get("command") or ""
    found = problems(command, event.get("cwd") or os.getcwd())
    if found:
        print("Blocked by lean-agent, public text needs fixing:\n  "
              + "\n  ".join(found)
              + "\nRewrite it plainly (commas, periods or parentheses for dashes, "
              "words for arrows, no attribution) and run the command again. If the text is "
              "right as it is, LEAN_ALLOW_SLOP=1 or LEAN_ALLOW_ATTRIBUTION=1 in front of the "
              "command, or in the env block of Claude Code settings, turns that check off.",
              file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        sys.exit(self_test())
    hookkit.run(main)

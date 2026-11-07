#!/usr/bin/env python3
"""Block agent attribution and AI slop in commits, pull requests, issues and releases.

Runs as a Claude Code PreToolUse hook on Bash. Reads the hook JSON on stdin,
and when the command is a git commit, tag or notes write, a pull request, merge
request, issue or release write on any host (gh, glab, az), or a gh or glab api
write, checks the text it would publish. Exit 2 blocks the call and tells the
agent what to fix. The commands and flags of each host are one table in
lib/hookkit.py.

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
any `cd`. The word and pattern lists are the rule sets in rules/, loaded by lib/rules.py.

  LEAN_SKIP_ATTRIBUTION=1    skip the attribution check
  LEAN_SKIP_SLOP=1           skip the slop check
  LEAN_HOOK_PUBLIC_TEXT=off  turn the whole hook off
  LEAN_RULES=-attribution    drop the attribution set (a user setting only)
Any of them can be set in the environment or in the `env` block of your user
Claude Code settings. Inline in front of the command, or in a repository's own
settings, they are ignored: this hook is a guard.

  public-text.py --self-test
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "lib"))
import hookkit  # noqa: E402
import rules  # noqa: E402
from house_style import scan_sets  # noqa: E402

HOOK = {
    "events": [
        {"event": "PreToolUse", "matcher": "Bash", "group": 1, "order": 3},
    ],
    "timeout": 10,
    "mode": "process",
    "pack": "core",
    "guard": True,
    "default": {"strict": "block", "standard": "note"},
}

GIT_WRITES = {"commit", "tag", "notes"}
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


def forge_texts(call, seg):
    """Texts a pull request, merge request, issue or release command of any host would publish."""
    flags = hookkit.FORGE_FLAGS[call.cli]
    texts = [("the " + flag.lstrip("-"), value) for flag, value in hookkit.forge_flag_values(call, flags["text"])]
    for _flag, value in hookkit.forge_flag_values(call, flags["file"]):
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
    call = hookkit.forge_call(seg)
    if not call:
        return None
    if call.is_api:
        return api_texts(call.args[1:], seg)
    if call.action and call.action != "ready":
        return forge_texts(call, seg)
    return None


def prose(text):
    text = FENCE.sub("", text)
    text = INLINE_CODE.sub("", text)
    return REVERT_SUBJECT.sub("", text)


def loaded_sets():
    """The rule sets, with attribution put back when a repository's own settings dropped it."""
    sets = rules.load()
    if not any(s["name"] == "attribution" for s in sets) and hookkit.from_repo("LEAN_RULES"):
        sets = rules.load(["attribution"]) + sets
        hookkit.guard_note("LEAN_RULES", "this repo's .claude/settings.json")
    return sets


def problems(command, cwd="."):
    found = []
    sets = None
    for seg in hookkit.segments(command, cwd):
        texts = published(seg)
        if texts is None or hookkit.off(seg):
            continue
        if sets is None:
            sets = loaded_sets()
        if not hookkit.disabled("LEAN_SKIP_ATTRIBUTION", seg):
            for where, text in [("the command", command)] + texts:
                found += [f"{kind}: {where} contains {what}"
                          for kind, what in scan_sets(text, sets, slop=False)]
        if not hookkit.disabled("LEAN_SKIP_SLOP", seg):
            for where, text in texts:
                found += [f"{kind}: {where} contains {what}"
                          for kind, what in scan_sets(prose(text), sets, attribution=False)]
    return list(dict.fromkeys(found))


def self_test():
    os.environ["LEAN_PROFILE"] = "strict"
    import tempfile

    blocked = [
        'git commit -m "fix it\n\nCo-Authored-By: Claude <noreply@anthropic.com>"',
        'git commit -m "x" -m "Claude-Session: https://claude.ai/code/session_1"',
        'gh pr create --title t --body "done\n\nGenerated with [Claude Code](https://x)"',
        'gh issue comment 5 --body "see https://claude.ai/code/session_abc"',
        'gh issue create --title t --body "Simulation page: https://claude.ai/code/' + 'artifact/05af"',
        "gh pr create --title t --body 'the plan: `https://claude.ai/code/" + "artifact/cb6f`'",
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
        "LEAN_SKIP_ATTRIBUTION=1 git commit -m 'a rob" + "ust retry'",
        "LEAN_SKIP_SLOP=1 git commit -m 'x' -m 'Claude-" + "Session: x'",
        # a guard: a loosening in the command's own prefix is ignored, so these block (with the guard note)
        "LEAN_ALLOW_SLOP=1 git commit -m 'a rob" + "ust retry'",
        "LEAN_SKIP_SLOP=1 git commit -m 'a rob" + "ust retry'",
        "LEAN_HOOK_PUBLIC_TEXT=off git commit -m 'a rob" + "ust retry'",
        "LEAN_HOOK_PUBLIC_TEXT=off git commit -m 'x' -m 'Claude-" + "Session: x'",
        "LEAN_ALLOW_SLOP=1 glab mr create --description 'a rob" + "ust retry'",
        "LEAN_SKIP_SLOP=1 glab mr create --description 'a rob" + "ust retry'",
        "gh pr create --title t --body 'a rob" + "ust fix, see `code`'",
        "gh pr merge 4 --squash --body 'a rob" + "ust retry'",
        'glab mr create --title t --description "fast \u2014 and safe"',
        'glab mr create --title t -d "done\n\nGenerated with [Claude Co' + 'de](https://x)"',
        "glab mr create --title=t --description='a rob" + "ust retry'",
        "glab mr update 3 --description 'old \u2192 new'",
        "glab mr note 3 -m 'see https://claude.ai/code/" + "session_abc'",
        "glab mr note create 3 --message 'Done. Addition" + "ally, it logs.'",
        "glab issue create --title t --description 'a seam" + "less fix'",
        "glab release create v1 --notes 'a compre" + "hensive release'",
        "glab api projects/1/merge_requests/2/notes -f body='a seam" + "less fix'",
        "glab api -X PUT projects/1/merge_requests/2 --raw-field 'description=old \u2192 new'",
        'az repos pr create --title t --description "fast \u2014 and safe"',
        "az repos pr create --title t --description 'one line' 'a rob" + "ust retry' --auto-complete true",
        "az repos pr create --title 'a seam" + "less fix' --description ok",
        "az repos pr create --title t --description '- a rob" + "ust fix'",
        "az repos pr create --title t --description 'first line' '- a rob" + "ust fix'",
        "az repos pr create --title t --description '-1 rob" + "ust'",
        "az repos pr create --title t --description='- a rob" + "ust fix'",
        "az repos pr update --id 3 --description 'old \u2192 new'",
        "az repos pr update --id 3 --status completed --description 'a compre" + "hensive fix'",
        "az repos pr create --description='a rob" + "ust retry' --title t",
        "az boards work-item create --title t --description 'a rob" + "ust retry'",
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
        "git add . # a rob" + "ust comment\ngit commit -m x",
        "git tag -l 'rob" + "ust*'",
        "gh api repos/o/r/issues -F per_page=100 --jq '.[].title | select(test(\"rob" + "ust\"))'",
        "glab mr create --title t --description 'the hook blocks a `Claude-Session:` trailer'",
        "glab mr list --search 'rob" + "ust'",
        "glab api -X GET projects/1/issues -f search='rob" + "ust'",
        "glab api projects/1/merge_requests",
        "az repos pr list --status active",
        "az repos pr create --title t --description 'wait for the database before the suite'",
        "az repos pr create --title t && echo - a rob" + "ust fix",
        "az repos pr update --id 3 --auto-complete true",
        "az repos pr show --id 3 --query title",
        "echo glab mr create --description 'a rob" + "ust retry'",
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
                    f"glab mr create --notes-file {slop}", f"glab release create v1 --notes-file={slop}",
                    "glab api projects/1/notes -F body=@body.md",
                    "gh api graphql -F query=@q.graphql",
                    "gh api graphql --input q.graphql"):
            if not problems(cmd, cwd=tmp):
                print(f"FAIL file not checked: {cmd!r}")
                failures += 1
        if problems("gh api graphql -F query=@read.graphql", cwd=tmp):
            print("FAIL a graphql query read from a file was treated as a write")
            failures += 1
    def std(event, **env):
        return hookkit.probe(os.path.realpath(__file__), event, LEAN_PROFILE="standard", **env)

    def want(label, got, rc, out=False):
        nonlocal failures
        if got[0] != rc or bool(got[1]) != out or got[2]:
            print(f"FAIL standard {label}: {got!r}")
            failures += 1

    with tempfile.TemporaryDirectory() as there:
        slop = {"tool_input": {"command": 'git commit -m "a rob' + 'ust retry"'}, "cwd": there}
        want("slop is a note", std(slop), 0, True)
        clear = {"LEAN_HOOK_PUBLIC_TEXT": None, "LEAN_SKIP_SLOP": None, "LEAN_SKIP_ATTRIBUTION": None,
                 "LEAN_ALLOW_SLOP": None, "LEAN_ALLOW_ATTRIBUTION": None}
        got = hookkit.probe(os.path.realpath(__file__), slop, LEAN_PROFILE="strict", **clear)
        if got[0] != 2 or "LEAN_SKIP_SLOP=1" not in got[2] or "LEAN_ALLOW" in got[2]:
            print(f"FAIL strict message should name only the new switches: {got!r}")
            failures += 1
        got = hookkit.probe(os.path.realpath(__file__), slop, LEAN_PROFILE="strict",
                            **dict(clear, LEAN_HOOK_PUBLIC_TEXT="off"))
        if got[0] != 0 or got[2]:
            print(f"FAIL LEAN_HOOK_PUBLIC_TEXT=off in the environment should turn the hook off: {got!r}")
            failures += 1

    import subprocess

    me = os.path.realpath(__file__)
    clean_env = {"LEAN_HOOK_PUBLIC_TEXT": None, "LEAN_SKIP_SLOP": None, "LEAN_SKIP_ATTRIBUTION": None,
                 "LEAN_RULES": None}
    with tempfile.TemporaryDirectory() as there:
        plain = {"tool_input": {"command": 'git commit -m "wait for database health"'}, "cwd": there}
        got = hookkit.probe(me, plain, LEAN_PROFILE="strict", **dict(clean_env, LEAN_RULES="nope"))
        if got[0] != 0:
            print(f"FAIL LEAN_RULES=nope should leave a clean message alone: {got!r}")
            failures += 1

        def repo_with(env):
            root = tempfile.mkdtemp(prefix="repo-", dir=there)
            subprocess.run(["git", "-C", root, "init", "-q"], check=True, capture_output=True)
            os.makedirs(os.path.join(root, ".claude"))
            with open(os.path.join(root, ".claude", "settings.json"), "w", encoding="utf-8") as fh:
                json.dump({"env": env}, fh)
            return root

        attr = 'git commit -m "x" -m "Co-Authored-By: Cla' + 'ude <noreply@example.com>"'
        for label, repo_env, want_code, want_note in (
                ("a repo dropping attribution is ignored", {"LEAN_RULES": "-attribution"}, 2, True),
                ("a user dropping attribution is kept", {}, 0, False)):
            root = repo_with(repo_env)
            event = {"tool_input": {"command": attr}, "cwd": root, "session_id": f"rules-{os.getpid()}-{want_code}"}
            got = hookkit.probe(me, event, LEAN_PROFILE="strict", CLAUDE_PROJECT_DIR=root,
                                **dict(clean_env, LEAN_RULES="-attribution"))
            if got[0] != want_code or ("LEAN_RULES from this repo" in got[2]) != want_note:
                print(f"FAIL {label}: {got!r}")
                failures += 1

    print("self-test: ok" if failures == 0 else f"self-test: {failures} failed")
    return 1 if failures else 0


def main():
    if sys.argv[1:] == ["--self-test"]:
        return self_test()
    if hookkit.disabled("LEAN_SKIP_ATTRIBUTION") and hookkit.disabled("LEAN_SKIP_SLOP"):
        return 0
    event = hookkit.read_event()
    command = hookkit.tool_input(event).get("command") or ""
    found = problems(command, event.get("cwd") or os.getcwd())
    if found:
        text = ("Blocked by lean-agent, public text needs fixing:\n  "
                + "\n  ".join(found)
                + "\nRewrite it plainly (commas, periods or parentheses for dashes, "
                "words for arrows, no attribution) and run the command again. If the text is "
                "right as it is, set LEAN_SKIP_SLOP=1 or LEAN_SKIP_ATTRIBUTION=1 in the env block of "
                "your own settings (~/.claude/settings.json) to turn that check off.")
        note = ("This public text may read badly:\n  " + "\n  ".join(found)
                + "\nConsider rewriting it plainly (commas, periods or parentheses for dashes, "
                "words for arrows, no attribution) before it goes out.")
        return hookkit.refuse("PreToolUse", text, note=note)
    return 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        sys.exit(self_test())
    hookkit.run(main)

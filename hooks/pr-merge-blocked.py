#!/usr/bin/env python3
"""When `gh pr merge` fails, point the agent at pr-blockers.sh instead of a one-blocker-at-a-time hunt.

A refused merge was once diagnosed across four tool calls on a pull request that added one
documentation file. The causes were independent facts that one script reads together.

Registered for Bash on PostToolUseFailure (a nonzero exit, which is how a refused merge usually
ends) and on PostToolUse (the same refusal when the command's exit code was hidden, for example by
`|| true` or a pipe). Never blocks: it prints a note as additionalContext and exits 0.

  LEAN_SKIP_PR_MERGE_BLOCKED=1   turn it off
  pr-merge-blocked.py --self-test
"""
import json
import os
import re
import shlex
import sys

REFUSAL = re.compile(
    r"not mergeable|base branch policy|protected branch|required status check|GH006|GH013"
    r"|merge strategy for .* is set by the merge queue|review is required|changes requested"
    r"|repository rule violations|pull request is not in a mergeable state|merge commits are not allowed"
    r"|squash merges are not allowed|failed to merge|could not merge",
    re.IGNORECASE,
)
SEPARATORS = {";", "&&", "||", "|", "&", "(", ")"}


def merge_args(command):
    """The arguments of the first real `gh pr merge` call, or None. Tokenised, so quoted text is not a call."""
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars=";&|()")
        lex.whitespace_split = True
        tokens = list(lex)
    except ValueError:
        return None
    start = True
    for i, t in enumerate(tokens):
        if t in SEPARATORS:
            start = True
            continue
        if start and tokens[i:i + 3] == ["gh", "pr", "merge"]:
            args = []
            for a in tokens[i + 3:]:
                if a in SEPARATORS:
                    break
                args.append(a)
            return args
        start = start and "=" in t and not t.startswith("-")
    return None


def response_text(resp):
    if isinstance(resp, str):
        return resp
    if isinstance(resp, dict):
        parts = [str(resp.get(k) or "") for k in ("stdout", "stderr", "text", "output", "error")]
        return "\n".join(parts)
    if isinstance(resp, list):
        return "\n".join(response_text(r) for r in resp)
    return ""


def exit_code(resp):
    if isinstance(resp, dict):
        for k in ("exit_code", "exitCode", "returnCode", "code"):
            v = resp.get(k)
            if isinstance(v, int):
                return v
    return None


def merge_target(args):
    num = repo = None
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-R", "--repo") and i + 1 < len(args):
            repo = args[i + 1]
            i += 2
            continue
        if a.startswith("--repo="):
            repo = a.split("=", 1)[1]
        elif num is None and re.fullmatch(r"#?\d+", a):
            num = a.lstrip("#")
        elif num is None:
            u = re.search(r"github\.com/([^/\s]+/[^/\s]+)/pull/(\d+)", a)
            if u:
                repo, num = repo or u.group(1), u.group(2)
        i += 1
    return num, repo


def note_for(event):
    if os.environ.get("LEAN_SKIP_PR_MERGE_BLOCKED") == "1":
        return None
    if event.get("tool_name") != "Bash":
        return None
    command = (event.get("tool_input") or {}).get("command") or ""
    args = merge_args(command)
    if args is None:
        return None
    name = event.get("hook_event_name") or "PostToolUse"
    text = response_text(event.get("tool_response")) + "\n" + str(event.get("error") or "")
    failed = name == "PostToolUseFailure"
    code = exit_code(event.get("tool_response"))
    if code not in (None, 0):
        failed = True
    if not failed and not REFUSAL.search(text):
        return None
    num, repo = merge_target(args)
    cmd = "pr-blockers.sh %s%s" % (num or "<pr-number>", " -R " + repo if repo else "")
    msg = (
        "That merge did not go through. Do not diagnose it one blocker at a time. Run once:\n\n"
        "    %s\n\n"
        "It lists failing or missing required checks, runs waiting at action_required, unresolved "
        "review threads, requested changes, whether the base uses a merge queue (and so which "
        "`gh pr merge` form works), ruleset rules such as extra approval for an unattributed "
        "author, and whether the head is behind. It is read-only. Read each review thread before "
        "resolving it." % cmd
    )
    return {"hookSpecificOutput": {"hookEventName": name, "additionalContext": msg}}


def self_test():
    fails = []

    def case(label, event, want, env=None):
        old = dict(os.environ)
        os.environ.pop("LEAN_SKIP_PR_MERGE_BLOCKED", None)
        os.environ.update(env or {})
        try:
            got = note_for(event)
        finally:
            os.environ.clear()
            os.environ.update(old)
        text = json.dumps(got) if got else ""
        if (want is None and got) or (want is not None and want not in text):
            fails.append("%s: want %r, got %r" % (label, want, text[:200]))

    def ev(cmd, resp, name="PostToolUse", **kw):
        e = {"tool_name": "Bash", "hook_event_name": name, "tool_input": {"command": cmd}, "tool_response": resp}
        e.update(kw)
        return e

    case("failure event", ev("gh pr merge 12 --squash", None, "PostToolUseFailure", error="Exit code 1"), "pr-blockers.sh 12")
    case("event name echoed", ev("gh pr merge 12", None, "PostToolUseFailure", error="x"), '"hookEventName": "PostToolUseFailure"')
    case("repo flag", ev("gh pr merge 5 -R o/r --squash", None, "PostToolUseFailure"), "pr-blockers.sh 5 -R o/r")
    case("url form", ev("gh pr merge https://github.com/o/r/pull/8", None, "PostToolUseFailure"), "pr-blockers.sh 8 -R o/r")
    case("hidden exit, refusal text", ev("gh pr merge 3 || true", {"stdout": "", "stderr": "X Pull request o/r#3 is not mergeable: the base branch policy prohibits the merge."}), "pr-blockers.sh 3")
    case("queue strategy refusal", ev("gh pr merge 4 --squash | cat", "The merge strategy for main is set by the merge queue"), "pr-blockers.sh 4")
    case("nonzero exit field", ev("gh pr merge 6", {"stdout": "", "stderr": "", "exit_code": 1}), "pr-blockers.sh 6")
    case("success is quiet", ev("gh pr merge 3 --squash", {"stdout": "Merged pull request #3", "stderr": ""}), None)
    case("other command quiet", ev("gh pr view 3", None, "PostToolUseFailure"), None)
    case("echo of the words quiet", ev("echo 'run gh pr merge later'", None, "PostToolUseFailure"), None)
    case("not bash quiet", dict(ev("gh pr merge 1", None, "PostToolUseFailure"), tool_name="Read"), None)
    case("after cd", ev("cd repo && gh pr merge 2", None, "PostToolUseFailure"), "pr-blockers.sh 2")
    case("env prefix", ev("GH_REPO=o/r gh pr merge 2", None, "PostToolUseFailure"), "pr-blockers.sh 2")
    case("skip env", ev("gh pr merge 1", None, "PostToolUseFailure"), None, {"LEAN_SKIP_PR_MERGE_BLOCKED": "1"})
    if fails:
        print("self-test failed:\n  " + "\n  ".join(fails))
        return 1
    print("self-test passed")
    return 0


def main():
    if sys.argv[1:] == ["--self-test"]:
        return self_test()
    try:
        event = json.load(sys.stdin)
    except Exception:
        return 0
    out = note_for(event)
    if out:
        print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())

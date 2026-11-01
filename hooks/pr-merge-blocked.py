#!/usr/bin/env python3
"""When a merge fails (`gh pr merge`, `glab mr merge`, `az repos pr update --status completed`), point the agent at pr-blockers.sh instead of a one-blocker-at-a-time hunt.

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

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "lib"))
import hookkit  # noqa: E402

REFUSAL = re.compile(
    r"not mergeable|base branch policy|protected branch|required status check|GH006|GH013"
    r"|merge strategy for .* is set by the merge queue|review is required|changes requested"
    r"|repository rule violations|pull request is not in a mergeable state|merge commits are not allowed"
    r"|squash merges are not allowed|failed to merge|could not merge"
    r"|cannot be merged|pipeline must succeed|discussions must be resolved|TF4011\d\d|TF4012\d\d|branch policy|polic(?:y|ies) .*(?:block|reject|fail)",
    re.IGNORECASE,
)
SEPARATORS = {";", "&&", "||", "|", "&", "(", ")"}


def merge_args(command):
    """(kind, arguments) of the first real merge call on any host, or None. Tokenised, so quoted text is not a call.

    The commands that count as a merge are the table in lib/hookkit.py.
    """
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
        if start and os.path.basename(t) in hookkit.FORGE_CLIS:
            words = []
            for a in tokens[i + 1:]:
                if a in SEPARATORS:
                    break
                words.append(a)
            call = hookkit.ForgeCall(os.path.basename(t), words, ".")
            if hookkit.forge_action(call) == "merge":
                return call.kind, call.rest
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


def merge_target(kind, args):
    """(number, repo) a merge call names. Azure names no repository: its repo comes from the clone."""
    num = repo = None
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-R", "--repo") and i + 1 < len(args) and kind != "azure":
            repo = args[i + 1]
            i += 2
            continue
        if kind == "azure" and a == "--id" and i + 1 < len(args):
            num = args[i + 1].lstrip("#") if re.fullmatch(r"#?\d+", args[i + 1]) else num
            i += 2
            continue
        if a.startswith("--repo=") and kind != "azure":
            repo = a.split("=", 1)[1]
        elif a.startswith("--id=") and kind == "azure" and re.fullmatch(r"\d+", a[5:]):
            num = a[5:]
        elif kind != "azure" and num is None and re.fullmatch(r"#?\d+", a):
            num = a.lstrip("#")
        elif kind == "github" and num is None:
            u = re.search(r"github\.com/([^/\s]+/[^/\s]+)/pull/(\d+)", a)
            if u:
                repo, num = repo or u.group(1), u.group(2)
        elif kind == "gitlab" and num is None:
            u = re.fullmatch(r"(https?://[^/\s]+/[^\s]+?)/-/merge_requests/(\d+)/?", a)
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
    found = merge_args(command)
    if found is None:
        return None
    kind, args = found
    name = event.get("hook_event_name") or "PostToolUse"
    text = response_text(event.get("tool_response")) + "\n" + str(event.get("error") or "")
    failed = name == "PostToolUseFailure"
    code = exit_code(event.get("tool_response"))
    if code not in (None, 0):
        failed = True
    if not failed and not REFUSAL.search(text):
        return None
    num, repo = merge_target(kind, args)
    cmd = "%s %s%s" % (hookkit.tool_path("pr-blockers.sh"), num or "<pr-number>", " -R " + repo if repo else "")
    msg = (
        "That merge did not go through. Do not diagnose it one blocker at a time. Run once:\n\n"
        "    %s\n\n"
        "It lists failing or missing required checks, runs waiting at action_required, unresolved "
        "review threads, requested changes, whether the base uses a merge queue (and so which "
        "merge form works), ruleset rules such as extra approval for an unattributed "
        "author, and whether the head is behind. On GitLab and Azure DevOps it reads the merge "
        "status, pipeline or policy results and open discussions. It is read-only. Read each review thread before "
        "resolving it." % cmd
    )
    return {"hookSpecificOutput": {"hookEventName": name, "additionalContext": msg}}


def self_test():
    os.environ["LEAN_PROFILE"] = "strict"
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

    case("failure event", ev("gh pr merge 12 --squash", None, "PostToolUseFailure", error="Exit code 1"), hookkit.tool_path("pr-blockers.sh") + " 12")
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
    case("glab merge", ev("glab mr merge 7 --squash", None, "PostToolUseFailure"), hookkit.tool_path("pr-blockers.sh") + " 7")
    case("glab repo flag", ev("glab mr merge 7 -R g/sub/r", None, "PostToolUseFailure"), "pr-blockers.sh 7 -R g/sub/r")
    case("glab url form", ev("glab mr merge https://gitlab.com/g/r/-/merge_requests/9", None, "PostToolUseFailure"), "pr-blockers.sh 9 -R https://gitlab.com/g/r")
    case("glab refusal text", ev("glab mr merge 3 || true", {"stdout": "", "stderr": "merge request cannot be merged: pipeline must succeed"}), "pr-blockers.sh 3")
    case("glab success is quiet", ev("glab mr merge 3", {"stdout": "Merged", "stderr": ""}), None)
    case("glab other command quiet", ev("glab mr view 3", None, "PostToolUseFailure"), None)
    case("glab echo quiet", ev("echo 'glab mr merge 3'", None, "PostToolUseFailure"), None)
    case("glab after cd", ev("cd repo && glab mr merge 2", None, "PostToolUseFailure"), "pr-blockers.sh 2")
    case("az merge", ev("az repos pr update --id 5 --status completed", None, "PostToolUseFailure"), hookkit.tool_path("pr-blockers.sh") + " 5")
    case("az merge, id after status", ev("az repos pr update --status completed --id 6 --org https://dev.azure.com/o", None, "PostToolUseFailure"), "pr-blockers.sh 6\\n")
    case("az refusal text", ev("az repos pr update --id 4 --status completed | cat", "TF401179: policy blocked the completion"), "pr-blockers.sh 4")
    case("az update without completing is quiet", ev("az repos pr update --id 5 --title t", None, "PostToolUseFailure"), None)
    case("az pr list is quiet", ev("az repos pr list", None, "PostToolUseFailure"), None)
    import sqlite3
    import subprocess
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "lean.db")
        event = ev("gh pr merge 12 --squash", None, "PostToolUseFailure", error="Exit code 1",
                   session_id="s1", cwd="/w")
        env = dict(os.environ, LEAN_DB=db)
        env.pop("LEAN_STATS", None)
        env.pop("LEAN_SKIP_PR_MERGE_BLOCKED", None)
        proc = subprocess.run([sys.executable, os.path.realpath(__file__)], input=json.dumps(event),
                              capture_output=True, text=True, env=env, timeout=30)
        if proc.returncode != 0 or "pr-blockers.sh 12" not in proc.stdout:
            fails.append("hook run: exit %r, stdout %r" % (proc.returncode, proc.stdout[:200]))
        rows = []
        if os.path.exists(db):
            rows = sqlite3.connect(db).execute("SELECT hook, outcome, session_id, cwd FROM hook_fires").fetchall()
        if rows != [("pr-merge-blocked.py", "note", "s1", "/w")]:
            fails.append("fire not recorded: %r" % (rows,))
        proc = subprocess.run([sys.executable, os.path.realpath(__file__)], input="not json",
                              capture_output=True, text=True, env=env, timeout=30)
        if proc.returncode != 0 or proc.stdout:
            fails.append("bad stdin: exit %r, stdout %r" % (proc.returncode, proc.stdout[:200]))
    if fails:
        print("self-test failed:\n  " + "\n  ".join(fails))
        return 1
    print("self-test passed")
    return 0


def main():
    out = note_for(hookkit.read_event())
    if out:
        hookkit.note(out["hookSpecificOutput"]["hookEventName"], out["hookSpecificOutput"]["additionalContext"])
    return 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        sys.exit(self_test())
    hookkit.run(main)

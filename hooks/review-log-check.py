#!/usr/bin/env python3
"""Refuse a push or pull request when the branch has no row in the review log.

PreToolUse hook on Bash, for git push, gh pr create and az repos pr create. A review that
was done but never logged leaves the retro with no data for that change. The log is
$REVIEW_LOG, else review-log.tsv at the repo root. Without a log file the repo does not use
the review log and the hook does nothing. It looks in the pr column (third) for the branch
name, or the branch name with / written as -. The default branch (main, master) and a
detached HEAD pass. Any error reading git or the file passes.

  LEAN_REVIEW_LOG_CHECK=off   turn it off (environment, settings env, or inline)
  review-log-check.py --self-test
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "lib"))
import hookkit  # noqa: E402

DEFAULT_BRANCHES = {"main", "master"}
DENY = (
    "Branch {branch} has no row in the review log ({log}). Log the review with "
    "`${{CLAUDE_PLUGIN_ROOT}}/skills/adversarial-review/log-review.sh <repo> {branch} <confirmed> "
    "<refuted> <blockers> \"<categories>\" \"<caught elsewhere first>\"` and commit the row. "
    "If the change was deliberately not reviewed, set LEAN_REVIEW_LOG_CHECK=off for this command.")


def is_push(seg):
    call = hookkit.git_call(seg)
    if call and call[0] == "push":
        return True
    gh = hookkit.gh_call(seg)
    if gh and gh[0][:2] == ["pr", "create"]:
        return True
    words = seg.words
    return bool(words) and os.path.basename(words[0]) == "az" and words[1:4] == ["repos", "pr", "create"]


def is_off(seg=None):
    value = os.environ.get("LEAN_REVIEW_LOG_CHECK")
    if value is None and seg is not None:
        value = seg.env.get("LEAN_REVIEW_LOG_CHECK")
    return (value or "").lower() == "off"


def logged(path, names):
    with open(path, encoding="utf-8", errors="replace") as fh:
        rows = fh.read().splitlines()[1:]
    for row in rows:
        cols = row.split("\t")
        if len(cols) > 2 and cols[2].strip() in names:
            return True
    return False


def message(command, cwd):
    """The deny text for the first push segment whose branch has no log row, else None."""
    for seg in hookkit.segments(command, cwd):
        if not is_push(seg) or is_off(seg):
            continue
        root = hookkit.git(seg.cwd, "rev-parse", "--show-toplevel")
        if not root:
            continue
        branch = hookkit.git(root, "symbolic-ref", "--short", "-q", "HEAD")
        if not branch or branch in DEFAULT_BRANCHES:
            continue
        path = os.environ.get("REVIEW_LOG") or seg.env.get("REVIEW_LOG") or "review-log.tsv"
        path = hookkit.resolve(path, root)
        if not os.path.isfile(path):
            continue
        try:
            if logged(path, {branch, branch.replace("/", "-")}):
                continue
        except OSError:
            continue
        return DENY.format(branch=branch, log=os.path.relpath(path, root) if path.startswith(root) else path)
    return None


def main():
    if is_off():
        return 0
    event = hookkit.read_event()
    command = hookkit.tool_input(event).get("command")
    if not isinstance(command, str) or not any(w in command for w in ("push", "pr")):
        return 0
    text = message(command, event.get("cwd") or os.getcwd())
    if text:
        print(text, file=sys.stderr)
        return 2
    return 0


def self_test():
    import subprocess
    import tempfile

    failures = 0

    def check(name, got, want):
        nonlocal failures
        if bool(got) != want:
            print(f"FAIL {name}: got {got!r}")
            failures += 1

    for var in ("LEAN_REVIEW_LOG_CHECK", "REVIEW_LOG"):
        os.environ.pop(var, None)
    header = "date\trepo\tpr\tconfirmed\trefuted\tblockers\tcategories\tcaught_elsewhere_first\tpre_push\tci\treview\tescaped\n"
    with tempfile.TemporaryDirectory() as tmp:
        def git(*args):
            subprocess.run(["git", "-C", tmp, "-c", "user.email=t@example.org", "-c", "user.name=t", *args],
                           check=True, capture_output=True)
        git("init", "-q", "-b", "main")
        with open(os.path.join(tmp, "a.txt"), "w") as fh:
            fh.write("x\n")
        git("add", ".")
        git("commit", "-q", "-m", "init")
        log = os.path.join(tmp, "review-log.tsv")
        git("checkout", "-q", "-b", "feat/x")
        check("no log file allows", message("git push", tmp), False)
        with open(log, "w") as fh:
            fh.write(header + "2026-01-01\tr\tfeat/other\t1\t0\t0\tc\te\t0\t0\t0\t0\n")
        check("row missing denies git push", message("git push origin HEAD", tmp), True)
        check("row missing denies gh pr create", message("gh pr create --fill", tmp), True)
        check("row missing denies az pr create", message("az repos pr create --title t", tmp), True)
        check("deny names the branch", "feat/x" in (message("git push", tmp) or ""), True)
        check("non-push command allows", message("git status && echo git push", tmp), False)
        check("gh pr view allows", message("gh pr view 3", tmp), False)
        check("inline off allows", message("LEAN_REVIEW_LOG_CHECK=off git push", tmp), False)
        os.environ["LEAN_REVIEW_LOG_CHECK"] = "off"
        check("environment off allows", main_allows(tmp), True)
        del os.environ["LEAN_REVIEW_LOG_CHECK"]
        with open(log, "a") as fh:
            fh.write("2026-01-02\tr\tfeat/x\t1\t0\t0\tc\te\t0\t0\t0\t0\n")
        check("slash form allows", message("git push", tmp), False)
        with open(log, "w") as fh:
            fh.write(header + "2026-01-02\tr\tfeat-x\t1\t0\t0\tc\te\t0\t0\t0\t0\n")
        check("dash form allows", message("git push", tmp), False)
        with open(log, "w") as fh:
            fh.write(header)
        check("empty log denies", message("git push", tmp), True)
        os.environ["REVIEW_LOG"] = os.path.join(tmp, "elsewhere.tsv")
        check("REVIEW_LOG without a file allows", message("git push", tmp), False)
        del os.environ["REVIEW_LOG"]
        git("checkout", "-q", "main")
        check("default branch allows", message("git push", tmp), False)
        git("checkout", "-q", "--detach")
        check("detached head allows", message("git push", tmp), False)
        check("not a repository allows", message("git push", "/"), False)
    print("self-test: ok" if failures == 0 else f"self-test: {failures} failed")
    return 1 if failures else 0


def main_allows(cwd):
    """True when main() exits 0 for a push in cwd with the current environment."""
    import io
    import json
    old = sys.stdin
    sys.stdin = io.StringIO(json.dumps({"tool_input": {"command": "git push"}, "cwd": cwd}))
    try:
        return main() == 0
    finally:
        sys.stdin = old


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        sys.exit(self_test())
    hookkit.run(main)

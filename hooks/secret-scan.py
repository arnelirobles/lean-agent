#!/usr/bin/env python3
"""Block a key or token from reaching git: a commit, a push, or a file write.

PreToolUse hook. It uses the scanner in tools/secret-scan (gitleaks when it is
installed, else the plugin's own patterns) and checks:

  Bash   git commit   the staged diff, plus the unstaged diff for -a, plus the
                      working tree and untracked files when the same command
                      runs git add first
  Bash   git push     the commits that are on no remote yet
  Write, Edit, MultiEdit
                      the text being written, when the file is tracked or would
                      be tracked. A gitignored file such as .env.local is fine.

The message names the file, the line and the pattern, never the value. A line
with `allow-secret` on it is skipped. Each block is recorded as a hook fire in
lean.db, so a weekly count is possible.

  LEAN_SKIP_SECRET_SCAN=1   turn it off, in the environment, in the env block
                            of Claude Code settings, or inline in front of the
                            command
  secret-scan.py --self-test
"""
import importlib.machinery
import importlib.util
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "lib"))
import hookkit  # noqa: E402

SKIP = "LEAN_SKIP_SECRET_SCAN"


def scanner():
    path = os.path.join(hookkit.TOOLS, "secret-scan")
    loader = importlib.machinery.SourceFileLoader("secret_scan_tool", path)
    spec = importlib.util.spec_from_loader("secret_scan_tool", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def commits_all(args):
    """True when git commit is given -a / --all (also inside a cluster such as -am)."""
    for word in args:
        if word == "--all" or (re.fullmatch(r"-[a-zA-Z]+", word) and "a" in word[1:]):
            return True
    return False


def bash_findings(command, cwd, tool):
    """[(what, findings)] for the git commit and git push calls in a command."""
    out = []
    segs = hookkit.segments(command, cwd)
    adds = any((hookkit.git_call(s) or ("",))[0] in ("add", "stage") for s in segs)
    for seg in segs:
        call = hookkit.git_call(seg)
        if not call or hookkit.disabled(SKIP, seg):
            continue
        sub, args, where, _config = call
        if sub == "commit" and "--dry-run" not in args:
            if not hookkit.git(where, "rev-parse", "--git-dir"):
                continue
            if adds:
                found = tool.scan_worktree(where, untracked=True)
            elif commits_all(args):
                found = tool.scan_worktree(where)
            else:
                found = tool.scan_staged(where)
            if found:
                out.append(("the commit", found))
        elif sub == "push" and "--dry-run" not in args:
            if not hookkit.git(where, "rev-parse", "--git-dir"):
                continue
            found = tool.scan_unpushed(where)
            if found:
                out.append(("a commit being pushed", found))
    return out


def written_text(name, data):
    if name == "Write":
        return data.get("content") or ""
    if name == "MultiEdit":
        return "\n".join(str(e.get("new_string") or "") for e in data.get("edits") or [] if isinstance(e, dict))
    return data.get("new_string") or ""


def write_findings(name, data, cwd, tool):
    path = data.get("file_path")
    if not isinstance(path, str) or not path:
        return []
    path = hookkit.resolve(path, cwd)
    folder = os.path.dirname(path)
    while folder and not os.path.isdir(folder):
        parent = os.path.dirname(folder)
        if parent == folder:
            return []
        folder = parent
    if not hookkit.git(folder, "rev-parse", "--git-dir"):
        return []
    if tool.git(folder, "check-ignore", "-q", "--no-index", path) is not None:
        return []
    top = (hookkit.git(folder, "rev-parse", "--show-toplevel") or "").strip()
    label = os.path.relpath(path, top) if top else path
    return tool.scan_text(written_text(name, data), label)


def main():
    if hookkit.disabled(SKIP):
        return 0
    event = hookkit.read_event()
    name = event.get("tool_name")
    data = hookkit.tool_input(event)
    cwd = event.get("cwd") or os.getcwd()
    if name == "Bash":
        command = data.get("command") or ""
        if "git" not in command or not re.search(r"\b(commit|push)\b", command):
            return 0
        tool = scanner()
        blocks = bash_findings(command, cwd, tool)
    elif name in ("Write", "Edit", "MultiEdit"):
        tool = scanner()
        found = write_findings(name, data, cwd, tool)
        blocks = [("this " + name.lower(), found)] if found else []
    else:
        return 0
    if blocks:
        print("\n".join(tool.message(found, what) for what, found in blocks)
              + f"\n{SKIP}=1 in front of the command, or in the env block of Claude Code settings, turns this check off.",
              file=sys.stderr)
        return 2
    return 0


def self_test():
    import json
    import shutil
    import sqlite3
    import subprocess
    import tempfile

    failures = 0

    def check(label, got, want):
        nonlocal failures
        if got != want:
            print(f"FAIL {label}: got {got!r}, want {want!r}")
            failures += 1

    tool = scanner()
    keys = tool.fake_keys()
    google = keys["google-api-key (AIza)"]
    npm = keys["npm-token (npm_)"]
    tmp = tempfile.mkdtemp(prefix="secret-hook-")
    try:
        env = dict(os.environ, HOME=tmp, LEAN_DB=os.path.join(tmp, "lean.db"), GIT_CONFIG_NOSYSTEM="1",
                   LEAN_SECRET_SCAN_HOME=os.path.join(tmp, "home"))
        env.pop(SKIP, None)
        env.pop("LEAN_STATS", None)
        here = os.path.realpath(__file__)

        def sh(*argv, cwd=None):
            return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, env=env)

        def hook(tool_name, tool_input, cwd, extra=None):
            e = dict(env, **(extra or {}))
            event = {"tool_name": tool_name, "tool_input": tool_input, "cwd": cwd, "session_id": "t1"}
            return subprocess.run([sys.executable, here], input=json.dumps(event), capture_output=True,
                                  text=True, env=e)

        def bash(command, cwd, extra=None):
            return hook("Bash", {"command": command}, cwd, extra)

        repo = os.path.join(tmp, "repo")
        sh("git", "init", "-q", repo)
        sh("git", "-C", repo, "config", "user.email", "t@example.org")
        sh("git", "-C", repo, "config", "user.name", "t")

        def write(rel, text):
            with open(os.path.join(repo, rel), "w") as f:
                f.write(text)

        write(".gitignore", ".env.local\n")
        write("ok.txt", "fine\n")
        sh("git", "-C", repo, "add", ".")
        check("clean commit passes", bash("git commit -m x", repo).returncode, 0)
        sh("git", "-C", repo, "commit", "-qm", "base")

        write("app.js", "const k = '" + google + "';\n")
        sh("git", "-C", repo, "add", "app.js")
        p = bash("git commit -m x", repo)
        check("staged key blocks the commit", p.returncode, 2)
        check("message names file, line, pattern", "app.js:1  google-api-key (AIza)" in p.stderr, True)
        check("message never prints the value", google in p.stderr + p.stdout, False)
        check("unrelated command passes", bash("git status", repo).returncode, 0)
        check("--dry-run passes", bash("git commit --dry-run", repo).returncode, 0)
        check("skip variable inline", bash("LEAN_SKIP_SECRET_SCAN=1 git commit -m x", repo).returncode, 0)
        check("skip variable in env", bash("git commit -m x", repo, {SKIP: "1"}).returncode, 0)
        check("cd into the repo is followed", bash(f"cd {repo} && git commit -m x", tmp).returncode, 2)
        check("git -C is followed", bash(f"git -C {repo} commit -m x", tmp).returncode, 2)
        check("outside a repository passes", bash("git commit -m x", tmp).returncode, 0)

        # the same key staged, then held back: -a and git add chains
        sh("git", "-C", repo, "reset", "-q")
        check("unstaged key, plain commit passes", bash("git commit -m x", repo).returncode, 0)
        sh("git", "-C", repo, "add", "-N", "app.js")
        check("unstaged key, commit -a blocks", bash("git commit -am x", repo).returncode, 2)
        check("unstaged key, --all blocks", bash("git commit --all -m x", repo).returncode, 2)
        check("git add && git commit blocks", bash("git add -A && git commit -m x", repo).returncode, 2)
        check("untracked key, git add && commit blocks",
              (os.remove(os.path.join(repo, "app.js")), sh("git", "-C", repo, "rm", "-q", "--cached", "app.js"),
               write("new.js", google), bash("git add . && git commit -m x", repo).returncode)[-1], 2)
        os.remove(os.path.join(repo, "new.js"))

        # allow-secret is per line
        write("t.js", "a = '" + npm + "' // allow-secret\n")
        sh("git", "-C", repo, "add", "t.js")
        check("allow-secret line passes", bash("git commit -m x", repo).returncode, 0)
        write("t.js", "a = '" + npm + "' // allow-secret\nb = '" + npm + "'\n")
        sh("git", "-C", repo, "add", "t.js")
        p = bash("git commit -m x", repo)
        check("the next line without the marker blocks", p.returncode, 2)
        check("and is line 2", "t.js:2" in p.stderr and "t.js:1" not in p.stderr, True)
        sh("git", "-C", repo, "rm", "-q", "-f", "--cached", "t.js")
        os.remove(os.path.join(repo, "t.js"))

        # push: a committed key, no remote has it
        write("k.js", "x = '" + npm + "'\n")
        sh("git", "-C", repo, "add", "k.js")
        sh("git", "-C", repo, "commit", "-qm", "key", "--no-verify")
        bare = os.path.join(tmp, "bare.git")
        sh("git", "init", "-q", "--bare", bare)
        sh("git", "-C", repo, "remote", "add", "origin", bare)
        p = bash("git push origin HEAD", repo)
        check("push of a commit with a key blocks", p.returncode, 2)
        check("push message names the file", "k.js:1  npm-token (npm_)" in p.stderr, True)
        check("push skip variable", bash("git push origin HEAD", repo, {SKIP: "1"}).returncode, 0)

        # Write / Edit / MultiEdit
        check("Write of a key into a tracked path blocks",
              hook("Write", {"file_path": os.path.join(repo, "src.js"), "content": "k=" + google}, repo).returncode, 2)
        p = hook("Write", {"file_path": os.path.join(repo, "src.js"), "content": "a\nk=" + google}, repo)
        check("Write message names file and line", "src.js:2  google-api-key (AIza)" in p.stderr, True)
        check("Write message hides the value", google in p.stderr, False)
        check("Write into a gitignored file passes",
              hook("Write", {"file_path": os.path.join(repo, ".env.local"), "content": "K=" + google}, repo).returncode, 0)
        check("Write of clean text passes",
              hook("Write", {"file_path": os.path.join(repo, "src.js"), "content": "hello"}, repo).returncode, 0)
        check("Edit blocks",
              hook("Edit", {"file_path": os.path.join(repo, "ok.txt"), "old_string": "fine", "new_string": google}, repo).returncode, 2)
        check("MultiEdit blocks", hook("MultiEdit", {"file_path": os.path.join(repo, "ok.txt"), "edits": [
            {"old_string": "a", "new_string": "b"}, {"old_string": "c", "new_string": google}]}, repo).returncode, 2)
        check("Write outside a repository passes",
              hook("Write", {"file_path": os.path.join(tmp, "loose", "x.txt"), "content": google}, tmp).returncode, 0)
        check("Write into a new folder of a repo blocks",
              hook("Write", {"file_path": os.path.join(repo, "new", "deep", "x.js"), "content": google}, repo).returncode, 2)
        check("allow-secret on a written line",
              hook("Write", {"file_path": os.path.join(repo, "s.js"), "content": google + " // allow-secret"}, repo).returncode, 0)
        check("skip variable on a write",
              hook("Write", {"file_path": os.path.join(repo, "s.js"), "content": google}, repo, {SKIP: "1"}).returncode, 0)

        # blocks are recorded as hook fires
        conn = sqlite3.connect(env["LEAN_DB"])
        rows = conn.execute("SELECT hook, outcome FROM hook_fires WHERE hook = 'secret-scan.py'").fetchall()
        conn.close()
        check("blocks are recorded in hook_fires", bool(rows) and all(r == ("secret-scan.py", "block") for r in rows), True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("self-test: ok" if failures == 0 else f"self-test: {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        sys.exit(self_test())
    hookkit.run(main)

#!/usr/bin/env python3
"""Block a key or token from reaching git: a commit, a push, or a file write.

PreToolUse hook. It uses the scanner in tools/secret-scan (gitleaks when it is
installed, plus the plugin's own patterns) and checks:

  Bash   git commit   the staged diff; with -a the work tree against HEAD; with
                      paths (git commit a.txt, -o, -i, --) those paths in the
                      work tree; plus untracked files when git add runs first
  Bash   git push     the commits the push sends that the target remote does
                      not have, by remote and refspec (all of --all, --tags)
  Write, Edit, MultiEdit, NotebookEdit
                      the text being written, when the file is tracked or would
                      be tracked. A gitignored file such as .env.local is fine.

It looks inside bash -c, sh -c, eval, braces, if/then and xargs, and follows
-C, --git-dir, --work-tree, GIT_DIR and GIT_WORK_TREE. A path it cannot work out
(cd $(...)) scans the starting repository, or blocks when that is not one.
If the scanner is missing or fails, the call is blocked with the reason: a
check that cannot run is not a pass.

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


SHELLS = {"bash", "sh", "zsh", "dash", "ksh"}
KEYWORDS = {"{", "}", "!", "if", "then", "else", "elif", "do", "while", "until", "fi", "done"}
QUIET = {"echo", "printf", "grep", "rg", "cat", "man", "which", "head", "tail", "less", "type", "jq"}
COMMIT_VALUE_SHORT = set("mFCct")
COMMIT_VALUE_LONG = {"--author", "--date", "--cleanup", "--fixup", "--squash", "--trailer", "--message",
                     "--file", "--reuse-message", "--reedit-message", "--template"}
ENV_PATH = re.compile(r"""\b(GIT_DIR|GIT_WORK_TREE)=(?:'([^']*)'|"([^"]*)"|([^\s;&|()]+))""")


class Call:
    """One git invocation found in a command."""

    def __init__(self, sub, args, cwd, base, git_dir, work_tree, env, dynamic):
        self.sub, self.args, self.cwd, self.base = sub, args, cwd, base
        self.git_dir, self.work_tree, self.env, self.dynamic = git_dir, work_tree, env, dynamic


class Env:
    def __init__(self, env):
        self.env = env


def is_dynamic(text):
    return "$" in text or "`" in text


def shell_string(words):
    """The script text of `sh -c STRING`, or of eval, else None."""
    if os.path.basename(words[0]) == "eval":
        return " ".join(words[1:])
    for i in range(1, len(words)):
        w = words[i]
        if not w.startswith("-") or w.startswith("--"):
            return None
        if "c" in w[1:]:
            return words[i + 1] if i + 1 < len(words) else None
    return None


def parse_git(words, seg_cwd, base, env, dynamic_cd, env_dirs):
    cwd, git_dir, work_tree, dynamic, i = seg_cwd, None, None, dynamic_cd, 1
    while i < len(words) and words[i].startswith("-"):
        w = words[i]
        name, eq, val = w.partition("=") if w.startswith("--") else (w, "", "")
        if w == "-C" and i + 1 < len(words):
            dynamic = dynamic or is_dynamic(words[i + 1])
            cwd = hookkit.safe_dir(words[i + 1], cwd, base)
            i += 2
        elif name in ("--git-dir", "--work-tree"):
            if not eq:
                if i + 1 >= len(words):
                    break
                val, i = words[i + 1], i + 1
            i += 1
            dynamic = dynamic or is_dynamic(val)
            if name == "--git-dir":
                git_dir = hookkit.resolve(val, cwd)
            else:
                work_tree = hookkit.resolve(val, cwd)
        elif w in ("-c", "--namespace", "--exec-path"):
            i += 2
        else:
            i += 1
    if i >= len(words):
        return None
    if git_dir is None:
        value = env.get("GIT_DIR") or env_dirs.get("GIT_DIR") or os.environ.get("GIT_DIR")
        if value:
            dynamic = dynamic or is_dynamic(value)
            git_dir = hookkit.resolve(value, cwd)
    if work_tree is None:
        value = env.get("GIT_WORK_TREE") or env_dirs.get("GIT_WORK_TREE") or os.environ.get("GIT_WORK_TREE")
        if value:
            dynamic = dynamic or is_dynamic(value)
            work_tree = hookkit.resolve(value, cwd)
    return Call(words[i], words[i + 1:], cwd, base, git_dir, work_tree, env, dynamic)


def find_calls(command, cwd):
    """(calls, unplaced): the git invocations in a command, looking inside sh -c, eval,
    braces, if/then and xargs. unplaced is True when git and commit or push appear but no
    git call could be placed."""
    calls, mentions = [], []
    dynamic_cd = bool(re.search(r"\bcd\b[^;&|\n]*[$`]", command))
    env_dirs = {}
    for m in ENV_PATH.finditer(command):
        env_dirs[m.group(1)] = m.group(2) or m.group(3) or m.group(4) or ""

    def walk(text, where, env, depth):
        for seg in hookkit.segments(text, where):
            words, senv = list(seg.words), dict(env)
            senv.update(seg.env)
            while True:
                size = len(words)
                while words and words[0] in KEYWORDS:
                    words.pop(0)
                while words and hookkit.ASSIGNMENT.fullmatch(words[0]):
                    key, value = words.pop(0).split("=", 1)
                    senv[key] = value
                words = hookkit.strip_wrappers(words, senv)
                if len(words) == size:
                    break
            if not words:
                continue
            first = os.path.basename(words[0])
            if first in SHELLS or first == "eval":
                inner = shell_string(words)
                if inner is not None and depth < 4:
                    walk(inner, seg.cwd, senv, depth + 1)
                    continue
            idx = next((j for j, w in enumerate(words) if os.path.basename(w) == "git"
                        and (j == 0 or first not in QUIET)), None)
            if idx is None:
                if first not in QUIET:
                    mentions.append(seg)
                continue
            call = parse_git(words[idx:], seg.cwd, seg.base, senv, dynamic_cd, env_dirs)
            if call:
                calls.append(call)

    walk(command, cwd, {}, 0)
    return calls, bool(mentions) and not calls and bool(re.search(r"\bgit\b", command))


def commit_plan(args):
    """What `git commit ARGS` takes: flags all, only, include, dry, and the pathspecs."""
    plan = {"all": False, "include": False, "dry": False, "paths": [], "dynamic": False}
    i, rest = 0, False
    while i < len(args):
        a = args[i]
        if rest or not a.startswith("-") or a == "-":
            plan["paths"].append(a)
        elif a == "--":
            rest = True
        elif a.startswith("--"):
            name = a.split("=", 1)[0]
            if name == "--all":
                plan["all"] = True
            elif name == "--include":
                plan["include"] = True
            elif name == "--dry-run":
                plan["dry"] = True
            elif name in COMMIT_VALUE_LONG and "=" not in a:
                i += 1
        else:
            for k, ch in enumerate(a[1:], 1):
                if ch == "a":
                    plan["all"] = True
                elif ch == "i":
                    plan["include"] = True
                elif ch in COMMIT_VALUE_SHORT:
                    if k == len(a) - 1:
                        i += 1
                    break
        i += 1
    plan["dynamic"] = any(is_dynamic(p) for p in plan["paths"])
    return plan


def bash_findings(command, cwd, tool):
    """Findings for every git commit and git push in a command, scanned in one pass."""
    calls, unplaced = find_calls(command, cwd)
    col = tool.Collector()
    adds = any(c.sub in ("add", "stage") for c in calls)
    for c in calls:
        if c.sub not in ("commit", "push") or hookkit.disabled(SKIP, Env(c.env)):
            continue
        repo = tool.Repo(c.cwd, c.git_dir, c.work_tree)
        if c.dynamic:
            repo = tool.Repo(c.base)
            if not tool.is_repo(repo):
                raise tool.ScanError("cannot tell which repository the command works in "
                                     "(a variable or command in a path), so it was not scanned")
        elif not tool.is_repo(repo):
            continue
        if c.sub == "push":
            tool.scan_push(repo, c.args, col=col)
            continue
        plan = commit_plan(c.args)
        if plan["dry"]:
            continue
        if plan["all"]:
            tool.scan_worktree(repo, untracked=adds, col=col)
        elif plan["dynamic"]:
            tool.scan_worktree(repo, untracked=True, staged=True, col=col)
        elif plan["paths"]:
            tool.scan_worktree(repo, paths=plan["paths"], staged=plan["include"], untracked=adds, col=col)
        elif adds:
            tool.scan_worktree(repo, untracked=True, col=col)
        else:
            tool.scan_staged(repo, col=col)
    if unplaced:
        repo = tool.Repo(cwd)
        if tool.is_repo(repo):
            tool.scan_worktree(repo, staged=True, col=col)
    return col.finish()


def as_text(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return "\n".join(as_text(v) for v in value)
    return str(value)


def written_text(name, data):
    if name == "Write":
        return as_text(data.get("content"))
    if name == "MultiEdit":
        return "\n".join(as_text(e.get("new_string")) for e in data.get("edits") or [] if isinstance(e, dict))
    if name == "NotebookEdit":
        return as_text(data.get("new_source"))
    return as_text(data.get("new_string"))


def write_findings(name, data, cwd, tool):
    """Line numbers count the new text of an edit, not the whole file."""
    path = data.get("file_path") or data.get("notebook_path")
    if not isinstance(path, str) or not path:
        return []
    path = hookkit.resolve(path, cwd)
    folder = os.path.dirname(path)
    while folder and not os.path.isdir(folder):
        parent = os.path.dirname(folder)
        if parent == folder:
            return []
        folder = parent
    folder = os.path.realpath(folder)
    repo = tool.Repo(folder)
    if not tool.is_repo(repo):
        return []
    top = os.path.realpath(tool.toplevel(repo))
    rel = os.path.relpath(os.path.realpath(path), top)
    if rel.startswith(".."):
        return []
    tracked = tool.git(repo, "--literal-pathspecs", "ls-files", "--error-unmatch", "--", rel, check=False)
    if tracked is None and tool.git(repo, "check-ignore", "-q", "--", rel, check=False) is not None:
        return []
    return tool.scan_text(written_text(name, data), rel)


def blocked(reason):
    print(f"Blocked by lean-agent: the secret scan could not run ({reason}), so nothing was checked. "
          f"Fix that, or set {SKIP}=1 in front of the command or in the env block of Claude Code settings.",
          file=sys.stderr)
    return 2


def main():
    if hookkit.disabled(SKIP):
        return 0
    event = hookkit.read_event()
    name = event.get("tool_name")
    data = hookkit.tool_input(event)
    cwd = event.get("cwd") if isinstance(event.get("cwd"), str) else os.getcwd()
    if name == "Bash":
        command = data.get("command")
        if not isinstance(command, str) or "git" not in command or not re.search(r"\b(commit|push)\b", command):
            return 0
        what = "this command"
    elif name in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        what = "this " + name.lower()
    else:
        return 0
    try:
        tool = scanner()
        found = bash_findings(command, cwd, tool) if name == "Bash" else write_findings(name, data, cwd, tool)
    except Exception as exc:
        return blocked(f"{type(exc).__name__}: {exc}")
    if found:
        print(tool.message(found, what)
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
        nogl = os.path.join(tmp, "nogl")
        os.makedirs(nogl)
        for name in ("git", "sh", "cat", "touch", "sleep", "printf", "xargs", "echo", "bash"):
            real = shutil.which(name)
            if real:
                os.symlink(real, os.path.join(nogl, name))
        env = dict(os.environ, HOME=tmp, LEAN_DB=os.path.join(tmp, "lean.db"), GIT_CONFIG_NOSYSTEM="1",
                   LEAN_SECRET_SCAN_HOME=os.path.join(tmp, "home"), PATH=nogl)
        for var in (SKIP, "LEAN_STATS", "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
            env.pop(var, None)
        here = os.path.realpath(__file__)

        def sh(*argv, cwd=None):
            return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, env=env)

        def hook(tool_name, tool_input, cwd, extra=None, script=None):
            e = dict(env, **(extra or {}))
            event = {"tool_name": tool_name, "tool_input": tool_input, "cwd": cwd, "session_id": "t1"}
            return subprocess.run([sys.executable, script or here], input=json.dumps(event), capture_output=True,
                                  text=True, env=e)

        def bash(command, cwd, extra=None, script=None):
            return hook("Bash", {"command": command}, cwd, extra, script)

        def make(name):
            r = os.path.join(tmp, name)
            sh("git", "init", "-q", r)
            sh("git", "-C", r, "config", "user.email", "t@example.org")
            sh("git", "-C", r, "config", "user.name", "t")
            return r

        def put(r, rel, text):
            path = os.path.join(r, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(text)

        def commit(r, msg="c"):
            sh("git", "-C", r, "add", "-A")
            sh("git", "-C", r, "commit", "-qm", msg, "--no-verify")

        repo = make("repo")
        write = lambda rel, text: put(repo, rel, text)  # noqa: E731
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
        # 14 on the hook side: the pre-commit framework's SKIP is not ours
        check("SKIP=flake8 does not skip", bash("SKIP=flake8 git commit -m x", repo).returncode, 2)

        # the same key staged, then held back: -a and git add chains
        sh("git", "-C", repo, "reset", "-q")
        check("unstaged key, plain commit passes", bash("git commit -m x", repo).returncode, 0)
        sh("git", "-C", repo, "add", "-N", "app.js")
        check("unstaged key, commit -a blocks", bash("git commit -am x", repo).returncode, 2)
        check("unstaged key, --all blocks", bash("git commit --all -m x", repo).returncode, 2)
        check("git add && git commit blocks", bash("git add -A && git commit -m x", repo).returncode, 2)
        # 1: pathspec commits take the work tree content of those paths
        check("intent-to-add file named in the commit blocks", bash("git commit -m x app.js", repo).returncode, 2)
        check("-o blocks", bash("git commit -o -m x app.js", repo).returncode, 2)
        check("-- blocks", bash("git commit -m x -- app.js", repo).returncode, 2)
        check("-m value is not taken as a path", bash("git commit -m app.js", repo).returncode, 0)
        sh("git", "-C", repo, "rm", "-q", "--cached", "app.js")
        os.remove(os.path.join(repo, "app.js"))
        write("t.js", "ok\n")
        write("other.js", "ok\n")
        commit(repo, "t")
        write("t.js", "ok\nk = '" + google + "'\n")
        check("modified tracked file named in the commit blocks", bash("git commit -m x t.js", repo).returncode, 2)
        check("--only blocks", bash("git commit --only -m x t.js", repo).returncode, 2)
        check("another path is not scanned", bash("git commit -m x other.js", repo).returncode, 0)
        write("other.js", "ok\nk = '" + npm + "'\n")
        sh("git", "-C", repo, "add", "other.js")
        write("t.js", "ok\n")
        check("only: a staged key outside the paths is not committed", bash("git commit -m x t.js", repo).returncode, 0)
        check("-i includes the index", bash("git commit -i -m x t.js", repo).returncode, 2)
        sh("git", "-C", repo, "reset", "-q")
        write("other.js", "ok\n")

        write("new.js", google)
        check("untracked key, git add && commit blocks", bash("git add . && git commit -m x", repo).returncode, 2)
        os.remove(os.path.join(repo, "new.js"))

        # 3: commands that wrap the commit
        write("w.js", "x = '" + google + "'\n")
        sh("git", "-C", repo, "add", "w.js")
        for label, cmd in (("bash -c", "bash -c 'git commit -m x'"), ("sh -c", 'sh -c "git commit -m x"'),
                           ("bash -lc", "bash -lc 'git commit -m x'"), ("eval", "eval 'git commit -m x'"),
                           ("braces", "{ git commit -m x; }"), ("if/then", "if true; then git commit -m x; fi"),
                           ("negation", "! git commit -m x"), ("xargs", "echo m | xargs git commit -m"),
                           ("nested", "bash -c \"sh -c 'git commit -m x'\""),
                           ("cd inside", f"bash -c 'cd {repo} && git commit -m x'"),
                           ("env wrapper", "env FOO=1 git commit -m x"), ("while", "while true; do git commit -m x; done")):
            check(f"wrapped: {label}", bash(cmd, repo).returncode, 2)
        check("wrapped, skip variable inside the string", bash("bash -c 'LEAN_SKIP_SECRET_SCAN=1 git commit -m x'", repo).returncode, 0)
        check("echo of a commit command does not scan", bash("echo git commit", repo).returncode, 0)
        check("git log --grep commit passes", bash("git log --grep commit", repo).returncode, 0)
        check("an unplaced git commit scans conservatively", bash("$(printf git) commit -m x", repo).returncode, 2)

        # 4: other repositories by option or variable
        outer = os.path.join(tmp, "elsewhere")
        os.makedirs(outer)
        gd = os.path.join(repo, ".git")
        check("--git-dir with --work-tree", bash(f"git --git-dir={gd} --work-tree={repo} commit -m x", outer).returncode, 2)
        check("--git-dir and --work-tree as two words", bash(f"git --git-dir {gd} --work-tree {repo} commit -m x", outer).returncode, 2)
        check("GIT_DIR inline", bash(f"GIT_DIR={gd} GIT_WORK_TREE={repo} git commit -m x", outer).returncode, 2)
        check("GIT_DIR exported earlier", bash(f"export GIT_DIR={gd}; export GIT_WORK_TREE={repo}; git commit -m x", outer).returncode, 2)
        check("GIT_DIR in the environment", bash("git commit -m x", outer, {"GIT_DIR": gd, "GIT_WORK_TREE": repo}).returncode, 2)
        check("cd $(...) from inside the repo scans it", bash("cd $(echo /) && git commit -m x", repo).returncode, 2)
        p = bash("cd $(echo /) && git commit -m x", outer)
        check("cd $(...) from a non-repo fails closed", p.returncode, 2)
        check("and says it could not tell", "could not run" in p.stderr, True)
        check("-C with a variable fails closed", bash("git -C $DIR commit -m x", outer).returncode, 2)
        check("-C with a variable in the repo scans it", bash("git -C $DIR commit -m x", repo).returncode, 2)

        sh("git", "-C", repo, "rm", "-q", "-f", "--cached", "w.js")
        os.remove(os.path.join(repo, "w.js"))

        # allow-secret is per line
        write("t.js", "a = '" + npm + "' // allow-secret\n")
        sh("git", "-C", repo, "add", "t.js")
        check("allow-secret line passes", bash("git commit -m x", repo).returncode, 0)
        write("t.js", "a = '" + npm + "' // allow-secret\nb = '" + npm + "'\n")
        sh("git", "-C", repo, "add", "t.js")
        p = bash("git commit -m x", repo)
        check("the next line without the marker blocks", p.returncode, 2)
        check("and is line 2", "t.js:2" in p.stderr and "t.js:1" not in p.stderr, True)
        sh("git", "-C", repo, "reset", "-q", "--hard")
        sh("git", "-C", repo, "clean", "-qfd")

        # 2: push scope. The key sits on priv already; pub must still be checked.
        write("k.js", "x = '" + npm + "'\n")
        sh("git", "-C", repo, "add", "k.js")
        sh("git", "-C", repo, "commit", "-qm", "key", "--no-verify")
        priv, pub = os.path.join(tmp, "priv.git"), os.path.join(tmp, "pub.git")
        sh("git", "init", "-q", "--bare", priv)
        sh("git", "init", "-q", "--bare", pub)
        sh("git", "-C", repo, "remote", "add", "priv", priv)
        sh("git", "-C", repo, "remote", "add", "pub", pub)
        p = bash("git push pub HEAD", repo)
        check("push of a commit with a key blocks", p.returncode, 2)
        check("push message names the file", "k.js:1  npm-token (npm_)" in p.stderr, True)
        check("push skip variable", bash("git push pub HEAD", repo, {SKIP: "1"}).returncode, 0)
        sh("git", "-C", repo, "branch", "other")
        sh("git", "-C", repo, "tag", "v1")
        sh("git", "-C", repo, "push", "-q", "priv", "HEAD:refs/heads/master")
        sh("git", "-C", repo, "fetch", "-q", "priv")
        check("pushing to priv, which has it, passes", bash("git push priv HEAD:master", repo).returncode, 0)
        for label, cmd in (("a branch by name", "git push pub other"), ("a full ref", "git push pub refs/heads/other"),
                           ("--all", "git push --all pub"), ("--tags", "git push --tags pub"),
                           ("a tag", "git push pub v1"), ("src:dst", "git push pub other:x"),
                           ("the same key on another remote", "git push pub HEAD"),
                           ("a wrapped push", "bash -c 'git push pub other'")):
            check(f"push scope: {label}", bash(cmd, repo).returncode, 2)
        check("push --dry-run passes", bash("git push --dry-run pub other", repo).returncode, 0)
        check("push --delete passes", bash("git push pub --delete other", repo).returncode, 0)
        p = bash("git push pub nosuchbranch", repo)
        check("an unresolvable ref fails closed", p.returncode, 2)
        check("and says nothing was checked", "could not run" in p.stderr, True)

        # Write / Edit / MultiEdit / NotebookEdit
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
        # 13
        p = hook("NotebookEdit", {"notebook_path": os.path.join(repo, "n.ipynb"), "new_source": "k=" + google}, repo)
        check("NotebookEdit blocks", p.returncode, 2)
        check("NotebookEdit names the notebook", "n.ipynb:1" in p.stderr, True)
        check("NotebookEdit with a list source blocks",
              hook("NotebookEdit", {"notebook_path": os.path.join(repo, "n.ipynb"), "new_source": ["a", google]}, repo).returncode, 2)
        check("a non-string content does not crash",
              hook("Write", {"file_path": os.path.join(repo, "src.js"), "content": 5}, repo).returncode, 0)
        check("a list content is scanned",
              hook("Write", {"file_path": os.path.join(repo, "src.js"), "content": ["x", google]}, repo).returncode, 2)
        write("ign.txt", "x\n")
        sh("git", "-C", repo, "add", "-f", "ign.txt")
        write(".gitignore", ".env.local\nign.txt\n")
        sh("git", "-C", repo, "add", ".gitignore")
        sh("git", "-C", repo, "commit", "-qm", "ign", "--no-verify")
        check("a tracked file that is also gitignored is scanned",
              hook("Write", {"file_path": os.path.join(repo, "ign.txt"), "content": google}, repo).returncode, 2)
        link = os.path.join(tmp, "lnk")
        os.symlink(repo, link)
        p = hook("Write", {"file_path": os.path.join(link, "sub", "src.js"), "content": google}, tmp)
        check("a write through a symlinked folder blocks", p.returncode, 2)
        check("and is labelled by the path inside the repo", "sub/src.js:1" in p.stderr and ".." not in p.stderr, True)

        # 5: a repo's own config must not run code during the scan
        trap = make("trap")
        put(trap, "a.txt", "hi\n")
        commit(trap, "base")
        mark = os.path.join(tmp, "ran")
        script = os.path.join(tmp, "trap.sh")
        with open(script, "w") as f:
            f.write(f"#!/bin/sh\ntouch '{mark}'\ncat \"$1\" 2>/dev/null\n")
        os.chmod(script, 0o755)
        sh("git", "-C", trap, "config", "core.fsmonitor", script)
        sh("git", "-C", trap, "config", "diff.evil.textconv", script)
        put(trap, ".gitattributes", "a.txt diff=evil\n")
        put(trap, "a.txt", "hi\nk = " + google + "\n")
        sh("git", "-C", trap, "add", "a.txt")
        sh("git", "-C", trap, "diff", "--cached")
        check("the trap fires on plain git", os.path.exists(mark), True)
        os.remove(mark)
        check("commit scan finds the key", bash("git commit -m x", trap).returncode, 2)
        check("and ran no repo-configured command", os.path.exists(mark), False)

        # 9: a scanner that is missing or crashes blocks with the reason
        plug = os.path.join(tmp, "plug")
        shutil.copytree(os.path.join(os.path.dirname(here), "..", "lib"), os.path.join(plug, "lib"),
                        ignore=shutil.ignore_patterns("__pycache__"))
        os.makedirs(os.path.join(plug, "hooks"))
        os.makedirs(os.path.join(plug, "tools"))
        copy = os.path.join(plug, "hooks", "secret-scan.py")
        shutil.copy(here, copy)
        p = bash("git commit -m x", repo, script=copy)
        check("a missing scanner blocks", p.returncode, 2)
        check("and says why", "could not run" in p.stderr and "nothing was checked" in p.stderr, True)
        check("hook error ignored is not printed", "hook error ignored" in p.stderr, False)
        check("unrelated commands still pass without the scanner", bash("git status", repo, script=copy).returncode, 0)
        with open(os.path.join(plug, "tools", "secret-scan"), "w") as f:
            f.write("raise RuntimeError('boom')\n")
        p = bash("git commit -m x", repo, script=copy)
        check("a crashing scanner blocks", p.returncode, 2)
        check("and shows the error", "boom" in p.stderr, True)
        check("a write is blocked too", hook("Write", {"file_path": os.path.join(repo, "a.js"), "content": "x"},
                                             repo, script=copy).returncode, 2)
        check("skip still works without the scanner", bash("git commit -m x", repo, {SKIP: "1"}, script=copy).returncode, 0)

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

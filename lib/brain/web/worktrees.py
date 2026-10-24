"""Git repositories the brain knows about and the state of their worktrees.

Read only: plain SELECTs on the connection given, and git commands that do not change anything.

  python3 lib/brain/web/worktrees.py --self-test
"""
import os
import subprocess
import sys

TIMEOUT = 10


_SAFE = ["-c", "core.fsmonitor=false", "-c", f"core.hooksPath={os.devnull}", "--no-optional-locks"]


def _git(run, cwd, *args):
    """Stdout of a git command run in cwd, or None if it failed. No hooks, no fsmonitor, no index writes."""
    try:
        r = run(["git", *_SAFE, *args], cwd=cwd, capture_output=True, text=True, timeout=TIMEOUT,
                env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return None
    return (r.stdout or "") if r.returncode == 0 else None


def _repo_of(path, run):
    """(folder, common git dir) for a folder inside a git repo, else None."""
    if not path or not os.path.isdir(path):
        return None
    top = _git(run, path, "rev-parse", "--show-toplevel")
    common = _git(run, path, "rev-parse", "--git-common-dir")
    if not top or not top.strip() or not common or not common.strip():
        return None
    common = os.path.realpath(os.path.join(path, common.strip()))
    top = top.strip()
    if os.path.basename(common) == ".git":
        top = os.path.dirname(common)
    return top, common


def known_repos(con, roots, run=subprocess.run) -> list:
    paths = []
    for sql in ("SELECT repo_dir FROM jobs", "SELECT session_cwd FROM items", "SELECT cwd FROM sessions"):
        try:
            paths += [r[0] for r in con.execute(sql).fetchall()]
        except Exception:
            pass
    for root in roots or []:
        try:
            paths += [os.path.join(root, n) for n in sorted(os.listdir(root))]
        except OSError:
            pass
    seen, out = set(), []
    for p in dict.fromkeys(paths):
        if not isinstance(p, str):
            continue
        repo = _repo_of(p, run)
        if repo and repo[1] not in seen:
            seen.add(repo[1])
            out.append(repo[0])
    return out


def _count(out):
    try:
        return int((out or "").strip())
    except ValueError:
        return None


def _entries(text):
    entry = {}
    for line in (text or "").splitlines() + [""]:
        if not line:
            if entry:
                yield entry
            entry = {}
            continue
        key, _, val = line.partition(" ")
        entry[key] = val


def list_worktrees(repo, run=subprocess.run) -> list:
    out = _git(run, repo, "worktree", "list", "--porcelain")
    rows = []
    for e in _entries(out):
        path = e.get("worktree")
        if not path or "bare" in e:
            continue
        branch = e.get("branch", "")
        if branch.startswith("refs/heads/"):
            branch = branch[len("refs/heads/"):]
        row = {"path": path, "branch": branch, "unpushed": 0, "dirty": False, "missing": not os.path.isdir(path)}
        if not row["missing"]:
            status = _git(run, path, "status", "--porcelain", "--ignore-submodules")
            row["dirty"] = None if status is None else bool(status.strip())
            n = _git(run, path, "rev-list", "--count", "@{u}..HEAD")
            if n is None:
                n = _git(run, path, "rev-list", "--count", "HEAD", "--not", "--remotes")
            row["unpushed"] = None if n is None else _count(n)
        rows.append(row)
    return rows


def _self_test() -> int:
    import shutil
    import sqlite3
    import tempfile
    os.environ["LEAN_BRAIN_DB"] = os.path.join(tempfile.mkdtemp(), "brain.db")
    fails = []

    def check(name, got, want):
        if got != want:
            fails.append(name)
            print(f"FAIL {name}: got {got!r}, want {want!r}")

    base = os.path.realpath(tempfile.mkdtemp())
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    os.environ.update({k: env[k] for k in env if k.startswith("GIT_")})

    def git(cwd, *a):
        subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, env=env)

    remote = os.path.join(base, "remote.git")
    git(base, "init", "-q", "--bare", "-b", "main", remote)
    main = os.path.join(base, "work", "proj")
    os.makedirs(main)
    git(main, "init", "-q", "-b", "main")
    open(os.path.join(main, "a.txt"), "w").write("a")
    git(main, "add", ".")
    git(main, "commit", "-qm", "first")
    git(main, "remote", "add", "origin", remote)
    git(main, "push", "-q", "-u", "origin", "main")
    wt1 = os.path.join(base, "wt1")
    wt2 = os.path.join(base, "wt2")
    wt3 = os.path.join(base, "wt3")
    git(main, "worktree", "add", "-q", "-b", "dirty", wt1)
    git(main, "worktree", "add", "-q", "-b", "ahead", wt2)
    git(main, "worktree", "add", "-q", "-b", "gone", wt3)
    open(os.path.join(wt1, "new.txt"), "w").write("x")
    open(os.path.join(wt2, "b.txt"), "w").write("b")
    git(wt2, "add", ".")
    git(wt2, "commit", "-qm", "second")
    shutil.rmtree(wt3)

    rows = {os.path.realpath(r["path"]): r for r in list_worktrees(main)}
    check("four worktrees", len(rows), 4)
    m = rows[os.path.realpath(main)]
    check("main clean and pushed", (m["branch"], m["dirty"], m["unpushed"], m["missing"]), ("main", False, 0, False))
    d = rows[os.path.realpath(wt1)]
    check("dirty worktree", (d["branch"], d["dirty"], d["unpushed"], d["missing"]), ("dirty", True, 0, False))
    a = rows[os.path.realpath(wt2)]
    check("one unpushed commit", (a["branch"], a["dirty"], a["unpushed"]), ("ahead", False, 1))
    g = rows[os.path.realpath(wt3)]
    check("deleted folder is missing", (g["missing"], g["dirty"], g["unpushed"]), (True, False, 0))
    git(wt2, "push", "-q", "-u", "origin", "ahead")
    check("pushed branch with upstream", [r for r in list_worktrees(main) if r["branch"] == "ahead"][0]["unpushed"], 0)
    # a git that fails every status and rev-list reads as unknown, not clean
    real_run = subprocess.run
    seen = []

    def flaky(argv, **kw):
        seen.append((argv, kw))
        if "status" in argv or "rev-list" in argv:
            raise subprocess.TimeoutExpired(argv, 10)
        return real_run(argv, **kw)
    ur = [r for r in list_worktrees(main, run=flaky) if not r["missing"]]
    check("failed git is unknown", {(r["dirty"], r["unpushed"]) for r in ur}, {(None, None)})
    check("safe flags on every call", all(a[0][:7] == ["git", "-c", "core.fsmonitor=false", "-c",
          f"core.hooksPath={os.devnull}", "--no-optional-locks", "worktree"] or a[0][6] in ("status", "rev-list")
          for a in seen), True)
    check("optional locks off", all(a[1]["env"].get("GIT_OPTIONAL_LOCKS") == "0" for a in seen), True)
    check("status ignores submodules", all("--ignore-submodules" in a[0] for a in seen if "status" in a[0]), True)
    check("not a repo", list_worktrees(base), [])
    check("git missing", list_worktrees(main, run=lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError())), [])

    other = os.path.join(base, "roots", "other")
    os.makedirs(other)
    git(other, "init", "-q", "-b", "main")
    plain = os.path.join(base, "roots", "plain")
    os.makedirs(plain)
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE jobs (repo_dir TEXT, branch TEXT)")
    con.execute("CREATE TABLE items (session_cwd TEXT)")
    con.execute("CREATE TABLE sessions (cwd TEXT)")
    con.execute("INSERT INTO jobs VALUES (?, 'x')", (main,))
    con.execute("INSERT INTO items VALUES (?)", (wt1,))
    con.execute("INSERT INTO items VALUES (?)", (wt3,))
    con.execute("INSERT INTO items VALUES (NULL)")
    con.execute("INSERT INTO sessions VALUES (?)", (plain,))
    con.execute("INSERT INTO sessions VALUES (?)", (os.path.join(base, "nope"),))
    found = known_repos(con, [os.path.join(base, "roots")])
    check("repos deduped by common dir", sorted(os.path.realpath(p) for p in found),
          sorted([os.path.realpath(main), os.path.realpath(other)]))
    check("no tables is fine", known_repos(sqlite3.connect(":memory:"), []), [])

    print("self-test: ok" if not fails else f"self-test: {len(fails)} failed")
    return 1 if fails else 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        sys.exit(_self_test())
    print(__doc__)

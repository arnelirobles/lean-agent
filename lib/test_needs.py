#!/usr/bin/env python3
"""What a repository's tests need, checked before a run, and which failures are the machine's fault.

  test_needs.py [DIR]        check DIR (default: the repository root, else the current directory)
  test_needs.py --self-test

test-phase calls this before a job starts and from `test-phase --doctor`. Nothing is installed.

A repository lists its prerequisites in .lean/test-needs, one per line. Blank lines and lines
starting with # are skipped:

    docker                    the Docker daemon answers `docker info`
    path ../sibling-repo      a folder or file exists, relative to the main checkout (the folder holding
                              .git), so a worktree of the repository looks in the same place
    env NAME                  the variable is set and not empty; the value is never printed
    command NAME              NAME is on PATH
    command NAME gnu          NAME is on PATH and `NAME --version` says GNU
    command NAME >=22.11      NAME is on PATH and `NAME --version` is at least that

With a test-needs file, bin/check-runtime.sh runs as part of the same check, for a run and for --doctor.
A "#" after whitespace starts a comment at the end of a line.

environmental() reads the other side: a run where most failures share one cause that is not the
code (Docker is down, the database cannot be reached). test-phase files one blocker for such a run.
"""
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.realpath(__file__))
RUNTIME = os.path.join(HERE, "..", "tools", "check-runtime.sh")
NEEDS = os.path.join(".lean", "test-needs")
KINDS = ("docker", "path", "env", "command")

# (label, what to do, pattern). The first cause a failure matches is its cause.
CAUSES = [
    ("docker unavailable", "start Docker, or run this where Docker is available",
     re.compile(r"DockerUnavailableException")),
    ("testcontainers resource reaper", "Docker is not usable by Testcontainers here; start Docker or run elsewhere",
     re.compile(r"ResourceReaper")),
    ("database unreachable", "nothing answered on the database address; start the database or its container",
     re.compile(r"NpgsqlException[^\n]*Failed to connect to")),
]
MIN_SHARED = 3  # fewer than this is a test problem, not a machine problem


def parse(text):
    """([(kind, args, line number)], [problem]). An unknown line is a problem, so a typo is loud."""
    needs, problems = [], []
    for n, raw in enumerate(text.splitlines(), 1):
        line = re.sub(r"\s+#.*$", "", raw).strip()
        if not line or line.startswith("#"):
            continue
        kind, *args = line.split()
        bad = None
        if kind not in KINDS:
            bad = f"unknown prerequisite {kind!r}"
        elif kind == "docker" and args:
            bad = "docker takes no argument"
        elif kind in ("path", "env") and len(args) != 1:
            bad = f"{kind} takes one argument"
        elif kind == "command" and not 1 <= len(args) <= 2:
            bad = "command takes a name and optionally gnu or >=VERSION"
        elif kind == "command" and len(args) == 2 and args[1] != "gnu" and not re.fullmatch(r">=\d+(\.\d+){0,2}", args[1]):
            bad = f"command check {args[1]!r} is not gnu or >=VERSION"
        if bad:
            problems.append(f"{NEEDS} line {n}: {bad}")
        else:
            needs.append((kind, args, n))
    return needs, problems


def _version(text):
    m = re.search(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?", text or "")
    return tuple(int(x) if x else 0 for x in m.groups()) if m else None


def _run(cmd, cwd, timeout=30):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd, timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    return r


def main_checkout(repo):
    """The folder holding .git for repo, which is repo itself unless repo is a linked worktree."""
    r = _run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], repo, 10)
    common = r.stdout.strip() if r and r.returncode == 0 else ""
    return os.path.dirname(common) if os.path.basename(common) == ".git" else repo


def check_one(repo, kind, args):
    """None when met, else one line naming what is missing. Environment values are never read into the line."""
    if kind == "docker":
        r = _run(["docker", "info"], repo, 30)
        if r is None:
            return "docker: the docker command is missing or did not answer"
        if r.returncode != 0:
            return "docker: the daemon is not reachable (`docker info` failed); start Docker"
    elif kind == "path":
        base = main_checkout(repo)
        if not os.path.exists(os.path.join(base, args[0])):
            return f"path: {args[0]} is not there (looked from {base})"
    elif kind == "env":
        if not os.environ.get(args[0]):
            return f"env: {args[0]} is not set"
    else:
        name = args[0]
        if not _which(name):
            return f"command: {name} is not on PATH"
        if len(args) == 2:
            r = _run([name, "--version"], repo)
            out = ((r.stdout + r.stderr) if r else "")
            if args[1] == "gnu":
                if r is None or r.returncode != 0 or "GNU" not in out:
                    return f"command: {name} is not the GNU one (on macOS install coreutils and put gnubin first on PATH)"
            else:
                want, got = _version(args[1][2:]), _version(out)
                if r is None or got is None:
                    return f"command: {name} --version gave no version, need {args[1]}"
                if got < want:
                    return f"command: {name} is {'.'.join(map(str, got))}, need {args[1]}"
    return None


def _which(name):
    for d in os.environ.get("PATH", "").split(os.pathsep):
        p = os.path.join(d or ".", name)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def runtime_problems(repo):
    """The MISMATCH lines of bin/check-runtime.sh, [] when it passes or is not installed."""
    if not os.path.isfile(RUNTIME):
        return []
    r = _run(["bash", RUNTIME, repo], repo, 120)
    if r is None:
        return ["check-runtime.sh did not run"]
    lines = [l.strip() for l in r.stdout.splitlines() if l.lstrip().startswith("MISMATCH")]
    if r.returncode != 0 and not lines:
        lines = ["check-runtime.sh failed: " + ((r.stderr or r.stdout).strip().splitlines() or ["no output"])[-1]]
    return lines


def preflight(repo):
    """Problems that stop a run, [] when it may start. No test-needs file means no checks."""
    path = os.path.join(repo, NEEDS)
    if not os.path.isfile(path):
        return []
    return [line for _, line in report(repo) if line.startswith("MISSING")]


def report(repo):
    """[(ok, text)] for every check, runtime check included. Text starts with ok or MISSING.
    With no test-needs file there is nothing to check, and the runtime check does not run."""
    out = []
    path = os.path.join(repo, NEEDS)
    if not os.path.isfile(path):
        return [(True, f"ok      no {NEEDS} in {repo}, nothing declared")]
    with open(path, errors="replace") as fh:
        needs, problems = parse(fh.read())
    out += [(False, "MISSING " + p) for p in problems]
    for kind, args, _ in needs:
        bad = check_one(repo, kind, args)
        out.append((bad is None, "ok      " + " ".join([kind] + args) if bad is None else "MISSING " + bad))
    out += [(False, "MISSING runtime: " + l.replace("MISMATCH", "", 1).strip()) for l in runtime_problems(repo)]
    return out


def environmental(failing):
    """(label, hint, names, counts) when most failures come from the machine, else None.

    failing maps a test name to the text around its failure. Each failure counts for the first cause it
    matches. The causes are added together: at least MIN_SHARED failures, and more than half of all of
    them. label is the commonest cause, counts maps each cause to its failures."""
    by = {}
    for name, detail in failing.items():
        for label, hint, rx in CAUSES:
            if rx.search(detail or ""):
                by.setdefault(label, (hint, []))[1].append(name)
                break
    names = [n for _, ns in by.values() for n in ns]
    if len(names) < MIN_SHARED or len(names) * 2 <= len(failing):
        return None
    top = max(by, key=lambda k: len(by[k][1]))
    hint = by[top][0] if len(by) == 1 else "; ".join(h for h, _ in by.values())
    return top, hint, names, {k: len(v[1]) for k, v in by.items()}


def repo_root():
    r = _run(["git", "rev-parse", "--show-toplevel"], os.getcwd(), 10)
    return r.stdout.strip() if r and r.returncode == 0 and r.stdout.strip() else os.getcwd()


def doctor(repo):
    lines = report(repo)
    for _, text in lines:
        print(text)
    bad = sum(1 for ok, _ in lines if not ok)
    print(f"test-needs: {len(lines)} checked, {bad} missing")
    return 1 if bad else 0


def self_test():
    fails = []
    with tempfile.TemporaryDirectory() as d:
        shims, repo = os.path.join(d, "bin"), os.path.join(d, "repo")
        os.makedirs(shims)
        os.makedirs(os.path.join(repo, ".lean"))
        os.makedirs(os.path.join(d, "sibling"))
        saved = os.environ.copy()

        def shim(name, body):
            p = os.path.join(shims, name)
            open(p, "w").write("#!/bin/sh\n" + body + "\n")
            os.chmod(p, 0o755)

        def needs(text):
            open(os.path.join(repo, NEEDS), "w").write(text)

        def once(text):
            needs(text)
            return [t for ok, t in report(repo) if not ok]

        os.environ["PATH"] = shims + os.pathsep + saved.get("PATH", "")
        try:
            if parse("# c\n\ndocker\npath ../x\nenv A\ncommand t gnu\ncommand n >=22.11\n")[1]:
                fails.append("a valid file must parse")
            for bad in ("docker now", "path", "env A B", "command", "command t latest", "ping x"):
                if not parse(bad)[1]:
                    fails.append(f"{bad!r} must be a problem")
            if once("bogus thing\n") == []:
                fails.append("an unknown line must be a problem")
            # docker
            shim("docker", "exit 0")
            if once("docker\n"):
                fails.append("docker that answers must pass")
            shim("docker", "echo cannot connect >&2; exit 1")
            got = once("docker\n")
            if len(got) != 1 or "not reachable" not in got[0]:
                fails.append(f"docker that fails must be named: {got}")
            # path
            if once("path ../sibling\n"):
                fails.append("a sibling that exists must pass")
            got = once("path ../nope\n")
            if len(got) != 1 or "../nope" not in got[0]:
                fails.append(f"a missing path must be named: {got}")
            # env: named, value never printed
            os.environ["NEEDS_TEST_SET"] = "s3cret-value-xyz"
            os.environ.pop("NEEDS_TEST_UNSET", None)
            if once("env NEEDS_TEST_SET\n"):
                fails.append("a set variable must pass")
            needs("env NEEDS_TEST_SET\nenv NEEDS_TEST_UNSET\n")
            text = "\n".join(t for _, t in report(repo))
            if "NEEDS_TEST_UNSET is not set" not in text or "s3cret-value-xyz" in text:
                fails.append(f"a missing variable must be named and no value printed: {text!r}")
            os.environ["NEEDS_TEST_EMPTY"] = ""
            if not once("env NEEDS_TEST_EMPTY\n"):
                fails.append("an empty variable must count as missing")
            # command, gnu and version
            got = once("command lean-no-such-command\n")
            if len(got) != 1 or "lean-no-such-command is not on PATH" not in got[0]:
                fails.append(f"a command not on PATH must be named: {got}")
            shim("timeout", "echo 'timeout (GNU coreutils) 9.4'")
            shim("stat", "echo 'stat: illegal option -- -' >&2; exit 1")
            shim("node", "echo v22.5.1")
            shim("gnufail", "echo 'GNU but broken'; exit 1")
            got = once("command gnufail gnu\n")
            if len(got) != 1 or "not the GNU one" not in got[0]:
                fails.append(f"a command that prints GNU and exits nonzero must fail the gnu check: {got}")
            if once("env NEEDS_TEST_SET  # a comment\ncommand node >=18 # more\n"):
                fails.append("a comment at the end of a line must be ignored")
            # a path is looked for from the main checkout, also when the job runs in a worktree of it
            main, wt = os.path.join(d, "main"), os.path.join(d, "wt")
            os.makedirs(main)
            open(os.path.join(d, "only-beside-main"), "w").close()
            for cmd in (["git", "init", "-q"], ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
                                                 "--allow-empty", "-m", "x"],
                        ["git", "worktree", "add", "-q", wt]):
                if subprocess.run(cmd, cwd=main, capture_output=True).returncode != 0:
                    fails.append(f"could not set up the worktree fixture: {cmd}")
            os.makedirs(os.path.join(wt, ".lean"))
            open(os.path.join(wt, NEEDS), "w").write("path ../only-beside-main\n")
            if [t for ok, t in report(wt) if not ok]:
                fails.append("a worktree must look for a relative path from the main checkout")
            open(os.path.join(wt, "only-in-wt"), "w").close()
            open(os.path.join(wt, NEEDS), "w").write("path only-in-wt\n")
            if not [t for ok, t in report(wt) if not ok]:
                fails.append("a worktree must not look for a relative path from itself")
            if once("command timeout gnu\n"):
                fails.append("GNU timeout must pass")
            got = once("command stat gnu\n")
            if len(got) != 1 or "not the GNU one" not in got[0]:
                fails.append(f"BSD stat must fail the gnu check: {got}")
            got = once("command node >=22.11\n")
            if len(got) != 1 or "22.5.1" not in got[0]:
                fails.append(f"node 22.5.1 must fail >=22.11: {got}")
            if once("command node >=22.5.1\ncommand node >=22.5\ncommand node >=18\n"):
                fails.append("node 22.5.1 must pass >=22.5 and >=18")
            # runtime check joins in
            open(os.path.join(repo, "package.json"), "w").write('{"engines":{"node":">=22.11.0"}}')
            got = once("command node\n")
            if not any("runtime:" in t and "22.11" in t for t in got):
                fails.append(f"check-runtime must run with the file: {got}")
            if preflight(repo) == []:
                fails.append("preflight must return the runtime mismatch")
            os.remove(os.path.join(repo, "package.json"))
            os.remove(os.path.join(repo, NEEDS))
            if preflight(repo) != []:
                fails.append("no test-needs file means no preflight problems")
        finally:
            os.environ.clear()
            os.environ.update(saved)

    docker = {f"T.Test{i}": f"failed T.Test{i}\n  Testcontainers.Builders.DockerUnavailableException: Docker is either not running" for i in range(1800)}
    got = environmental(docker)
    if not got or got[0] != "docker unavailable" or len(got[2]) != 1800 or got[3] != {"docker unavailable": 1800}:
        fails.append("1800 Docker failures must be one cause")
    reaper = {f"T.R{i}": "System.TypeInitializationException: DotNet.Testcontainers.Containers.ResourceReaper" for i in range(5)}
    pg = {f"T.P{i}": "Npgsql.NpgsqlException : Failed to connect to 127.0.0.1:5432" for i in range(4)}
    if not environmental(reaper) or not environmental(pg):
        fails.append("ResourceReaper and Npgsql connect failures must be causes")
    if environmental({"T.A": "DockerUnavailableException", "T.B": "DockerUnavailableException"}):
        fails.append("fewer than the minimum is not an environment problem")
    mixed = dict(docker)
    mixed.update({f"T.Real{i}": "Assert.Equal failed" for i in range(2000)})
    if environmental(mixed):
        fails.append("a minority sharing a cause is not an environment problem")
    if environmental({f"T.X{i}": "Assert.Equal failed" for i in range(10)}):
        fails.append("ordinary failures are not an environment problem")
    # Exactly half is not more than half; one over is.
    half = {f"T.D{i}": "DockerUnavailableException" for i in range(3)}
    half.update({f"T.Real{i}": "Assert.Equal failed" for i in range(3)})
    if environmental(half):
        fails.append("exactly half of the failures is not more than half")
    half["T.D3"] = "DockerUnavailableException"
    if not environmental(half):
        fails.append("4 of 7 failures is more than half")
    # Causes add up, the commonest names the blocker, and the later of two causes can be the commonest.
    two = {f"T.D{i}": "DockerUnavailableException" for i in range(2)}
    two.update({f"T.P{i}": "NpgsqlException : Failed to connect to x" for i in range(3)})
    got = environmental(two)
    if not got or got[0] != "database unreachable" or got[3] != {"docker unavailable": 2, "database unreachable": 3}:
        fails.append(f"two causes must add up, commonest first: {got}")
    two2 = {f"T.D{i}": "DockerUnavailableException" for i in range(3)}
    two2.update({f"T.P{i}": "NpgsqlException : Failed to connect to x" for i in range(2)})
    got = environmental(two2)
    if not got or got[0] != "docker unavailable":
        fails.append(f"the commonest cause must name the blocker: {got}")
    if fails:
        print("self-test failed:\n  " + "\n  ".join(fails))
        return 1
    print("self-test passed")
    return 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        sys.exit(self_test())
    target = sys.argv[1] if len(sys.argv) > 1 else repo_root()
    if not os.path.isdir(target):
        print(f"test_needs: {target} is not a directory", file=sys.stderr)
        sys.exit(2)
    sys.exit(doctor(os.path.abspath(target)))

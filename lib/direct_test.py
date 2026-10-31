"""One matcher for a direct test run that a lane can take, read by script-note and wait-loop.

A segment is a lane job when it starts with dotnet test (a .sln or .csproj is in reach) or with
npm, pnpm or yarn test, npx vitest or vitest run (a pnpm-lock.yaml is in reach), because test-phase
runs dotnet projects and pnpm Vitest packages only. `dotnet test --list-tests` runs nothing.
"""
import glob
import json
import os
import re

RUN = re.compile(
    r"^\s*(?:[A-Za-z_]\w*=\S*\s+)*(?:time\s+)?"
    r"(?:(?P<dotnet>dotnet\s+test)|(?P<node>(?:npm|pnpm|yarn)(?:\s+run)?\s+test|npx\s+vitest|vitest\s+run))(?![\w:.-])")
INLINE_ALLOW = re.compile(r"^\s*(?:[A-Za-z_]\w*=\S*\s+)*LEAN_DIRECT_TEST=allow\b", re.I)


def stop_at(cwd):
    """The folder a walk up from cwd may reach: the git root, else cwd itself."""
    here = os.path.abspath(cwd or ".")
    walk = here
    while True:
        if os.path.exists(os.path.join(walk, ".git")):
            return walk
        parent = os.path.dirname(walk)
        if parent == walk:
            return here
        walk = parent


def reach(cwd, patterns):
    """True when a pattern matches in cwd, a parent up to the git root (cwd when not in git) or a folder one level below cwd."""
    here = os.path.abspath(cwd or ".")
    top = stop_at(here)
    while True:
        if any(glob.glob(os.path.join(glob.escape(here), p)) for p in patterns):
            return True
        parent = os.path.dirname(here)
        if here == top or parent == here:
            break
        here = parent
    return any(glob.glob(os.path.join(glob.escape(os.path.abspath(cwd or ".")), "*", p)) for p in patterns)


# A script that hands the run to other packages says nothing about which runner they use.
DELEGATES = re.compile(r"(?:^|[\s;&|])(?:turbo|nx|lerna)\b|\s(?:-r|--recursive|--filter|-w|--workspaces?)\b|\bworkspaces?\s+(?:run|foreach)\b")


def jest_only(package_json):
    """True when a package.json names jest and never vitest, so the queue cannot run its tests yet."""
    try:
        with open(package_json, encoding="utf-8") as fh:
            pkg = json.load(fh)
    except (OSError, ValueError):
        return False
    if not isinstance(pkg, dict):
        return False
    deps = {}
    for key in ("dependencies", "devDependencies"):
        if isinstance(pkg.get(key), dict):
            deps.update(pkg[key])
    scripts = pkg.get("scripts") if isinstance(pkg.get("scripts"), dict) else {}
    test = scripts.get("test") if isinstance(scripts.get("test"), str) else ""
    if "vitest" in deps or re.search(r"\bvitest\b", test) or DELEGATES.search(test):
        return False
    return "jest" in deps or bool(re.search(r"\bjest\b", test))


def package_json_near(cwd):
    """The package.json in cwd or the nearest parent up to the git root, or ""."""
    here = os.path.abspath(cwd or ".")
    top = stop_at(here)
    while True:
        path = os.path.join(here, "package.json")
        if os.path.isfile(path):
            return path
        parent = os.path.dirname(here)
        if here == top or parent == here:
            return ""
        here = parent


def lane_job(segment, cwd):
    """True when the segment is a direct test run that test-phase can take."""
    m = RUN.search(segment)
    if not m or INLINE_ALLOW.search(segment) or "--list-tests" in segment:
        return False
    if m.group("dotnet"):
        return reach(cwd, ("*.sln", "*.csproj"))
    if "vitest" not in segment:  # npm, pnpm or yarn test runs the repo's own script, and the queue has no jest runner
        pkg = package_json_near(cwd)
        if pkg and jest_only(pkg):
            return False
    return reach(cwd, ("pnpm-lock.yaml",))


def blocks(segments, cwd):
    """True when LEAN_DIRECT_TEST is not allow and any segment is a lane job."""
    if os.environ.get("LEAN_DIRECT_TEST", "").lower() == "allow":
        return False
    return any(lane_job(s, cwd) for s in segments)


def _self_test():
    import tempfile
    fails = []
    old = os.environ.pop("LEAN_DIRECT_TEST", None)

    def mk(root, files):
        for rel, body in files.items():
            path = os.path.join(root, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as fh:
                fh.write(body)

    def want(label, got, expected):
        if got != expected:
            fails.append(f"{label}: got {got}, want {expected}")

    with tempfile.TemporaryDirectory() as d:
        d = os.path.realpath(d)
        jest = json.dumps({"scripts": {"test": "jest"}, "devDependencies": {"jest": "29"}})
        vite = json.dumps({"scripts": {"test": "vitest run"}, "devDependencies": {"vitest": "2"}})
        both = json.dumps({"scripts": {"test": "vitest run"}, "devDependencies": {"jest": "29", "vitest": "2"}})
        mk(d, {"jest/pnpm-lock.yaml": "", "jest/package.json": jest, "vite/pnpm-lock.yaml": "",
               "vite/package.json": vite, "both/pnpm-lock.yaml": "", "both/package.json": both,
               "bare/pnpm-lock.yaml": ""})
        want("pnpm test in a jest repo", blocks(["pnpm test"], os.path.join(d, "jest")), False)
        want("npm test in a jest repo", blocks(["npm test"], os.path.join(d, "jest")), False)
        want("pnpm test in a vitest repo", blocks(["pnpm test"], os.path.join(d, "vite")), True)
        want("pnpm test with jest and vitest", blocks(["pnpm test"], os.path.join(d, "both")), True)
        want("pnpm test with no package.json", blocks(["pnpm test"], os.path.join(d, "bare")), True)
        want("vitest run in a jest repo", blocks(["vitest run"], os.path.join(d, "jest")), True)
        # A script that runs vitest is not jest only, even when vitest is not in this package.json's dependencies.
        mk(d, {"vtest/pnpm-lock.yaml": "", "vtest/package.json": json.dumps(
            {"scripts": {"test": "vitest run"}, "devDependencies": {"jest": "29"}})})
        want("pnpm test whose script runs vitest", blocks(["pnpm test"], os.path.join(d, "vtest")), True)
        # A workspace root that hands the run to its packages is not jest only.
        for name, script in (("ws-r", "pnpm -r test"), ("ws-f", "pnpm --filter web test"), ("ws-t", "turbo run test"),
                             ("ws-n", "nx run-many -t test"), ("ws-y", "yarn workspaces run test")):
            mk(d, {f"{name}/pnpm-lock.yaml": "", f"{name}/package.json": json.dumps(
                {"scripts": {"test": script}, "devDependencies": {"jest": "29"}})})
            want(f"pnpm test with {script!r}", blocks(["pnpm test"], os.path.join(d, name)), True)
        # A lock file above the repository is not this project's.
        mk(d, {"outer/pnpm-lock.yaml": "", "outer/proj/.git/HEAD": "", "outer/proj/src/x.txt": "",
               "plain/pnpm-lock.yaml": "", "plain/proj/x.txt": "",
               "repo/.git/HEAD": "", "repo/pnpm-lock.yaml": "", "repo/pkg/x.txt": ""})
        want("a stray lock above the git root", blocks(["pnpm test"], os.path.join(d, "outer", "proj", "src")), False)
        want("a stray lock above the cwd, no git", blocks(["pnpm test"], os.path.join(d, "plain", "proj")), False)
        # The package.json search stops at the git root too: a jest package.json above it is not this project's.
        mk(d, {"outer2/package.json": jest, "outer2/proj/.git/HEAD": "", "outer2/proj/pnpm-lock.yaml": "",
               "outer2/proj/src/x.txt": ""})
        want("a jest package.json above the git root", blocks(["pnpm test"], os.path.join(d, "outer2", "proj", "src")), True)
        want("a lock at the git root", blocks(["pnpm test"], os.path.join(d, "repo", "pkg")), True)
        want("a lock in the cwd", blocks(["pnpm test"], os.path.join(d, "plain")), True)
    if old is not None:
        os.environ["LEAN_DIRECT_TEST"] = old
    if fails:
        print("self-test failed:\n  " + "\n  ".join(fails))
        return 1
    print("self-test passed")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_self_test() if sys.argv[1:] == ["--self-test"] else 2)

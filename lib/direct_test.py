"""One matcher for a direct test run that a lane can take, read by script-note and wait-loop.

A segment is a lane job when it starts with dotnet test (a .sln or .csproj is in reach) or with
npm, pnpm or yarn test, npx vitest or vitest run (a pnpm-lock.yaml is in reach), because test-phase
runs dotnet projects and pnpm Vitest packages only. `dotnet test --list-tests` runs nothing.
"""
import glob
import os
import re

RUN = re.compile(
    r"^\s*(?:[A-Za-z_]\w*=\S*\s+)*(?:time\s+)?"
    r"(?:(?P<dotnet>dotnet\s+test)|(?P<node>(?:npm|pnpm|yarn)(?:\s+run)?\s+test|npx\s+vitest|vitest\s+run))(?![\w:.-])")
INLINE_ALLOW = re.compile(r"^\s*(?:[A-Za-z_]\w*=\S*\s+)*LEAN_DIRECT_TEST=allow\b", re.I)


def reach(cwd, patterns):
    """True when a pattern matches in cwd, a parent or a folder one level below cwd."""
    here = os.path.abspath(cwd or ".")
    while True:
        if any(glob.glob(os.path.join(glob.escape(here), p)) for p in patterns):
            return True
        parent = os.path.dirname(here)
        if parent == here:
            break
        here = parent
    return any(glob.glob(os.path.join(glob.escape(os.path.abspath(cwd or ".")), "*", p)) for p in patterns)


def lane_job(segment, cwd):
    """True when the segment is a direct test run that test-phase can take."""
    m = RUN.search(segment)
    if not m or INLINE_ALLOW.search(segment) or "--list-tests" in segment:
        return False
    if m.group("dotnet"):
        return reach(cwd, ("*.sln", "*.csproj"))
    return reach(cwd, ("pnpm-lock.yaml",))


def blocks(segments, cwd):
    """True when LEAN_DIRECT_TEST is not allow and any segment is a lane job."""
    if os.environ.get("LEAN_DIRECT_TEST", "").lower() == "allow":
        return False
    return any(lane_job(s, cwd) for s in segments)
